"""端口 —— 宿主要实现的两个接口。

    StoragePort   数据落到哪儿、怎么保证一致
    WakePort      事件交给谁

**端口只定行为,不定实现。** 宿主填的每个方法都是孤立的一件事;
"先落地再投递""迟到数据不覆盖当前值"这些顺序和规则在 kit 的处理管线里,
宿主没有那个入口 —— 让写错的那条路根本不存在,比在文档里提醒别写错可靠。

这两个 Protocol 都是 ``runtime_checkable`` 的,宿主可以用 ``isinstance``
自查有没有漏方法;但真正的验收是跑 ``perceptkit.conformance`` 那套测试 ——
方法签名对得上不代表语义对得上。
"""
from __future__ import annotations

from .storage import StoragePort
from .wake import WakePort

__all__ = ["StoragePort", "WakePort"]
