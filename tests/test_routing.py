"""路由图的守卫 —— 对着「拆了一半」这个形状，不是对着某几个字段。

2026-09-06 把健康能力拆成「报告闸（整包上报）」和「查询档（单指标）」，
拆了存储侧没拆上报侧：12 条查询档声明了、没有任何信号指向它们，而宿主
照老规矩「查这个信号的能力、看它能不能被查询」拿到的是报告闸。后果是
心率、血氧、体重、血糖**一个都到不了 agent** —— 数据照收照存、接口 200、
测试全绿，六天没人发现。

下面每一条都能单独抓住那次的形状。
"""
from __future__ import annotations

import perceptkit.catalog as catalog
import perceptkit.fields as fields
import perceptkit.routing as routing


def _manifest():
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    return MINIMAL_SIGNALS


# ---------------------------------------------------------------------------
# 按角色查，不是按"有没有人引用过"
# ---------------------------------------------------------------------------

def test_every_query_capability_actually_gates_some_field():
    """标了"可查询"的能力，必须真有字段从它这儿查。

    只查「有没有人引用过」是不够的 —— 一个查询档**只被当报告闸引用**
    也能过，而查询仍然是死的。那正是 2026-09-06 的形状本身：
    12 条查询档在能力表里躺着，透明度界面列得出来，用户以为这项能力在
    工作，实际没有任何字段走它。
    """
    gating = {r.query_capability for r in routing.ROUTES.values()}
    # 少数信号不走 `signals` 上报，有自己的入库通道，所以不会出现在这张
    # 按上报字段建的路由图里。**只豁免这一类，逐个列出**——整张 manifest
    # 一起 OR 进来就等于把这条守卫关掉了：一个查询档只要被某个信号引用过
    # 就算数，而"被引用过"正是上次漏掉这个 bug 的判据。
    non_report_ingest = {
        d.capability
        for k, d in _manifest().items()
        if k not in catalog.SIGNALS and d.capability not in {
            s.capability for s in catalog.SIGNALS.values()
        }
    }
    declared = {k for k, c in catalog.CAPABILITIES.items() if c.query_tool}
    dead = declared - gating - non_report_ingest
    assert not dead, f"这些能力标了可查询，但没有任何字段从它查：{sorted(dead)}"


def test_every_context_capability_actually_projects_some_field():
    """标了"常驻上下文"的能力，必须真有字段进便宜快照。"""
    cheap = set(routing.snapshot_fields(include_query_tools=False))
    projecting = {
        r.query_capability for r in routing.ROUTES.values() if r.report_field in cheap
    }
    declared = {k for k, c in catalog.CAPABILITIES.items() if c.context_field}
    dead = declared - projecting
    assert not dead, f"这些能力标了常驻上下文，但没有字段进快照：{sorted(dead)}"


def test_report_gates_gate_reports_and_nothing_else():
    """报告闸只管上报，不该有 query_tool。

    反过来说：一个既是报告闸、又标了可查询的能力，就是把两种角色又混回去了。
    """
    for key in sorted(routing.pack_gates()):
        assert not catalog.CAPABILITIES[key].query_tool, (
            f"{key} 是整包的报告闸，它下面的指标已经各有查询档了，"
            "它自己不该再标 query_tool —— 那是把两种角色又混回一起"
        )


# ---------------------------------------------------------------------------
# 反向守卫：修好"看得见"不许顺手让不该看见的也看见
# ---------------------------------------------------------------------------

def test_internal_only_fields_never_reach_the_agent():
    """只写进存储、从不给 agent 看的字段，不许出现在任何投影里。

    counter_epoch_id 是计数器"第几轮"的内部标记，说给 agent 听只会让它
    以为这是个可以聊的指标。
    """
    reachable = set(routing.snapshot_fields(include_query_tools=True))
    leaked = routing.INTERNAL_ONLY_FIELDS & reachable
    assert not leaked, f"内部字段泄进了 agent 投影：{sorted(leaked)}"


def test_pull_only_metrics_stay_out_of_the_cheap_wake_snapshot():
    """唤醒快照每次心跳都要读，健康指标属于按需查询那一档。"""
    cheap = set(routing.snapshot_fields(include_query_tools=False))
    for f in ("current_heart_rate", "weight_kg", "blood_glucose_mmol_l"):
        assert f not in cheap, f"{f} 不该进便宜的唤醒快照"
    assert "local_time" in cheap  # 常驻的照旧在


# ---------------------------------------------------------------------------
# 三套词表之间不许再出现第二份手抄
# ---------------------------------------------------------------------------

