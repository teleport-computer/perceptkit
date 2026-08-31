"""唤醒端口 —— 把事件交给宿主的 agent runtime。

只有一个方法。它刻意很窄:kit 不知道也不该知道宿主是用队列、消息、
还是直接函数调用把事件送到 runtime 的。

**戳醒 ≠ 该开口。** runtime 收下这次唤醒之后,继续睡、只看一眼、还是开口
说话,是三个平行选项 —— 这个包不参与那个决定,也不产出任何"该说话了"式的
措辞。回执里的 ``accepted`` 只表示"我收到了并且会处理",不表示"我会说话"。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..contracts.delivery import DeliveryAttempt
from ..contracts.event import PerceptionEvent
from ..contracts.receipt import WakeReceipt


@runtime_checkable
class WakePort(Protocol):
    """宿主的 runtime 适配器。"""

    def wake(self, event: PerceptionEvent, attempt: DeliveryAttempt) -> WakeReceipt:
        """把一个事件交给 runtime,返回它的应答。

        **实现必须按 ``event.event_id`` 幂等。** 崩溃重投是常态不是异常:
        投出去之后、回执存下来之前进程挂掉,重启后一定会再投一次。
        runtime 认得这个 id 就返回 ``duplicate``,不要真的再处理一遍 ——
        否则用户会被同一件事提醒两次。

        ``attempt`` 带着"这是第几次"。``event_id`` 跨重试不变,
        ``attempt_id`` 每次都变 —— 后者用来在日志里分辨"第三次重试失败"
        和"三个并发投递失败"。

        **不要在这里抛异常表示"runtime 拒绝"** —— 拒绝是一种正常应答,
        用 ``rejected`` / ``conversation_suppressed`` 表达。异常留给
        真正的意外(连接断了、序列化失败),调用方会把它当作
        ``enqueue_failed`` 处理并安排重试。
        """
        ...


__all__ = ["WakePort"]
