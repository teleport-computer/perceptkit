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
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from ..algorithms import history as _history
from ..algorithms import trend_models as _trend
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort
from ..processing import recurrence as _recurrence

#: 所有 list 查询的默认与硬上限。agent 问一句"我这个月都去过哪"，
#: 不设上限就是几千条直接塞进模型上下文。
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


def _page(rows: list, cursor: str | None, limit: int) -> tuple[list, str | None]:
    """按偏移量分页。

    **只用在有界集合上**（一个人的日历镜像、待投递的事件）—— 观测时间线
    那种可能上百万行的，用的是 ``(occurred_at, observation_id)`` 键游标，
    因为偏移量分页在中间插入一条迟到数据时会漏掉或重复一条，而且不报错。
    这里的集合不会被中途插入到打乱顺序的程度，用偏移量换取不改端口。
    """
    start = int(cursor) if cursor else 0
    size = _clamp(limit)
    page = rows[start:start + size]
    return page, (str(start + size) if start + size < len(rows) else None)


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
    cursor: str | None = None, limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    """当前来源镜像里的日程。

    ⚠️ 同步长期失败时应该显示 stale，而不是继续声称这是最新完整的数据 ——
    调用方要自己去看 ``SourceSyncState.last_successful_sync_at``。
    """
    # 先按窗口取一批（上限而不是页大小 —— 重复日程展开之后条数会变多，
    # 按页大小取会让展开后不足一页）。
    rows = storage.list_calendar_events(
        subject_id=subject_id, start=start, end=end, limit=MAX_LIMIT,
    )
    out: list[dict[str, Any]] = []
    for e in rows:
        base = {"source_event_id": e.source_event_id, **e.event_fields}
        rule_raw = e.event_fields.get("recurrence")
        if not rule_raw or start is None or end is None:
            # 没有窗口就不展开 —— "把所有重复日程都给我"对一条无限重复的
            # 规则没有答案，只有一个上限截断出来的假象。
            out.append(base)
            continue
        try:
            rule = _recurrence.RecurrenceRule.parse(rule_raw)
            occurrences = _recurrence.expand(
                e.event_fields["start_at"], rule,
                window_start=start.date(), window_end=end.date(),
            )
        except (_recurrence.RecurrenceUnsupported, KeyError, ValueError) as exc:
            # 展不开就把系列本身交出去并说清原因，**不猜日期**。
            # 算错重复日程会让用户准时出现在一个不存在的会议上。
            out.append({**base, "recurrence_expanded": False,
                        "recurrence_note": str(exc)})
            continue
        for at in occurrences:
            out.append({**base, "start_at": at, "recurrence_expanded": True,
                        "recurrence_identity": e.recurrence_identity
                        or e.source_event_id})
    return _page(out, cursor, limit)


