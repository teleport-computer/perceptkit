"""重复日程的滚动展开。

产品规范 §12：「Calendar 无限重复事件 | 保存 rule/series，并滚动展开查询窗口」。
先前只有 ``recurrence_identity`` 这个字段，没有展开逻辑 —— 于是"每周一的例会"
要么被展开到无限未来存进库，要么就查不到。

## 为什么只做一个子集

完整的 RFC 5545 RRULE 是一整个库的量（BYSETPOS、EXDATE、跨夏令时的
BYHOUR、闰月…），而这个包是零依赖的。硬做等于在包里重新实现一个日历库，
**而重复日程算错日期是那种「看起来完全正常」的错**：用户会准时出现在
一个不存在的会议上。

所以这里只认一个小而确定的子集，**不认识的一律明确拒绝**，
由宿主自己展开好再传进来。拒绝是安全的，猜不是。

    认    每天 / 每周（可指定星期几）/ 每月同一天，带 interval、until、count
    不认  BYSETPOS(「每月第二个周二」)、EXDATE(「除了这几天」)、
          按年、以及任何我们没把握的写法
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

#: 一次展开最多产出多少条。**必须有上限** —— "每天，永不结束"配上一个
#: 十年的查询窗口就是三千多条直接塞进模型上下文。
MAX_INSTANCES = 200

SUPPORTED_FREQ = ("daily", "weekly", "monthly")

#: 我们明确不支持的写法。列出来是为了让拒绝的理由能说清是哪一条，
#: 而不是笼统一句"不支持"。
UNSUPPORTED_KEYS = {
    "bysetpos": "「每月第二个周二」这类按序号选的规则",
    "exdate": "「除了这几天」的例外列表",
    "byyearday": "按一年中的第几天",
    "byweekno": "按第几周",
}


class RecurrenceUnsupported(ValueError):
    """这条重复规则我们不认识。**拒绝，不猜。**"""


@dataclass(frozen=True)
class RecurrenceRule:
    """一条重复规则的最小描述。"""

    freq: str
    interval: int = 1
    #: 每周重复时指定星期几，0=周一。空 = 跟着起始日那天。
    byweekday: tuple[int, ...] = ()
    #: 到这天为止（含）。``None`` = 无限重复。
    until: date | None = None
    #: 最多重复多少次。``None`` = 不限次数。
    count: int | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "RecurrenceRule":
        bad = [k for k in raw if k.lower() in UNSUPPORTED_KEYS]
        if bad:
            why = "、".join(UNSUPPORTED_KEYS[k.lower()] for k in bad)
            raise RecurrenceUnsupported(
                f"这条规则用到了{why}，我们不展开它。"
                "算错重复日程的日期是那种「看起来完全正常」的错 —— "
                "用户会准时出现在一个不存在的会议上。请宿主自己展开好再传进来"
            )
        freq = str(raw.get("freq", "")).lower()
        if freq not in SUPPORTED_FREQ:
            raise RecurrenceUnsupported(
                f"freq={raw.get('freq')!r} 不在 {list(SUPPORTED_FREQ)} 里"
            )
        # 刻意不写 `raw.get("interval", 1) or 1` —— 那会把 interval=0
        # 静默变成 1，正是"不许猜"要防的那种改写。
        raw_interval = raw.get("interval")
        interval = 1 if raw_interval is None else int(raw_interval)
        if interval < 1:
            raise RecurrenceUnsupported(
                f"interval={interval} 必须 >= 1（0 或负数没有意义，"
                "而把它当成 1 就是替上游改写了规则）"
            )

        until = raw.get("until")
        if isinstance(until, datetime):
            until = until.date()
        elif isinstance(until, str):
            until = date.fromisoformat(until[:10])

        return cls(
            freq=freq, interval=interval,
            byweekday=tuple(int(d) for d in (raw.get("byweekday") or ())),
            until=until,
            count=int(raw["count"]) if raw.get("count") is not None else None,
        )


def _step(current: date, rule: RecurrenceRule) -> date:
    if rule.freq == "daily":
        return current + timedelta(days=rule.interval)
    if rule.freq == "weekly":
        return current + timedelta(weeks=rule.interval)
    # monthly：同一天。**遇到没有那一天的月份就跳过**（1 月 31 日 → 2 月没有
    # 31 号）。不做"退回月末"，那是另一种语义，各家日历还不一致。
    y, m = current.year, current.month + rule.interval
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    try:
        return current.replace(year=y, month=m)
    except ValueError:
        return date(y, m, 28) + timedelta(days=4)   # 落到下个月，下轮再跳


def expand(
    start: datetime, rule: RecurrenceRule, *,
    window_start: date, window_end: date,
    max_instances: int = MAX_INSTANCES,
) -> list[datetime]:
    """把一条重复规则在给定窗口内展开成具体的发生时刻。

    **保留原始的时刻和时区** —— 只挪日期，不碰时分和 offset。
    每周一早上十点的会，跨夏令时之后仍然是"本地时间早上十点"。
    """
    out: list[datetime] = []
    if window_end < window_start:
        return out

    day = start.date()
    emitted = 0
    # 无限重复配上一个很远的窗口，光是走到窗口起点就可能要几万步。
    # 步数上限按窗口跨度 + 已产出条数给，够用且不会失控。
    budget = (window_end - window_start).days + max_instances * 2 + 366

    weekdays = set(rule.byweekday) if rule.freq == "weekly" and rule.byweekday else None
    # 「每两周的周一」要按【周】数间隔，不是按天。以起始日那一周的周一为基准，
    # 只有相隔整数个 interval 周的那些周才算数 —— 按天走会把每周都算上。
    week0 = start.date() - timedelta(days=start.date().weekday())

    while budget > 0 and day <= window_end:
        budget -= 1
        if rule.until is not None and day > rule.until:
            break
        if rule.count is not None and emitted >= rule.count:
            break

        hit = day >= start.date()
        if hit and weekdays is not None:
            weeks_apart = ((day - timedelta(days=day.weekday())) - week0).days // 7
            hit = day.weekday() in weekdays and weeks_apart % rule.interval == 0
        if hit:
            emitted += 1
            if day >= window_start:
                out.append(datetime.combine(day, start.timetz()))
                if len(out) >= max_instances:
                    break

        day = (day + timedelta(days=1)) if weekdays is not None else _step(day, rule)

    return out


__all__ = [
    "MAX_INSTANCES", "SUPPORTED_FREQ", "UNSUPPORTED_KEYS",
    "RecurrenceUnsupported", "RecurrenceRule", "expand",
]
