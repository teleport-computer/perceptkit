"""Wake adapter 的一致性检查 —— 宿主用它证明自己的 ``WakePort`` 实现是对的。

产品规范 §20 把 storage / wake / report 三种 adapter conformance 并列为最低
交付物。storage 那一套早就有了；这是 wake 那一套。

用法（在宿主自己的测试里）::

    from perceptkit.conformance import run_wake_conformance

    def test_my_runtime_adapter_is_conformant():
        problems = run_wake_conformance(lambda: MyRuntimeWake(fake_queue()))
        assert not problems, "\\n".join(problems)

---

## 为什么 wake 需要单独一套

storage 错了通常会崩或者查不到；**wake 错了只会让用户被同一件事提醒两次**，
或者一条提醒永远不到 —— 两种都不报错，都只有用户会发现。

三件最容易做错的事，这套检查全都盯着：

    幂等      崩溃重投是常态不是异常。投出去之后、回执存下来之前进程挂掉，
              重启一定会再投一次。runtime 不按 event_id 幂等，用户就被提醒两次
    别抛异常  "runtime 拒绝"是一种**正常应答**，用 rejected / suppressed 表达。
              抛异常会被当成投递失败，于是无限重试一条对方明确不想要的事件
    回执对得上 回执里的 event_id / attempt_id 必须原样回传。对不上的话，
              调用方就无法判断这条回执说的是哪一次投递
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..contracts.delivery import DeliveryAttempt
from ..contracts.event import EventCondition, PerceptionEvent
from ..contracts.receipt import WAKE_STATUSES, WakeReceipt

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)

WakeFactory = Callable[[], Any]

#: 这套检查覆盖的保证。宿主读这份清单就知道自己被要求了什么。
WAKE_GUARANTEES: tuple[str, ...] = (
    "W1 回执的 status 必须是协议里的那几个之一",
    "W2 回执必须原样回传 event_id 和 attempt_id",
    "W3 同一个 event_id 重投必须是幂等的（第二次返回 duplicate，不重复处理）",
    "W4 拒绝和抑制要用回执表达，不能抛异常",
    "W5 attempt_id 变了但 event_id 没变，仍然算同一件事",
    "W6 回执的 received_at 必须是带时区的时间",
)

#: 这套检查**证明不了**的事。写下来免得被当成验过了。
WAKE_NOT_PROVABLE: tuple[str, ...] = (
    "真实的超时行为 —— 要宿主自己让 runtime 卡住，确认返回 enqueue_failed "
    "而不是永远挂着",
    "真正的并发投递 —— 两条连接同时投同一个 event_id，只能有一个真正处理",
    "跨进程幂等 —— 内存里的 seen 集合重启就没了，真实实现要落库",
)


def _event(event_id: str = "evt_1") -> PerceptionEvent:
    return PerceptionEvent(
        event_id=event_id, definition_id="d1", definition_version=1,
        subject_id="u1", type="activity.step_goal_reached", signal="steps",
        occurred_at=T0, received_at=T0,
        condition=EventCondition(type="threshold_crossing", operator="gte", value=3000),
        field_name="step_count", previous=2999, current=3000,
    )


def _attempt(event_id: str = "evt_1", attempt_id: str = "att_1",
             count: int = 1) -> DeliveryAttempt:
    return DeliveryAttempt(
        event_id=event_id, attempt_id=attempt_id, attempt_number=count,
    )


def _call(wake: Any, event: PerceptionEvent, attempt: DeliveryAttempt,
          problems: list[str], label: str) -> WakeReceipt | None:
    try:
        return wake.wake(event, attempt)
    except Exception as exc:                       # noqa: BLE001 — 这正是要抓的
        problems.append(
            f"W4 {label}：wake() 抛了 {type(exc).__name__}({exc})。"
            "拒绝和抑制是正常应答，要用 rejected / conversation_suppressed 回执表达 —— "
            "抛异常会被调用方当成投递失败，于是无限重试一条对方明确不想要的事件"
        )
        return None


def run_wake_conformance(new: WakeFactory) -> list[str]:
    """跑一遍 wake adapter 的一致性检查，返回问题清单（空 = 通过）。

    ``new`` 每次调用要给一个**全新的**适配器实例 —— 检查之间不能共享状态，
    否则 W3 的幂等检查会被上一轮的痕迹影响。
    """
    problems: list[str] = []

    # -- W1 / W2 / W6：回执本身的形状 -----------------------------------
    wake = new()
    ev, att = _event(), _attempt()
    receipt = _call(wake, ev, att, problems, "首次投递")
    if receipt is not None:
        if receipt.status not in WAKE_STATUSES:
            problems.append(
                f"W1 回执 status={receipt.status!r} 不在协议里。"
                f"合法值：{sorted(WAKE_STATUSES)}"
            )
        if receipt.event_id != ev.event_id:
            problems.append(
                f"W2 回执的 event_id 是 {receipt.event_id!r}，投的是 {ev.event_id!r}。"
                "对不上的话调用方无法判断这条回执说的是哪一次投递"
            )
        if receipt.attempt_id != att.attempt_id:
            problems.append(
                f"W2 回执的 attempt_id 是 {receipt.attempt_id!r}，"
                f"投的是 {att.attempt_id!r}"
            )
        if receipt.received_at.tzinfo is None:
            problems.append(
                "W6 回执的 received_at 没有时区。裸时间在跨时区宿主上会被解释错，"
                "而且错得不会报错"
            )

    # -- W3 / W5：幂等 ---------------------------------------------------
    wake = new()
    ev = _event("evt_idem")
    first = _call(wake, ev, _attempt("evt_idem", "att_1", 1), problems, "幂等第一次")
    # 同一个 event_id、不同的 attempt_id —— 这正是崩溃重投的形状
    second = _call(wake, ev, _attempt("evt_idem", "att_2", 2), problems, "幂等重投")
    if first is not None and second is not None:
        if second.status != "duplicate":
            problems.append(
                f"W3 同一个 event_id 投第二次返回了 {second.status!r}，应该是 'duplicate'。"
                "崩溃重投是常态：投出去之后、回执存下来之前进程挂掉，重启一定会再投一次。"
                "不幂等的话用户会被同一件事提醒两次"
            )
        if second.event_id != ev.event_id:
            problems.append("W5 重投的回执 event_id 变了 —— 它跨重试必须保持不变")
        if second.attempt_id != "att_2":
            problems.append(
                "W5 重投的回执 attempt_id 没跟上这一次的尝试。"
                "event_id 认的是「哪件事」，attempt_id 认的是「哪一次投递」，两个都要对"
            )

    return problems


__all__ = ["WAKE_GUARANTEES", "WAKE_NOT_PROVABLE", "run_wake_conformance"]
