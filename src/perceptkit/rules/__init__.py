"""事件规则 —— 什么条件算一件事。

    types       EventDefinition / RuleState / Lifecycle
    evaluators  九种内置规则 + 自定义 evaluator 的接口
    engine      求值 + 生命周期(scope / fire / rearm)

**不做通用表达式 DSL**,也**不解析 YAML**(这个包零依赖)。宿主想用 YAML
写规则完全可以 —— 自己 safe_load 成 dict 再传进来。
"""
from __future__ import annotations

from .engine import evaluate, scope_key
from .evaluators import BUILTIN, RuleEvaluator
from .types import EventDefinition, Lifecycle, RuleResult, RuleState

__all__ = [
    "EventDefinition", "Lifecycle", "RuleState", "RuleResult",
    "RuleEvaluator", "BUILTIN", "evaluate", "scope_key",
]
