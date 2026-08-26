"""能力表的内部一致性:每个信号都要有一个已声明的能力可归属。

原测试文件另有一条 ``test_io_shell_reexports_the_same_objects``,断言宿主的
re-export 壳和内核对象是同一批对象——那是宿主集成测试,不属于这个包,
迁移时未带过来（宿主侧应在自己仓库里保留一份等价断言）。
"""
from __future__ import annotations

import perceptkit.catalog as catalog


def test_capability_and_signal_counts_match_baseline():
    # 基线值：21 个能力、20 个信号（迁移时的快照）。
    # 这两个数字变了就是加/删了能力——变更应该是有意为之，不是意外漂移。
    assert len(catalog.CAPABILITIES) == 21
    assert len(catalog.SIGNALS) == 20


def test_every_signal_points_at_a_declared_capability():
    for signal in catalog.SIGNALS.values():
        assert signal.capability in catalog.CAPABILITIES, signal.input
