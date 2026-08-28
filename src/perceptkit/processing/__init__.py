"""处理管线 —— 谁在什么时候被调用。

**这是上一版真正缺的东西。** 算式都在,但顺序留在了宿主的业务代码里 ——
别人拿到的是一盒零件和一本没有装配图的说明书。

    normalize  校验 · 定时区 · 算归属日期 · 算去重身份
    aggregate  按 manifest 声明的策略路由到 history 里那批算法(不重新实现)
    pipeline   前七步:批级幂等 -> 标准化 -> 观测级幂等 -> 写观测 ->
               记身份 -> 更新当前值 -> 折进聚合
"""
from __future__ import annotations

from .aggregate import aggregating_fields, fold_into_day
from .normalize import (
    NormalizedObservation,
    NormalizeResult,
    normalize_observations,
    validate_value,
)
from .pipeline import AGGREGATION_VERSION, IngestOutcome, ingest_report

__all__ = [
    "normalize_observations", "validate_value",
    "NormalizedObservation", "NormalizeResult",
    "aggregating_fields", "fold_into_day",
    "ingest_report", "IngestOutcome", "AGGREGATION_VERSION",
]

from .dispatch import (  # noqa: E402
    DispatchOutcome,
    RuleOutcome,
    dispatch_once,
    drain,
    evaluate_and_enqueue,
    event_id_for,
)

from .scheduled import (  # noqa: E402
    ScheduledOutcome,
    evaluate_absence,
    evaluate_daily,
    streak_length,
)

__all__ += [
    "evaluate_and_enqueue", "RuleOutcome", "event_id_for",
    "dispatch_once", "drain", "DispatchOutcome",
    "evaluate_daily", "evaluate_absence", "streak_length", "ScheduledOutcome",
]
