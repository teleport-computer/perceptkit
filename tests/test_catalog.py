"""能力表的内部一致性:每个信号都要有一个已声明的能力可归属。

原测试文件另有一条 ``test_io_shell_reexports_the_same_objects``,断言宿主的
re-export 壳和内核对象是同一批对象——那是宿主集成测试,不属于这个包,
迁移时未带过来（宿主侧应在自己仓库里保留一份等价断言）。
"""
from __future__ import annotations

import perceptkit.catalog as catalog


def test_capability_and_signal_counts_match_baseline():
    # 基线值：33 个能力、20 个信号。
    # 这两个数字变了就是加/删了能力——变更应该是有意为之，不是意外漂移。
    #
    # 两个数字为什么不相等：能力里有 12 个是 2026-09-06 拆出来的**单指标
    # 查询档**（血氧、HRV…），而它们不是独立的上报键 —— 客户端仍然按
    # HealthKit 的授权分组一次报一整包。信号数没跟着涨，正是因为上报契约
    # 没变。
    assert len(catalog.CAPABILITIES) == 33
    assert len(catalog.SIGNALS) == 20


def test_every_signal_points_at_a_declared_capability():
    for signal in catalog.SIGNALS.values():
        assert signal.capability in catalog.CAPABILITIES, signal.input


# ---------------------------------------------------------------------------
# 上报契约 vs 存储信号 —— 两张表，别拿一张改另一张
# ---------------------------------------------------------------------------

def test_the_report_keys_ios_actually_sends_are_all_in_the_catalog():
    """SIGNALS 是按 **iOS 上报键**建索引的，不是按存储信号名。

    2026-09-06 把存储侧的健康信号拆成单指标时，顺手把这张表的键也改成了
    拆完的名字。后果：客户端照旧发 `health_vitals`，宿主查表查不到，
    整包判 unknown_signal 退回 —— 返回的还是 200，客户端不会重试，
    用户只会发现体征、体重、血压从某天起再也没更新过。

    存储侧怎么拆是 manifest 的事，跟这张表无关。
    """
    from perceptkit.catalog import SIGNALS
    for key in ("health_vitals", "health_body", "health_metabolic"):
        assert key in SIGNALS, f"iOS 发的 {key} 在上报表里查不到"


def test_step_count_is_still_an_accepted_report_field():
    """步数在存储侧升成了独立信号，但它仍然是**装在体征包里**送上来的。

    从上报表里漏掉它，等于宿主不再接受这个字段。
    """
    from perceptkit.catalog import SIGNALS
    assert "step_count" in SIGNALS["health_vitals"].outputs
