"""趋势按三类模型分开算。

★ 为什么不能一套算法走天下（这是本模块存在的全部理由）：
  现有 read_trend 是「跟最近 N 天的中位数比」，它只适合围绕一个稳定值波动的量。
  体重一年从 80 掉到 60，用它会得出「你比平时轻 10 公斤」——而这个人
  从来没有一个叫「平时」的体重。周期性的量（经期）同样不适用。

★ 零 I/O、纯函数：日期只做字符串解析与差值，不取当前时间。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import datetime as _dt

FLUCTUATING = "fluctuating"   # 有「平时水平」，偏离才是信号
DRIFTING = "drifting"         # 没有「平时水平」，方向与速率才是信号
CYCLICAL = "cyclical"         # 看间隔，不看数值高低

# signal -> 模型。未列出的信号沿用波动型（现有 read_trend 的行为）。
TREND_MODEL: dict[str, str] = {
    "health_sleep": FLUCTUATING,
    "health_vitals": FLUCTUATING,
    "health_activity": FLUCTUATING,
    "health_body": DRIFTING,
    "health_cycle": CYCLICAL,
    # 只查询、不叫醒（见下方 QUERY_ONLY）：血糖/血压/当前心率
    "health_metabolic": FLUCTUATING,
}

# 允许进入主动叫醒的指标必须同时满足：有模型 + 不在这张表里。
#
# 血糖、血压、当前心率缺餐前/餐后、体位、运动关系与稳定采样协议，
# 这些数不能直接做同质趋势比较 —— 给 agent 查得到，但不主动打扰。
QUERY_ONLY: frozenset[str] = frozenset({
    "health_metabolic",
})

# 字段级的"只查询、不叫醒"——比 QUERY_ONLY 更细一档（Codex code_review
# 2026-08-23 抓到）：当前心率不是一个独立信号，是 health_vitals 信号里的
# 一个字段；而 health_vitals 整体是 FLUCTUATING 且不在 QUERY_ONLY 里，
# 单看"有模型 + 不在 QUERY_ONLY"会让当前心率被判定成可以叫醒——但它每次
# 心跳都在变，跟血糖血压一样缺采样协议，不该拿来触发主动打扰。
# 存 (signal, field) 二元组，不是单独一张 field 名单：同名字段换了信号
# 语境可能就该叫醒，必须连着信号一起认。
QUERY_ONLY_FIELDS: frozenset[tuple[str, str]] = frozenset({
    ("health_vitals", "current_heart_rate"),
})

# 不参与趋势的派生量 / 常量。
#
# BMI 是体重的派生量：体重掉了 BMI 必然跟着掉，两个都叫醒等于同一件事叫两次。
# 成人身高基本是常量，趋势无意义。
DERIVED_OR_CONSTANT_FIELDS: frozenset[str] = frozenset({
    "bmi",
    "height_cm",
})


def wake_eligible(signal: str, field: str | None = None) -> bool:
    """这个 (signal, field) 允不允许触发主动叫醒 —— 唯一的判定入口。

    ``QUERY_ONLY``（整个信号只查询）、``QUERY_ONLY_FIELDS``（信号内单个字段
    只查询，如当前心率）、``DERIVED_OR_CONSTANT_FIELDS``（派生量/常量字段，
    与信号无关）三张表一起查——接线层不该自己拼「有模型 + 不在 QUERY_ONLY」
    这条逻辑，那样会漏掉字段级例外。``field`` 缺省时只判信号级。
    """
    if field is not None and field in DERIVED_OR_CONSTANT_FIELDS:
        return False
    if signal in QUERY_ONLY:
        return False
    if field is not None and (signal, field) in QUERY_ONLY_FIELDS:
        return False
    return True

_DAYS_PER_MONTH = 30.4375     # 365.25 / 12，避免月份长度参差


def model_for(signal: str) -> str:
    return TREND_MODEL.get(signal, FLUCTUATING)


def _date(raw) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _numeric(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _points(rows: Sequence[Mapping], field: str | None) -> list[tuple[_dt.date, float]]:
    out: list[tuple[_dt.date, float]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        d = _date(row.get("date"))
        doc = row.get("doc")
        v = _numeric(doc.get(field)) if (isinstance(doc, Mapping) and field) else None
        if d is not None and v is not None:
            out.append((d, v))
    out.sort(key=lambda p: p[0])
    return out


def _rate_per_month(points: list[tuple[_dt.date, float]]) -> float | None:
    if len(points) < 2:
        return None
    span_days = (points[-1][0] - points[0][0]).days
    if span_days <= 0:
        return None
    delta = points[-1][1] - points[0][1]
    return round(delta / (span_days / _DAYS_PER_MONTH), 3)


def read_drift(rows: Sequence[Mapping], signal: str, field: str | None = None) -> dict:
    """漂移型：起止对比 + 每月变化速率 + 近期是否在加速。"""
    points = _points(rows, field)
    first = {"date": points[0][0].isoformat(), "value": points[0][1]} if points else None
    last = {"date": points[-1][0].isoformat(), "value": points[-1][1]} if points else None
    total = round(points[-1][1] - points[0][1], 3) if len(points) >= 2 else (
        0.0 if len(points) == 1 else None
    )
    overall = _rate_per_month(points)

    # 近期速率：最后三分之一的点（至少 2 个）
    recent = None
    if len(points) >= 4:
        recent = _rate_per_month(points[-max(2, len(points) // 3):])

    accelerating = False
    if overall is not None and recent is not None and overall != 0.0:
        # 同向且更陡才算加速；反向或走平都不算
        accelerating = (recent * overall > 0) and (abs(recent) > abs(overall))

    return {
        "signal": signal,
        "field": field,
        "model": DRIFTING,
        "first": first,
        "last": last,
        "total_delta": total,
        "per_month": overall,
        "recent_per_month": recent,
        "accelerating": accelerating,
        "n": len(points),
    }


def read_cycles(events: Sequence[str], *, today: str) -> dict:
    """周期型：看相邻两次的间隔，以及这次是否比往常推迟。

    ``events`` 是事件日期（升序或乱序都行，内部会排序去重）；
    ``today`` 由调用方传入 —— 内核不取当前时间。
    """
    dates = sorted({d for d in (_date(e) for e in (events or ())) if d is not None})
    now = _date(today)

    intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
    typical = None
    if intervals:
        ordered = sorted(intervals)
        mid = len(ordered) // 2
        typical = (ordered[mid] if len(ordered) % 2
                   else round((ordered[mid - 1] + ordered[mid]) / 2))

    days_since = (now - dates[-1]).days if (now is not None and dates) else None
    overdue = None
    if typical is not None and days_since is not None:
        overdue = max(0, days_since - typical)

    return {
        "model": CYCLICAL,
        "intervals": intervals,
        "typical_interval": typical,
        "days_since_last": days_since,
        "overdue_by": overdue,
        "n": len(dates),
    }
