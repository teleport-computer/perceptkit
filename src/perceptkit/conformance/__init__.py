"""一致性测试工具。

    memory  内存版 StoragePort —— **只是测试工具,不是生产实现**
    suite   十条一致性保证的检查。宿主用它证明自己的 adapter 是对的

🔴 内存实现天然原子、天然没有并发,所以它**验不出**真实数据库的事务边界和
隔离级别。"RuleState 和 Outbox 必须同事务"这类保证在这里永远是绿的。
宿主必须另外用真实数据库 + 两条独立连接 + 在关键写操作之间打断点来证明。
"""
from __future__ import annotations

from .memory import InMemoryStorage
from .suite import GUARANTEES, NOT_PROVABLE_IN_MEMORY, run_storage_conformance

__all__ = [
    "InMemoryStorage",
    "run_storage_conformance", "GUARANTEES", "NOT_PROVABLE_IN_MEMORY",
]
