"""一致性测试工具。

    memory  内存版 StoragePort —— **只是测试工具,不是生产实现**
    suite   storage adapter：十条一致性保证
    wake    wake adapter：回执形状与幂等
    report  report adapter：producer 产出的信封本身对不对

产品规范 §20 把这三种 adapter conformance 并列为最低交付物。

🔴 内存实现天然原子、天然没有并发,所以它**验不出**真实数据库的事务边界和
隔离级别。"RuleState 和 Outbox 必须同事务"这类保证在这里永远是绿的。
宿主必须另外用真实数据库 + 两条独立连接 + 在关键写操作之间打断点来证明。
"""
from __future__ import annotations

from .memory import InMemoryStorage
from .suite import GUARANTEES, NOT_PROVABLE_IN_MEMORY, run_storage_conformance
from .report import (
    REPORT_GUARANTEES, REPORT_NOT_PROVABLE, run_report_conformance,
)
from .wake import WAKE_GUARANTEES, WAKE_NOT_PROVABLE, run_wake_conformance

__all__ = [
    "InMemoryStorage",
    "run_storage_conformance", "GUARANTEES", "NOT_PROVABLE_IN_MEMORY",
    "run_wake_conformance", "WAKE_GUARANTEES", "WAKE_NOT_PROVABLE",
    "run_report_conformance", "REPORT_GUARANTEES", "REPORT_NOT_PROVABLE",
]
