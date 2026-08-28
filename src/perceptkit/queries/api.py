"""读取侧 —— agent 主动来查。

写入侧（``processing``）和读取侧（这里）**方向相反、共用同一份存储**：

    设备上报 ──▶ 标准化 ──▶ 存 ──▶ 事件 ──▶ 戳醒 agent
                             ▲
                             │
                    agent 想知道什么 ──▶ 这八个函数

**这八个函数不是转发器。** 直接查数据库的宿主会漏掉四件每个宿主都必须一样的事：

    TTL 判定      过期的当前值不许冒充现在。让模型说"你电量还有 87%"
                  （其实是四小时前的），是这类系统最常见的一种说错话。
    趋势模型      波动 / 漂移 / 周期三种算法，同一句"最近怎么样"要走不同的路，
                  选错了结论就是错的。
    缺数据显式化  十四天里两天没戴表，不能当成"睡了 0 分钟" —— 那会把均值直接拉垮。
    隐私投影      精确坐标、BSSID 这类字段永远不给 agent 看，由 manifest 声明，
                  不靠每个宿主自觉。

**工具层（MCP schema、工具名、中文描述、给谁开）留在宿主。** kit 提供数据和契约，
宿主决定怎么把它暴露成工具 —— 那一层全是产品决策，换个宿主全不一样。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from .. import history as _history
from .. import trend_models as _trend
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort

#: 所有 list 查询的默认与硬上限。agent 问一句"我这个月都去过哪"，
#: 不设上限就是几千条直接塞进模型上下文。
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


def visible_fields(sig: SignalDefinition, *, on_demand: bool = True) -> tuple[str, ...]:
    """这个信号有哪些字段能给 agent 看。

    ``query_visibility="never"`` 的一律排除 —— 精确坐标、BSSID 这类既不该
    持久化也不该进模型上下文，由 manifest 声明成规则，不靠每个宿主自觉。
    """
    allowed = {"always"} | ({"on_demand"} if on_demand else set())
    return tuple(f.key for f in sig.fields if f.query_visibility in allowed)


def project(sig: SignalDefinition, value: Mapping[str, Any] | None,
            *, on_demand: bool = True) -> dict[str, Any] | None:
    """按 manifest 的可见性过滤一份 payload。"""
    if value is None:
        return None
    keep = set(visible_fields(sig, on_demand=on_demand))
    return {k: v for k, v in value.items() if k in keep}


# ---------------------------------------------------------------------------
# 当前值
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CurrentView:
    """一个信号的当前状态，以及"它还算不算当前"。"""

    signal: str
    #: ``fresh`` / ``stale`` / ``unavailable`` / ``no_data``
    state: str
    value: dict[str, Any] | None
    #: 最后一次可靠值。**stale 时也给**，但必须带 ``as_of``，不能冒充当前。
    last_known: dict[str, Any] | None = None
    as_of: str | None = None


def get_current(
    storage: StoragePort, *, subject_id: str, signals: Sequence[str],
    manifest: Mapping[str, SignalDefinition], now: datetime,
    on_demand: bool = True,
) -> dict[str, CurrentView]:
    """取当前值，带 TTL 判定和隐私投影。"""
    out: dict[str, CurrentView] = {}
    raw = storage.get_current(subject_id=subject_id, signals=list(signals))
    for signal in signals:
        sig = manifest.get(signal)
        projections = raw.get(signal) or []
        if sig is None or not projections:
            out[signal] = CurrentView(signal, "no_data", None)
            continue
        proj = max(projections, key=lambda p: p.observed_at)
        visible = project(sig, proj.typed_value, on_demand=on_demand)
        fresh = proj.expires_at is None or proj.expires_at > now
        if proj.availability != "observed":
            state = "unavailable"
        else:
            state = "fresh" if fresh else "stale"
        out[signal] = CurrentView(
            signal=signal,
            state=state,
            value=visible if state == "fresh" else None,
            last_known=visible,
            as_of=proj.observed_at.isoformat(),
        )
    return out


def get_last_known(
    storage: StoragePort, *, subject_id: str, signal: str,
    manifest: Mapping[str, SignalDefinition], on_demand: bool = True,
) -> CurrentView:
    """最后一次可靠值，**不判 TTL**。

    和 ``get_current`` 的区别是意图：这个函数的调用方已经知道自己要的是
    "最后一次"，不是"现在"。所以永远带 ``as_of``，永远不说 fresh。
    """
    sig = manifest.get(signal)
    projections = storage.get_current(subject_id=subject_id, signals=[signal]).get(signal)
    if sig is None or not projections:
        return CurrentView(signal, "no_data", None)
    proj = max(projections, key=lambda p: p.observed_at)
    return CurrentView(
        signal=signal, state="last_known", value=None,
        last_known=project(sig, proj.typed_value, on_demand=on_demand),
        as_of=proj.observed_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------

def list_timeline(
    storage: StoragePort, *, subject_id: str, signal: str,
    manifest: Mapping[str, SignalDefinition],
    start: datetime | None = None, end: datetime | None = None,
    cursor: str | None = None, limit: int = DEFAULT_LIMIT,
    on_demand: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """原始观测时间线。分页，有硬上限。"""
    sig = manifest.get(signal)
    rows, nxt = storage.list_observations(
        subject_id=subject_id, signal=signal, start=start, end=end,
        cursor=cursor, limit=_clamp(limit),
    )
    out = [
        {
            "occurred_at": o.occurred_at.isoformat(),
            "availability": o.availability,
            "value": project(sig, o.typed_value, on_demand=on_demand) if sig else None,
            "local_date": o.effective_local_date.isoformat(),
        }
        for o in rows
    ]
    return out, nxt


@dataclass(frozen=True)
class DailyView:
    date: str
    value: dict[str, Any]
    #: 这一天有没有数据。**空缺的日子不补零** —— `no_data` 不是 0。
    has_data: bool = True


def get_daily_aggregates(
    storage: StoragePort, *, subject_id: str, signal: str,
    start_date: date, end_date: date,
) -> list[DailyView]:
    """日聚合。**只返回有数据的日子，不补零。**

    补零是这类系统最常见的一个静默错误：十四天里两天没戴表，补两个 0 进去，
    平均睡眠时长立刻被拉垮，而且没有任何地方报错。
    """
    rows = storage.get_aggregate(
        subject_id=subject_id, signal=signal,
        start_date=start_date, end_date=end_date, aggregation_kind="daily",
    )
    return [
        DailyView(date=r.local_date.isoformat(), value=r.typed_aggregate)
        for r in sorted(rows, key=lambda r: r.local_date)
    ]


def get_trend(
    storage: StoragePort, *, subject_id: str, signal: str, field: str,
    manifest: Mapping[str, SignalDefinition],
    start_date: date, end_date: date,
) -> dict[str, Any]:
    """趋势。**按 manifest 声明的模型选算法** —— 选错了结论就是错的。

    返回里一定带 ``days_with_data`` / ``days_missing``：缺了几天必须说出来，
    否则调用方无从判断这个趋势可不可信。
    """
    sig = manifest.get(signal)
    if sig is None:
        return {"model": "none", "reason": f"manifest 里没有 {signal}"}
    fd = sig.field_map().get(field)
    if fd is None:
        return {"model": "none", "reason": f"{signal} 没有 {field} 这个字段"}
    if fd.query_visibility == "never":
        return {"model": "none", "reason": "这个字段不对 agent 开放"}

    rows = storage.get_aggregate(
        subject_id=subject_id, signal=signal,
        start_date=start_date, end_date=end_date, aggregation_kind="daily",
    )
    docs = [
        {"date": r.local_date.isoformat(), "doc": r.typed_aggregate}
        for r in sorted(rows, key=lambda r: r.local_date)
    ]
    span = (end_date - start_date).days + 1
    coverage = {"days_with_data": len(docs), "days_missing": max(0, span - len(docs))}

    if not docs:
        return {"model": fd.trend_model, "reason": "这段时间一条数据都没有", **coverage}

    # 三种模型走三条完全不同的路。旧的按信号名查表那条路(trend_models.model_for)
    # 保留不动;这里按 manifest 走,因为 manifest 是新的单一声明处。
    model = fd.trend_model
    if model == "drifting":
        result = _trend.read_drift(docs, signal, field)
    elif model == "cyclical":
        result = _trend.read_cycles([d["date"] for d in docs],
                                    today=end_date.isoformat())
    elif model == "fluctuating":
        result = _history.read_trend(docs, signal, field)
    else:
        return {"model": "none", "reason": "这个字段没有声明趋势模型", **coverage}

    return {"model": model, "unit": fd.unit, **coverage, **result}


# ---------------------------------------------------------------------------
# 来源镜像与事件
# ---------------------------------------------------------------------------

def list_calendar_events(
    storage: StoragePort, *, subject_id: str,
    start: datetime | None = None, end: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """当前来源镜像里的日程。

    ⚠️ 同步长期失败时应该显示 stale，而不是继续声称这是最新完整的数据 ——
    调用方要自己去看 ``SourceSyncState.last_successful_sync_at``。
    """
    rows = storage.list_calendar_events(
        subject_id=subject_id, start=start, end=end, limit=_clamp(limit),
    )
    return [{"source_event_id": e.source_event_id, **e.event_fields} for e in rows]


def list_reminders(
    storage: StoragePort, *, subject_id: str, include_completed: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    rows = storage.list_reminders(
        subject_id=subject_id, include_completed=include_completed,
        limit=_clamp(limit),
    )
    return [{"source_reminder_id": r.source_reminder_id, **r.reminder_fields}
            for r in rows]


def list_events(
    storage: StoragePort, *, subject_id: str, limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """这个用户还没送达的事件。用于排查"为什么没提醒我"。"""
    return [
        {
            "event_id": e.event_id, "type": e.event_type,
            "occurred_at": e.occurred_at.isoformat(),
            "delivery_state": e.delivery_state, "attempts": e.attempt_count,
        }
        for e in storage.list_pending_events(subject_id=subject_id,
                                             limit=_clamp(limit))
    ]


__all__ = [
    "DEFAULT_LIMIT", "MAX_LIMIT", "CurrentView", "DailyView",
    "visible_fields", "project",
    "get_current", "get_last_known", "list_timeline", "get_daily_aggregates",
    "get_trend", "list_calendar_events", "list_reminders", "list_events",
]
