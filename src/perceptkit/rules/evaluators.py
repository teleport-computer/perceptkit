"""九种内置规则。

每个 evaluator 只回答一件事：**这次观测，让这条规则命中了吗。**
"今天已经触发过了要不要再触发""明天要不要重新武装"是生命周期的事，
在 ``engine`` 里统一处理 —— 不然九个 evaluator 各写一遍，迟早不一致。

九种：

    changed             值变了
    equals              值等于某个东西
    enters / leaves     进入 / 离开某个状态
    threshold_crossing  跨过一条线（**不是"大于某个数"**，见下）
    delta               相邻两次的变化量超过某个幅度
    occurrence          这条观测到达本身就是事件
    streak              连续 N 个周期满足条件
    absence             该来的没来
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from .types import MAX_SEEN_KEYS, EventDefinition, RuleResult, RuleState


@runtime_checkable
class RuleEvaluator(Protocol):
    """自定义 evaluator 的接口。

    宿主可以注册代码型 evaluator 处理内置模板覆盖不了的逻辑。
    **但普通用户配置只能用声明式模板** —— 用户配的规则跑在服务端，
    允许任意代码就是一整类新的安全面。
    """

    kind: str

    def evaluate(
        self, definition: EventDefinition, state: RuleState,
        current: Any, context: dict[str, Any],
    ) -> RuleResult:
        ...


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _hit(state: RuleState, previous: Any, current: Any, reason: str) -> RuleResult:
    return RuleResult(True, state, previous=previous, current=current, reason=reason)


def _miss(state: RuleState, previous: Any, current: Any, reason: str) -> RuleResult:
    return RuleResult(False, state, previous=previous, current=current, reason=reason)


# ---------------------------------------------------------------------------

def eval_changed(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    prev = s.previous_value
    if prev is None and current is None:
        return _miss(s, prev, current, "两次都没有值")
    if prev == current:
        return _miss(s, prev, current, "没变")
    # 第一次见到某个值不算"变了" —— 否则用户刚装上 app，所有信号会一起触发。
    if prev is None:
        return _miss(s, prev, current, "第一次观测，不算变化")
    return _hit(s, prev, current, f"{prev!r} -> {current!r}")


def eval_equals(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    if current == d.value:
        return _hit(s, s.previous_value, current, f"等于 {d.value!r}")
    return _miss(s, s.previous_value, current, f"不等于 {d.value!r}")


def eval_enters(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """从"不是它"变成"是它"。**只在跨入那一刻触发**，之后一直是它也不再触发。"""
    prev = s.previous_value
    if current == d.value and prev != d.value:
        return _hit(s, prev, current, f"进入 {d.value!r}")
    return _miss(s, prev, current, "没有跨入")


def eval_leaves(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    prev = s.previous_value
    if prev == d.value and current != d.value:
        return _hit(s, prev, current, f"离开 {d.value!r}")
    return _miss(s, prev, current, "没有跨出")


_OPS: dict[str, Callable[[float, float], bool]] = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
}


def eval_threshold_crossing(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """**跨过**一条线，不是"在线的那一边"。

    这是最容易写错的一条：``current >= 3000`` 会让 3001、3010、3100 的每次
    上报都重复触发 —— 用户走一天路能被提醒几十次。正确的是
    ``previous < 3000 and current >= 3000``。

    第一次观测就已经在线的另一边时**不触发**：用户可能是中午才装上 app，
    那时步数已经过万，不该立刻收到"你走够 3000 步了"。
    """
    op = _OPS.get(d.operator or "gte")
    threshold = _numeric(d.value)
    now = _numeric(current)
    prev = _numeric(s.previous_value)
    if op is None or threshold is None or now is None:
        return _miss(s, s.previous_value, current, "阈值或当前值不是数字")
    if prev is None:
        return _miss(s, s.previous_value, current, "第一次观测，没有可比的前值")
    if op(prev, threshold):
        return _miss(s, s.previous_value, current, "之前就已经在线的另一边")
    if op(now, threshold):
        return _hit(s, s.previous_value, current,
                    f"{prev:g} -> {now:g} 跨过 {threshold:g}")
    return _miss(s, s.previous_value, current, "还没跨过")


def eval_delta(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """相邻两次的变化幅度超过某个值。用于"突然掉了很多"这类。"""
    now, prev, limit = _numeric(current), _numeric(s.previous_value), _numeric(d.value)
    if now is None or prev is None or limit is None:
        return _miss(s, s.previous_value, current, "缺少可比的数字")
    change = now - prev
    magnitude = abs(change)
    if magnitude >= abs(limit):
        return _hit(s, s.previous_value, current, f"变化 {change:+g}，超过 {limit:g}")
    return _miss(s, s.previous_value, current, f"变化 {change:+g}，未达 {limit:g}")


def eval_occurrence(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """观测到达本身就是事件。没有前后值可比，靠去重键挡住重复。

    去重键取自 ``ctx``（通常是 ``source_event_id``）。**没有去重键时不触发** ——
    宁可漏一次，也不要因为客户端重传而让用户被同一件事提醒两次。
    """
    key = ctx.get(d.dedupe_field)
    if not key:
        return _miss(s, None, current, f"没有 {d.dedupe_field}，无法去重，跳过")
    if key in s.seen_keys:
        return _miss(s, None, current, "这次事件之前处理过")
    # 有上限：高频信号会让这条状态记录无限膨胀，而它每次求值都要被读出来。
    seen = (s.seen_keys + (key,))[-MAX_SEEN_KEYS:]
    return _hit(RuleState(
        previous_value=s.previous_value, fired_in_scope=s.fired_in_scope,
        last_fired_at=s.last_fired_at, seen_keys=seen,
    ), None, current, f"发生了（{d.dedupe_field}={key}）")


def eval_streak(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """连续 N 个周期满足条件。

    两个参数分开：``operator`` / ``value`` 是**每天的条件**（"睡眠 < 360 分钟"），
    ``params["periods"]`` 是**连续几天**。挤在一个字段里表达不了。

    连续长度由调用方通过 ``ctx["streak_length"]`` 给 —— 它要读历史，
    而 evaluator 只看单次观测。

    **由日聚合完成时驱动，不是每条观测都跑**：前台每 30 秒一条观测，
    但"连续三天"这件事一天只可能变化一次。

    **边缘触发**：只在恰好跨到 N 的那一次触发。第 N+1 天仍然连续，
    但不再提醒 —— 否则"连续三天睡不好"会变成天天念叨。
    """
    need = int(_numeric(d.params.get("periods")) or _numeric(d.value) or 0)
    length = int(_numeric(ctx.get("streak_length")) or 0)
    prev_length = int(_numeric(s.previous_value) or 0)
    if need <= 0:
        return _miss(s, prev_length, length, "没有指定连续长度")
    if length >= need > prev_length:
        return _hit(s, prev_length, length, f"连续到达 {length} 个周期")
    return _miss(s, prev_length, length, f"连续 {length}，需要 {need}")


def eval_absence(d: EventDefinition, s: RuleState, current: Any, ctx) -> RuleResult:
    """该来的没来。

    多久算"没来"由 ``ctx["silent_seconds"]`` 给 —— 同样要读历史。
    这条规则天然由**定时检查**驱动，而不是由上报驱动：没有上报的时候，
    才是它该触发的时候。
    """
    limit = _numeric(d.value)
    silent = _numeric(ctx.get("silent_seconds"))
    if limit is None or silent is None:
        return _miss(s, None, current, "缺少静默时长")
    if silent >= limit and not s.fired_in_scope:
        return _hit(s, None, current, f"已经 {silent:g}s 没有数据，超过 {limit:g}s")
    return _miss(s, None, current, f"静默 {silent:g}s，未达 {limit:g}s")


BUILTIN: dict[str, Callable[..., RuleResult]] = {
    "changed": eval_changed,
    "equals": eval_equals,
    "enters": eval_enters,
    "leaves": eval_leaves,
    "threshold_crossing": eval_threshold_crossing,
    "delta": eval_delta,
    "occurrence": eval_occurrence,
    "streak": eval_streak,
    "absence": eval_absence,
}


__all__ = ["RuleEvaluator", "BUILTIN"] + [f"eval_{k}" for k in BUILTIN]