def list_reminders(
    storage: StoragePort, *, subject_id: str, include_completed: bool = False,
    cursor: str | None = None, limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    rows = storage.list_reminders(
        subject_id=subject_id, include_completed=include_completed,
        limit=MAX_LIMIT,
    )
    return _page([{"source_reminder_id": r.source_reminder_id, **r.reminder_fields}
                  for r in rows], cursor, limit)


def list_events(
    storage: StoragePort, *, subject_id: str, status: str | None = None,
    event_type: str | None = None,
    start: datetime | None = None, end: datetime | None = None,
    cursor: str | None = None, limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    """这个用户的事件。用于排查「为什么没提醒我」。

    ``status`` 按投递状态筛。**「为什么没提醒我」的答案往往不是 pending，
    而是 suppressed 或 rejected** —— 只能看待投递的话，那些事件在排查时
    根本不出现，看上去就像"压根没产生过"。

    返回 ``(事件, 下一页游标)``。所有 list 查询都必须分页或有明确上限
    （产品规范 §15）。
    """
    rows = list(storage.list_pending_events(subject_id=subject_id,
                                            limit=MAX_LIMIT))
    if status is not None:
        rows = [e for e in rows if e.delivery_state == status]
    if event_type is not None:
        rows = [e for e in rows if e.event_type == event_type]
    if start is not None:
        rows = [e for e in rows if e.occurred_at >= start]
    if end is not None:
        rows = [e for e in rows if e.occurred_at <= end]
    page, nxt = _page(rows, cursor, limit)
    return [
        {
            "event_id": e.event_id, "type": e.event_type,
            "occurred_at": e.occurred_at.isoformat(),
            "delivery_state": e.delivery_state, "attempts": e.attempt_count,
        }
        for e in page
    ], nxt


def list_definitions(
    definitions: Sequence[Any], *, subject_id: str | None = None,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    """这个用户当前配着哪些规则（产品规范 §15）。

    **不查存储** —— 规则定义是宿主装配 kit 时传进来的配置，不是 kit 存的数据。
    宿主按用户存规则的话，自己筛完再传进来；``subject_id`` 只是原样带回，
    方便调用方对上是谁的。

    为什么需要这个查询：用户能自己配规则，就一定会问「我配的那条还在吗、
    是不是被关掉了」。没有它，唯一的排查手段是去读宿主的配置表 ——
    而那正是这个包想让宿主不必自己造的东西。
    """
    out: list[dict[str, Any]] = []
    for d in definitions:
        enabled = getattr(d, "enabled", True)
        if not include_disabled and not enabled:
            continue
        out.append({
            "definition_id": getattr(d, "definition_id", None),
            "version": getattr(d, "version", None),
            "signal": getattr(d, "signal", None),
            "field": getattr(d, "field_name", None),
            "condition": getattr(d, "condition_type", None),
            "event_type": getattr(d, "event_type", None),
            # 关掉的规则**也列出来并标明** —— 「它没触发」和「它被关了」
            # 是两个完全不同的排查方向。
            "enabled": enabled,
            "subject_id": subject_id,
        })
    return out


def export_subject(
    storage: StoragePort, *, subject_id: str,
    manifest: Mapping[str, SignalDefinition],
    start: datetime | None = None, end: datetime | None = None,
    per_signal_limit: int = MAX_LIMIT,
) -> dict[str, Any]:
    """把一个人的全部感知数据导出来。

    产品规范 §8 要的是「按 subject 定位、**导出**和删除」。删除有
    ``purge_subject``，定位有各个 list —— 导出这一半以前没有。
    没有它，"把我的数据给我"只能靠调用方自己把八个查询拼一遍，
    而**拼漏一个就是少给了用户一部分数据，还没人会发现**。

    刻意用现有端口方法拼，不新增端口：导出不是热路径，为它增加所有宿主
    都必须实现的一个方法不划算。

    ⚠️ 导出的是 kit 管的东西。宿主自己存的（原始载荷、加密信封、
    它自己的业务表）要由宿主追加进来 —— 返回值里的 ``kit_managed_only``
    就是提醒这件事的。
    """
    signals = sorted(manifest)
    observations: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        rows, _ = list_timeline(
            storage, subject_id=subject_id, signal=signal, manifest=manifest,
            start=start, end=end, limit=per_signal_limit,
            # 导出给用户本人，所以按需字段也要给；
            # `query_visibility="never"` 的仍然不给 —— 那些**根本没存**。
            on_demand=True,
        )
        if rows:
            observations[signal] = rows

    current = {
        signal: {"state": view.state, "value": view.value,
                 "last_known": view.last_known, "as_of": view.as_of}
        for signal, view in get_current(
            storage, subject_id=subject_id, signals=signals,
            manifest=manifest, now=end or _far_future(),
        ).items()
        if view.state != "no_data"
    }

    return {
        "subject_id": subject_id,
        "kit_managed_only": True,
        "current": current,
        "observations": observations,
        # 导出取第一页 —— 一个人的日历镜像是有界的，per_signal_limit 已经够大。
        "calendar_events": list_calendar_events(
            storage, subject_id=subject_id, start=start, end=end,
            limit=per_signal_limit)[0],
        "reminders": list_reminders(storage, subject_id=subject_id,
                                    include_completed=True,
                                    limit=per_signal_limit)[0],
        # 导出取第一页就够 —— 待投递的事件不是"用户的数据"，
        # 是我们还没送到的东西，列在这里只为完整。
        "pending_events": list_events(storage, subject_id=subject_id,
                                      limit=per_signal_limit)[0],
    }


def _far_future() -> datetime:
    """导出不判新鲜度 —— 用户要的是"我有什么数据"，不是"哪些还算当前"。

    这个包不读时钟，所以用一个固定的远期时间，而不是 ``now()``。
    """
    return datetime(9999, 12, 31, tzinfo=timezone.utc)


__all__ = [
    "DEFAULT_LIMIT", "MAX_LIMIT", "CurrentView", "DailyView",
    "visible_fields", "project",
    "get_current", "get_last_known", "list_timeline", "get_daily_aggregates",
    "export_subject", "list_definitions",
    "get_trend", "list_calendar_events", "list_reminders", "list_events",
]
