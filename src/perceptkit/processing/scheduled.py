"""由时钟驱动的两种规则 —— 主管线跑不到它们。

主管线是**数据驱动**的：有观测进来才跑。这对九种规则里的七种都对，
但另外两种不行：

    streak    "连续三天睡眠不足" —— 要读历史，而且【一天只该判一次】
              前台每 30 秒一条观测，跟着观测跑就是一天几千次，
              而这件事一天只可能变化一次。
              → 挂在【某天的聚合算完】那一刻

    absence   "你三天没记录体重了" —— **没有数据才该触发**
              跟着观测跑的话，它永远等不到自己被调用的那一刻。
              → 由定时器驱动

**宿主不用为此多起一个东西。** 投递那条线本来就需要一个定时循环
（``dispatch_pending`` 得有人定期调），这两个搭在同一个循环上就行。

⚠️ 产品规范把 ``absence`` 列进了内置规则，但**整份文档没说它由谁驱动** ——
其他八种都是"数据来了 → 判断"，只有它是"数据没来 → 判断"。这是规范的一处缺口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..contracts.context import IngestContext
from ..contracts.event import PerceptionEvent
from ..contracts.records import StoredObservation
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort
from ..rules.types import EventDefinition, RuleResult
from .dispatch import RuleOutcome, definitions_for_signal, evaluate_and_enqueue
from .normalize import NormalizedObservation

#: 算连续天数时最多往回看多少天。不设上限的话，一条"连续 N 天"的规则
#: 会在每次日聚合时把整个历史读一遍。
MAX_STREAK_LOOKBACK_DAYS = 90

_OPS: dict[str, Callable[[float, float], bool]] = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
}


@dataclass
class ScheduledOutcome:
    events: list[PerceptionEvent] = field(default_factory=list)
    misses: list[tuple[str, str | None]] = field(default_factory=list)


def _daily_value(doc: Mapping[str, Any], field_key: str, strategy: str) -> float | None:
    """从一天的聚合文档里取出那个字段的代表数字。

    不同聚合方式的文档形状不一样 —— 这里只处理数值型的那几种，
    其余返回 ``None``（比如"按状态分时长"就没有单一代表值）。
    """
    cell = doc.get(field_key)
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return float(cell)
    if not isinstance(cell, Mapping):
        return None
    if strategy in ("daily_total", "cumulative"):
        raw = cell.get("total")
    elif strategy == "numeric_dist":
        # 用平均值当代表：min/max 太容易被单次异常读数带偏。
        if cell.get("count"):
            raw = cell["sum"] / cell["count"]
        else:
            raw = None
    else:
        raw = cell.get("value")
    return float(raw) if isinstance(raw, (int, float)) else None


def streak_length(
    storage: StoragePort,
    definition: EventDefinition,
    signal: SignalDefinition,
    *,
    subject_id: str,
    through: date,
    max_days: int = MAX_STREAK_LOOKBACK_DAYS,
) -> int:
    """从 ``through`` 往回数，连续多少天满足这条规则的**每天条件**。

    **缺数据的那天直接断掉，不跳过。** "连续三天睡眠不足"里如果有一天没戴表，
    那就不是连续三天 —— 把缺失当成"满足"或"跳过"都是在替用户编事实。
    """
    fd = signal.field_map().get(definition.field_name or "")
    if fd is None:
        return 0
    op = _OPS.get(definition.operator or "lt")
    threshold = definition.value
    if op is None or not isinstance(threshold, (int, float)):
        return 0

    start = through - timedelta(days=max_days - 1)
    by_day = {
        a.local_date: a.typed_aggregate
        for a in storage.get_aggregate(
            subject_id=subject_id, signal=signal.key,
            start_date=start, end_date=through, aggregation_kind="daily",
        )
    }

    count = 0
    day = through
    while day >= start:
        doc = by_day.get(day)
        if doc is None:
            break                      # 那天没数据 —— 连续断了
        value = _daily_value(doc, definition.field_name or "", fd.aggregation_strategy)
        if value is None or not op(value, float(threshold)):
            break
        count += 1
        day -= timedelta(days=1)
    return count


def _fake_observation(
    signal: SignalDefinition, *, subject_id: str, when: datetime, day: date,
) -> NormalizedObservation:
    """给 ``evaluate_and_enqueue`` 造一个载体。

    定时求值没有真实观测（这正是它存在的理由），但事件仍然要落到某个
    signal / 某一天上。造一条**不落库**的载体，只用来把上下文传下去。
    """
    stored = StoredObservation(
        observation_id=f"scheduled:{signal.key}:{day.isoformat()}",
        subject_id=subject_id, signal=signal.key, signal_schema_version=signal.schema_version,
        source="scheduler", occurred_at=when, received_at=when,
        availability="observed", effective_local_date=day, typed_value={},
    )
    return NormalizedObservation(
        stored=stored, identity_digest=stored.observation_id,
        fact_key=stored.observation_id, content_digest="",
    )


def evaluate_daily(
    *,
    storage: StoragePort,
    subject_id: str,
    local_date: date,
    now: datetime,
    signals: Mapping[str, SignalDefinition],
    definitions: Sequence[EventDefinition],
    extra_evaluators: Mapping[str, Callable[..., RuleResult]] | None = None,
) -> ScheduledOutcome:
    """某一天的聚合算完之后调一次，跑 ``streak`` 这类按天判的规则。

    宿主在跨日时调用（每个 subject 一次），不要跟着观测跑。
    """
    outcome = ScheduledOutcome()
    context = IngestContext(subject_id=subject_id, received_at=now)

    for definition in definitions:
        if definition.condition_type != "streak" or not definition.enabled:
            continue
        signal = signals.get(definition.signal)
        if signal is None:
            continue
        length = streak_length(
            storage, definition, signal, subject_id=subject_id, through=local_date,
        )
        item = _fake_observation(signal, subject_id=subject_id, when=now, day=local_date)
        rules: RuleOutcome = evaluate_and_enqueue(
            item, context=context, storage=storage, definitions=[definition],
            extra_evaluators=extra_evaluators,
            extra_context={"streak_length": length},
            signal_definition=signal,
        )
        outcome.events.extend(rules.events)
        outcome.misses.extend(rules.misses)
    return outcome


def evaluate_absence(
    *,
    storage: StoragePort,
    subject_id: str,
    now: datetime,
    signals: Mapping[str, SignalDefinition],
    definitions: Sequence[EventDefinition],
    extra_evaluators: Mapping[str, Callable[..., RuleResult]] | None = None,
) -> ScheduledOutcome:
    """定时调，跑 ``absence``（该来的没来）这类规则。

    **静默时长从当前值的观测时刻算起** —— 用当前值而不是翻观测明细，
    因为明细可能已经按保留期清理掉了，而当前值一定还在。
    """
    outcome = ScheduledOutcome()
    context = IngestContext(subject_id=subject_id, received_at=now)

    for definition in definitions:
        if definition.condition_type != "absence" or not definition.enabled:
            continue
        signal = signals.get(definition.signal)
        if signal is None:
            continue

        projections = storage.get_current(
            subject_id=subject_id, signals=[definition.signal]
        ).get(definition.signal) or []
        if not projections:
            # 从来没有过数据。**不触发** —— "你三天没记录体重了"对一个
            # 从没记过体重的用户来说是句莫名其妙的话。
            outcome.misses.append((definition.definition_id, "这个信号从来没有过数据"))
            continue

        last = max(p.observed_at for p in projections)
        silent = (now - last).total_seconds()
        item = _fake_observation(
            signal, subject_id=subject_id, when=now, day=now.date(),
        )
        rules = evaluate_and_enqueue(
            item, context=context, storage=storage, definitions=[definition],
            extra_evaluators=extra_evaluators,
            extra_context={"silent_seconds": silent},
            signal_definition=signal,
        )
        outcome.events.extend(rules.events)
        outcome.misses.extend(rules.misses)
    return outcome


__all__ = [
    "MAX_STREAK_LOOKBACK_DAYS", "ScheduledOutcome",
    "streak_length", "evaluate_daily", "evaluate_absence",
]
