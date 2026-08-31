"""投递状态机 —— 事件从落地到送达之间会经过什么。

**为什么状态机必须由 kit 定义，而不是留给每个宿主。**

投递这件事本身（写哪个队列、起几个 worker、用什么定时器）确实是宿主的。
但"什么时候算投出去了""崩在中间怎么恢复""重试会不会重复打扰用户"，
是**正确性问题**——每个宿主自己发挥的结果是：

    A 宿主 先投再落地           → 崩了丢事件
    B 宿主 落地了但不重投        → 崩了事件永远卡住
    C 宿主 重投但没去重          → 用户被同一件事提醒三次
    D 宿主 投出去就扣冷却额度    → runtime 拒了，额度白扣，那轮该说的话没说

同一个 kit 装到四个宿主上，四种可靠性 —— "可插拔"就是假的。所以状态和
转移规则在这里定死，宿主只实现"怎么把状态存下来、怎么把 worker 跑起来"。

**冷却额度为什么要先占位再兑现**（这条产品规范里没有）。规范只说
"accepted 之后提交冷却/额度"，但没说 accepted **之前**那段窗口怎么办：
两个 worker 同时捞到同一个事件、都还没拿到 accepted、于是都不扣额度、
于是都投出去了。所以 ``claimed`` 状态必须持有一个**可过期的占位**，
accepted 时兑现成真正的消耗，其余情况释放。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

#: 已经落地，等人来捞。**事件走到这个状态才算不会丢。**
PENDING = "pending"

#: 某个 worker 领走了，持有一个带到期时间的租约和一个冷却额度占位。
#: 租约到期没有进展 → 回到 ``pending`` 让别人接手（原持有者可能已经死了）。
CLAIMED = "claimed"

#: 终态：runtime 收下了。**只有这个状态会把额度占位兑现成真正的消耗。**
DELIVERED = "delivered"

#: 终态：runtime 收到了但选择不响应（会话正忙、安静时段）。
#: **这不是失败**，事件已经送达 —— 重投只会打扰第二次。额度占位释放。
SUPPRESSED = "suppressed"

#: 终态：runtime 明确拒绝（事件类型不认、subject 不属于它）。不重试。
REJECTED = "rejected"

#: 终态：重试次数用尽。留着给人看，不再自动投。
DEAD_LETTER = "dead_letter"

#: 终态：规则说了这条不唤醒。**事件仍然是事实、仍然落地**，只是不投。
#: 和 ``suppressed`` 不同 —— 那是 runtime 收到之后自己选择不响应，
#: 这个是压根没打算投。两者混用会让"到底送没送到"说不清。
NOT_DISPATCHED = "not_dispatched"

DELIVERY_STATES: frozenset[str] = frozenset({
    PENDING, CLAIMED, DELIVERED, SUPPRESSED, REJECTED, DEAD_LETTER, NOT_DISPATCHED,
})

#: 终态：不会再变，也不再占用额度占位。
TERMINAL_STATES: frozenset[str] = frozenset({
    DELIVERED, SUPPRESSED, REJECTED, DEAD_LETTER, NOT_DISPATCHED,
})

#: 合法的状态转移。任何不在这里的转移都是 bug，不是"边界情况"。
_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({CLAIMED}),
    # claimed → pending 有两条路：主动放回（投递失败，等下次重试），
    # 或租约到期被别人接管。两条都合法。
    CLAIMED: frozenset({PENDING, DELIVERED, SUPPRESSED, REJECTED, DEAD_LETTER}),
    DELIVERED: frozenset(),
    SUPPRESSED: frozenset(),
    REJECTED: frozenset(),
    DEAD_LETTER: frozenset(),
    NOT_DISPATCHED: frozenset(),
}


class IllegalTransition(ValueError):
    """试图做一个不合法的状态转移。

    这类错误不该被 catch 掉当边界情况处理 —— 它意味着投递逻辑有 bug，
    继续往下走只会让状态更乱。
    """


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    if current not in DELIVERY_STATES:
        raise IllegalTransition(f"未知状态 {current!r}")
    if not can_transition(current, target):
        allowed = sorted(_TRANSITIONS.get(current, frozenset()))
        raise IllegalTransition(
            f"{current} -> {target} 不合法；从 {current} 只能走到 {allowed or ['(终态)']}"
        )


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# 回执 -> 状态
# ---------------------------------------------------------------------------

def next_state_for_receipt(status: str, *, attempts_left: bool) -> str:
    """runtime 给了这个回执之后，事件该进哪个状态。

    ``attempts_left`` 为假时，本该重试的转成 ``dead_letter`` —— 无限重试
    会让一个投不出去的事件永远占着 worker。
    """
    from .receipt import (
        WAKE_ACCEPTED, WAKE_DUPLICATE, WAKE_ENQUEUE_FAILED,
        WAKE_REJECTED, WAKE_SUPPRESSED,
    )
    if status in (WAKE_ACCEPTED, WAKE_DUPLICATE):
        # duplicate 也算送达：runtime 认得这个 event_id，说明之前那次其实成了，
        # 只是回执没存下来。当成功处理，别再投第三次。
        return DELIVERED
    if status == WAKE_SUPPRESSED:
        return SUPPRESSED
    if status == WAKE_REJECTED:
        return REJECTED
    if status == WAKE_ENQUEUE_FAILED:
        return PENDING if attempts_left else DEAD_LETTER
    raise IllegalTransition(f"没有为回执状态 {status!r} 定义后续状态")


def consumes_budget(state: str) -> bool:
    """走到这个状态时，冷却额度占位该不该兑现成真正的消耗。

    **只有 ``delivered``。** 被压制、被拒绝、进死信都要把占位释放掉 ——
    用户那一轮该说的话没说出去、额度却被吃掉了，是最难查的一类问题：
    没有报错、没有日志，只有"它今天怎么不说话"。
    """
    return state == DELIVERED


@dataclass(frozen=True)
class DeliveryAttempt:
    """一次投递尝试的身份。

    ``event_id`` 跨重试**保持不变** —— runtime 靠它幂等，变了就等于
    每次重试都是一个新事件，用户会被重复打扰。
    ``attempt_id`` 每次都变 —— 用来把回执对上具体是哪一次投递，
    以及在日志里分辨"第三次重试失败"和"三个并发投递失败"。
    """

    event_id: str
    attempt_id: str
    attempt_number: int

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("attempt_number 从 1 开始")


__all__ = [
    "PENDING", "CLAIMED", "DELIVERED", "SUPPRESSED", "REJECTED", "DEAD_LETTER",
    "NOT_DISPATCHED",
    "DELIVERY_STATES", "TERMINAL_STATES",
    "IllegalTransition", "can_transition", "assert_transition", "is_terminal",
    "next_state_for_receipt", "consumes_budget", "DeliveryAttempt",
]