def test_renames_are_the_only_hand_written_part_and_they_all_apply():
    """改过名的那几条必须真的是上报字段，否则就是写废了没人发现。"""
    report_fields = {f for sig in catalog.SIGNALS.values() for f in sig.outputs}
    stale = set(routing.FIELD_RENAMES) - report_fields
    assert not stale, f"这些改名规则对应的上报字段已经不存在了：{sorted(stale)}"


def test_every_route_lands_on_a_field_the_manifest_actually_declares():
    """路由的终点必须真的存在 —— 落到一个没声明的字段上会被管线静默过滤掉。

    这正是拆包没拆干净时的症状：bmi / 体脂 / 身高跟着体重一起送出去，
    manifest 里没有这几个字段，于是被当未声明字段丢掉，用户只会发现
    这些指标从来没有过数据。
    """
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    missing = []
    for r in routing.ROUTES.values():
        sig = MINIMAL_SIGNALS.get(r.storage_signal)
        if sig is None:
            continue  # 只走老路扁平快照的字段，manifest 里本来就没有
        if not any(f.key == r.storage_field for f in sig.fields):
            missing.append(f"{r.report_field} -> {r.storage_signal}.{r.storage_field}")
    assert not missing, "路由落到了 manifest 没声明的字段上：\n" + "\n".join(missing)


def test_health_signals_fields_py_exposes_are_all_reachable():
    """``fields.py`` 声称自己是"agent 能看到什么"的出处，routing 管"谁把门"。

    两张表不是同一件事，但必须对同一批指标成立 —— 否则宿主不知道该信哪个。
    健康那几条 fields.py 存的是「去哪个上报键下面找」，所以能直接连上。
    """
    reachable = set(routing.snapshot_fields(include_query_tools=True))
    unreachable = []
    for agent_name, probes in fields.AGENT_SIGNAL_FIELDS.items():
        report_keys = [p for p in probes if p in catalog.SIGNALS]
        if not report_keys:
            continue  # 不是"去某个上报键下面找"的那种，跳过
        for key in report_keys:
            usable = [
                f for f in catalog.SIGNALS[key].outputs
                if f != "user_state" and f in reachable
            ]
            if not usable:
                unreachable.append(f"{agent_name} -> {key}")
    assert not unreachable, (
        "fields.py 说 agent 能看到这些信号，但它们一个可达字段都没有：\n"
        + "\n".join(unreachable)
    )


# ---------------------------------------------------------------------------
# 参考适配器的拆包表不许和路由图各说各话
# ---------------------------------------------------------------------------

def _example_split():
    from examples.ios_adapter import SPLIT_OFF

    return {(k, f): v for k, m in SPLIT_OFF.items() for f, v in m.items()}


def _routed_split():
    return {
        (r.report_key, r.storage_field): (r.storage_signal, r.storage_field)
        for r in routing.ROUTES.values()
        if r.is_split_off
    }


def test_the_reference_adapter_never_contradicts_the_route_graph():
    """参考适配器拆到哪儿，必须和路由图说的一致。

    它此前是一张独立手抄表 —— 路由图改了它不会红，而"拆到别处去了"
    这种错落库之后看不出来（值合法、位置错）。
    """
    example, routed = _example_split(), _routed_split()
    disagree = [
        (k, example[k], routed[k]) for k in set(example) & set(routed) if example[k] != routed[k]
    ]
    assert not disagree, f"适配器和路由图对落点的说法不一致：{disagree}"
    invented = sorted(set(example) - set(routed))
    assert not invented, f"适配器拆了路由图里没有的字段：{invented}"


def test_the_three_metrics_that_stay_in_the_pack_are_pinned_on_purpose():
    """体重 / 血糖 / 静息心率目前**留在整包信号里**，没有真拆出去。

    这是已知缺口，不是这次要改的东西 —— 改它等于改数据落点，要迁移。
    manifest 给这三个各声明了独立信号（体重要永久留、静息心率是一次测量），
    而适配器仍把它们留在 health_body / health_metabolic / health_vitals 里，
    于是它们跟着整包的保留期和身份策略走。

    钉在这里是为了：① 缺口可见，不会被当成"已经拆好了"；
    ② 哪天真拆了，这条会红，提醒同步迁移。
    """
    stay_behind = sorted(set(_routed_split()) - set(_example_split()))
    assert stay_behind == [
        ("health_body", "weight_kg"),
        ("health_metabolic", "blood_glucose_mmol_l"),
        ("health_vitals", "resting_heart_rate"),
    ], f"留在整包里的指标变了：{stay_behind}"
