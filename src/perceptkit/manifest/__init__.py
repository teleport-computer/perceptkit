"""Manifest —— 每个信号、每个字段的单一声明处。

在这之前，一个字段的属性散在七个互不关联的地方（catalog / fields / history /
retention / attribution / trend_models / prompts）。加一个字段要同时改七处，
**漏一处不会有任何东西变红**。manifest 把它们并成一处，并让四条自动检查
有了施力点。

    types    声明的形状（SignalDefinition / FieldDefinition + 允许的取值）
    minimal  最小可执行 manifest：五个信号，覆盖四种存储形态
    checks   四条自动检查，对应产品规范 §8

manifest 只声明属性，不实现算法 —— 但它声明的每个名字都必须能解析到实现，
``check_named_implementations_exist`` 盯着这条。
"""
from __future__ import annotations

from .checks import (
    check_history_has_retention,
    check_named_implementations_exist,
    check_types_and_units,
    check_wake_eligible_fields_have_comparators,
    validate_manifest,
)
from .minimal import (
    BATTERY,
    FOCUS_STATE,
    LOCATION_CITY,
    MINIMAL_SIGNALS,
    PRESENCE_RECOVERY,
    STEPS,
)
from .types import PERMANENT, FieldDefinition, SignalDefinition

__all__ = [
    "SignalDefinition", "FieldDefinition", "PERMANENT",
    "MINIMAL_SIGNALS",
    "BATTERY", "PRESENCE_RECOVERY", "STEPS", "LOCATION_CITY", "FOCUS_STATE",
    "validate_manifest",
    "check_types_and_units", "check_history_has_retention",
    "check_named_implementations_exist",
    "check_wake_eligible_fields_have_comparators",
]
