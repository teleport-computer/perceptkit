"""求值引擎 —— 把 evaluator 的"命中了吗"变成"该不该产生事件"。

生命周期统一在这里处理,不让九个 evaluator 各写一遍:

    scope     这条规则在什么范围内计一次(每天 / 永远)
    fire      一个范围内触发几次(一次 / 每次)
    rearm     什么时候重新武装(次日 / 冷却后 / 永不)

**scope 换了就是一条新状态。** "今天已经触发过"不会影响明天 —— 这就是
``rearm=next_scope`` 的实现方式:不需要定时任务去重置,换个 scope_key 就行。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Mapping

from .evaluators import BUILTIN
from .types import EventDefinition, RuleResult, RuleState


def scope_key(definition: EventDefinition, *, local_date: date) -> str:
    """这条规则此刻算在哪个范围里。"""
    if definition.lifecycle.scope == "local_day":
        return local_date.isoformat()
    return "forever"


def _cooled_down(state: RuleState, *, now: datetime, cooldown: float) -> bool:
    if cooldown <= 0 or not state.last_fired_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_fired_at)
    except ValueError:
        return True
    return (now - last).total_seconds() >= cooldown


def evaluate(
    definition: EventDefinition,
    state: RuleState,
    current: Any,
    *,
    now: datetime,
    context: Mapping[str, Any] | None = None,
    extra_evaluators: Mapping[str, Callable[..., RuleResult]] | None = None,
) -> RuleResult:
    """求值一条规则,返回是否触发以及**下一版状态**。

    调用方必须把返回的 state 写回去 —— 哪怕没触发。``previous_value`` 每次
    都要更新,否则 ``threshold_crossing`` 永远拿不到正确的前值。
    """
    ctx = dict(context or {})
    if not definition.enabled:
        return RuleResult(False, state, reason="规则已停用")

    evaluators = dict(BUILTIN)
    if extra_evaluators:
        evaluators.update(extra_evaluators)
    fn = evaluators.get(definition.condition_type)
    if fn is None:
        return RuleResult(False, state,
                          reason=f"没有 {definition.condition_type!r} 这种规则")

    outcome = fn(definition, state, current, ctx)
    lifecycle = definition.lifecycle

    # 无论触发与否,前值都要推进 —— 这是 crossing / changed / delta 的前提。
    #
    # 但如果 evaluator 自己动了 previous_value(自定义 evaluator 往往要维护
    # 自己的派生状态),就尊重它的版本 —— 以前无条件覆盖,导致任何需要自己
    # 管状态的 evaluator 都没法工作。
    evaluator_moved_previous = outcome.state.previous_value != state.previous_value
    if evaluator_moved_previous:
        next_previous = outcome.state.previous_value
    elif definition.condition_type == "streak":
        next_previous = outcome.current      # streak 的"前值"是连续长度,不是观测值
    else:
        next_previous = current
    advanced = RuleState(
        previous_value=next_previous,
        fired_in_scope=state.fired_in_scope,
        last_fired_at=state.last_fired_at,
        seen_keys=outcome.state.seen_keys,
    )

    if not outcome.fired:
        return RuleResult(False, advanced, outcome.previous, outcome.current,
                          outcome.reason)

    # 冷却和 fire 模式是两件事。以前只在 fire=once 时检查冷却,
    # 于是 fire=every + rearm=cooldown 完全没有冷却 —— 配了个不生效的东西。
    if (lifecycle.rearm == "cooldown"
            and not _cooled_down(state, now=now,
                                 cooldown=lifecycle.cooldown_seconds)):
        return RuleResult(False, advanced, outcome.previous, outcome.current,
                          "还在冷却中")
    if (lifecycle.fire == "once" and state.fired_in_scope
            and lifecycle.rearm != "cooldown"):
        return RuleResult(False, advanced, outcome.previous, outcome.current,
                          "这个范围内已经触发过了")

    fired_state = RuleState(
        previous_value=advanced.previous_value,
        fired_in_scope=True,
        last_fired_at=now.isoformat(),
        seen_keys=advanced.seen_keys,
    )
    return RuleResult(True, fired_state, outcome.previous, outcome.current,
                      outcome.reason)


__all__ = ["evaluate", "scope_key"]
