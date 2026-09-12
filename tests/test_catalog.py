"""能力表的内部一致性:每个信号都要有一个已声明的能力可归属。

原测试文件另有一条 ``test_io_shell_reexports_the_same_objects``,断言宿主的
re-export 壳和内核对象是同一批对象——那是宿主集成测试,不属于这个包,
迁移时未带过来（宿主侧应在自己仓库里保留一份等价断言）。
"""
from __future__ import annotations

import perceptkit.catalog as catalog
import perceptkit.routing as routing


def test_capability_and_signal_counts_match_baseline():
    # 基线值：34 个能力、20 个信号。
    # 这两个数字变了就是加/删了能力——变更应该是有意为之，不是意外漂移。
    #
    # 两个数字为什么不相等：能力里有 13 个是 2026-09-06 拆出来的**单指标
    # 查询档**（血氧、HRV、步数…），而它们不是独立的上报键 —— 客户端仍然按
    # HealthKit 的授权分组一次报一整包。信号数没跟着涨，正是因为上报契约
    # 没变。
    assert len(catalog.CAPABILITIES) == 34
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


# ---------------------------------------------------------------------------
# 报告闸 vs 查询档 —— 拆了一半的后果（2026-09-12）
# ---------------------------------------------------------------------------
#
# 2026-09-06 把健康信号拆成单指标时，能力表分成了两种角色：
#
#     报告闸   客户端按 HealthKit 授权分组一次报一整包，闸按包来
#              health_vitals / health_body / health_metabolic，故意不带 query_tool
#     查询档   agent 按**单个指标**问，所以按指标各占一行，query_tool=True
#
# 但 `Signal.capability` 仍然只指着报告闸，拆出去的 12 条查询档**没有任何
# 信号指向它们**。后果：宿主照老规矩「查这个信号的能力，看它能不能查询」
# 拿到的是报告闸（没有 query_tool）→ 整包体征字段一个都进不了 agent 快照。
# 数据照收照存，就是出不来，全程不报错。
#
# 下面两条守卫对着的就是这个形状，不是对着这一次的具体字段。

def test_every_reportable_field_can_reach_the_agent():
    """G-a：每个上报字段都必须有一条能到 agent 的路。

    「存得进、查不出」是这个库最贵的一类 bug —— 它不报错、测试全绿、
    字段都在，只是数据永远出不来。凡是收下来的字段，要么能进快照
    （context_field），要么能被查询（query_tool），二者必居其一；
    都不能的字段就是只写不读，那它不该出现在上报契约里。
    """
    reachable = set(routing.snapshot_fields(include_query_tools=True))
    unreachable = {
        f
        for sig in catalog.SIGNALS.values()
        for f in sig.outputs
        if f != "user_state" and f not in reachable
    }
    assert not unreachable, f"这些字段收得下、但永远到不了 agent：{sorted(unreachable)}"


def test_every_declared_capability_is_actually_wired():
    """G-b：声明了的能力必须有人指向它，否则就是死声明。

    死声明比缺声明更坏：能力表里看得见，透明度界面上也列得出来，
    用户以为这项能力在工作 —— 而实际上没有任何字段走它。
    2026-09-06 拆出来的 12 条查询档就在这个状态里待了六天。

    从三条路一起数 —— 不是每个能力都由 `signals` 上报触达：
    照片走自己的入库通道（manifest 里的 photo_library_added），
    历史事件按 kind 归属（KIND_CAPABILITY）。漏掉这两条会把正常的能力
    误报成死声明。
    """
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    wired = {sig.capability for sig in catalog.SIGNALS.values()}
    wired |= {r.query_capability for r in routing.ROUTES.values()}
    wired |= set(catalog.KIND_CAPABILITY.values())
    wired |= {d.capability for d in MINIMAL_SIGNALS.values()}
    dangling = set(catalog.CAPABILITIES) - wired
    assert not dangling, f"这些能力声明了但没有任何字段指向它：{sorted(dangling)}"


def test_heart_rate_is_queryable_even_though_its_report_gate_is_not():
    """这一次的具体回归：整包闸不可查，不代表包里的指标不可查。

    宿主不许自己拼「查信号的能力、看 query_tool」——那条规则在拆分之后
    就错了。正确的入口是 snapshot_fields()。
    """
    assert not catalog.CAPABILITIES["health_vitals"].query_tool  # 报告闸，本来就不该可查
    assert catalog.CAPABILITIES["health_current_hr"].query_tool  # 查询档在这
    assert "current_heart_rate" in routing.snapshot_fields(include_query_tools=True)
    # 而它不该出现在「便宜的唤醒快照」里：心率每次心跳都在变。
    assert "current_heart_rate" not in routing.snapshot_fields(include_query_tools=False)
