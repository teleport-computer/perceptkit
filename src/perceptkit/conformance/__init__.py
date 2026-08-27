"""一致性测试工具。

    memory  内存版 StoragePort —— **只是测试工具,不是生产实现**

🔴 内存实现天然原子、天然没有并发,所以它**验不出**真实数据库的事务边界和
隔离级别。"RuleState 和 Outbox 必须同事务"这类保证在这里永远是绿的。
宿主必须另外用真实数据库 + 两条独立连接 + 在关键写操作之间打断点来证明。
"""
from __future__ import annotations

from .memory import InMemoryStorage

__all__ = ["InMemoryStorage"]
