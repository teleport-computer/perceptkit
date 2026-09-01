"""Manifest 的四条自动检查。

**每条检查都配一个"故意写坏"的用例。** 只测"正确的 manifest 能通过"是没有
牙的守卫——路径写错、条件写反，它照样全绿。这里每条都验证它**确实会因为
对应的缺口变红**。
"""
from __future__ import annotations

import dataclasses

import pytest

from perceptkit.manifest import (
    MINIMAL_SIGNALS,
    PERMANENT,
    FieldDefinition,
    SignalDefinition,
    validate_manifest,
)
from perceptkit.manifest.checks import (
    check_history_has_retention,
    check_named_implementations_exist,
    check_types_and_units,
    check_wake_eligible_fields_have_comparators,
)


def _one(sig: SignalDefinition) -> dict[str, SignalDefinition]:
    return {sig.key: sig}


def _mutate_field(sig: SignalDefinition, index: int, **over) -> SignalDefinition:
    fields = list(sig.fields)
    fields[index] = dataclasses.replace(fields[index], **over)
    return dataclasses.replace(sig, fields=tuple(fields))


# ---------------------------------------------------------------------------
# 最小 manifest 本身
# ---------------------------------------------------------------------------

def test_the_minimal_manifest_is_clean():
    assert validate_manifest(MINIMAL_SIGNALS) == []


def test_section_5_1_signals_are_all_present():
    """§5.1「时间、设备与短期环境」逐条对完之后加进来的。
    少一个就是漏了一条已确认的产品要求。"""
    for key in ("time_context", "battery", "broadcast", "screen_change",
                "presence_recovery", "audio_route", "weather"):
        assert key in MINIMAL_SIGNALS, key


def test_the_signal_we_decided_not_to_build_is_written_down():
    """明确「不做」也要写下来 —— 否则下一个人会当成漏项又捡回来。"""
    from perceptkit.manifest import DECLINED_SIGNALS
    assert "network_connection" in DECLINED_SIGNALS
    assert DECLINED_SIGNALS["network_connection"].strip()


def test_screen_change_keeps_no_fingerprint():
    """指纹序列存久了能反推用户屏幕上出现过什么。
    这个信号只许有「变了/没变」这一个布尔。"""
    sig = MINIMAL_SIGNALS["screen_change"]
    assert [f.key for f in sig.fields] == ["changed"]
    assert not sig.stores_history


def test_timezone_is_declared_as_an_iana_name_that_can_wake():
    """只有偏移不够：纽约冬天 -05:00、夏天 -04:00 是同一个时区，
    光看偏移分不出来，夏令时切换那天就会算错。"""
    fields = MINIMAL_SIGNALS["time_context"].field_map()
    assert fields["time_zone_id"].wake_eligible
    assert MINIMAL_SIGNALS["time_context"].keeps_history_forever


def test_the_five_signals_cover_the_shapes_the_pipeline_has_to_handle():
    """选这五个的意义就在覆盖面——少一种形态，管线就有一条路没走过。"""
    modes = {s.storage_mode for s in MINIMAL_SIGNALS.values()}
    identities = {s.identity_strategy for s in MINIMAL_SIGNALS.values()}
    attributions = {s.attribution_strategy for s in MINIMAL_SIGNALS.values()}
    assert {"current_only", "current_timeline_aggregate",
            "current_short_timeline"} <= modes
    assert identities == {"singleton", "source_event_id", "deterministic_digest"}
    # 跨午夜那条路**目前没有信号真的走**。先前有六个信号声明了
    # split_at_midnight，但它们发的是时间点快照、没有 start_at/end_at，
    # 每一条都警告一次再退回按 occurred_at 归属 —— 等于这条路从没被走过，
    # 只是看起来被走过。已改成 instant（行为逐字节不变）。
    #
    # 睡眠和运动确实带区间，但产品上刻意整段归到【结束】那天
    # （"昨晚睡了几个小时"问的是醒来那天），所以走 episode_end。
    # 真要按午夜把时长劈成两天，是**聚合层**的事，那个窟窿还开着。
    assert "split_at_midnight" not in attributions
    assert "episode_end" in attributions            # 带区间的路有信号走过
    assert "source_local_date" in attributions      # 上游直接给日期的路也要有


def test_signals_that_diverge_from_the_product_spec_say_why_in_the_data():
    """和规范有出入的地方必须写在数据结构里，不是藏在注释里——
    读 manifest 的人不一定读过代码注释。"""
    for key in ("presence_recovery", "focus_state", "location_city"):
        assert MINIMAL_SIGNALS[key].note, f"{key} 偏离了规范却没写原因"


def test_restricted_fields_are_never_visible_to_the_agent():
    """精确坐标这类字段的"永不外泄"必须是可测的规则，不是口头约定。"""
    for sig in MINIMAL_SIGNALS.values():
        for f in sig.fields:
            if f.privacy_class == "restricted":
                assert f.query_visibility == "never", f"{sig.key}.{f.key}"


# ---------------------------------------------------------------------------
# ① 类型和单位
# ---------------------------------------------------------------------------

def test_catches_a_numeric_field_without_a_unit():
    """没有单位的数字跨宿主必然被解释错——公斤还是磅，接收方只能猜。"""
    broken = _mutate_field(MINIMAL_SIGNALS["steps"], 0, unit=None)
    problems = check_types_and_units(_one(broken))
    assert any("unit" in p for p in problems)


def test_catches_an_enum_field_that_never_says_what_the_values_are():
    broken = _mutate_field(MINIMAL_SIGNALS["presence_recovery"], 2, enum=None)
    assert any("enum" in p for p in check_types_and_units(_one(broken)))


def test_catches_a_bogus_value_type():
    broken = _mutate_field(MINIMAL_SIGNALS["battery"], 0, value_type="floatish")
    assert any("value_type" in p for p in check_types_and_units(_one(broken)))


def test_catches_a_signal_with_no_fields_at_all():
    broken = dataclasses.replace(MINIMAL_SIGNALS["battery"], fields=())
    assert check_types_and_units(_one(broken))


# ---------------------------------------------------------------------------
# ② 历史与保留期
# ---------------------------------------------------------------------------

def test_catches_history_without_a_retention_period():
    """忘了声明保留期，历史就无限增长且永远不会被清理——而且没有任何症状，
    直到某天库满了。"""
    broken = dataclasses.replace(MINIMAL_SIGNALS["steps"], history_retention_days=0)
    assert any("history" in p or "保留期" in p or "历史" in p
               for p in check_history_has_retention(_one(broken)))


def test_catches_current_only_that_also_claims_to_keep_history():
    broken = dataclasses.replace(MINIMAL_SIGNALS["battery"],
                                 history_retention_days=PERMANENT)
    assert check_history_has_retention(_one(broken))


def test_catches_history_that_nothing_will_ever_aggregate():
    """存了明细却没有任何字段声明怎么聚合 = 存下来没人读。"""
    broken = _mutate_field(MINIMAL_SIGNALS["focus_state"], 0,
                           aggregation_strategy="none")
    assert check_history_has_retention(_one(broken))


def test_catches_a_nonsense_retention_number():
    broken = dataclasses.replace(MINIMAL_SIGNALS["steps"], history_retention_days=-7)
    assert check_history_has_retention(_one(broken))


# ---------------------------------------------------------------------------
# ③ 名字必须解析到实现
# ---------------------------------------------------------------------------

def test_catches_a_normalizer_name_with_nothing_behind_it():
    """这一条抓的是现状里那 10 个空指针式的 resolver 名字：
    声明了但没有任何实现，不会抛异常，只会静默地不标准化。"""
    broken = _mutate_field(MINIMAL_SIGNALS["location_city"], 0,
                           normalizer="coarse_locality")
    assert any("normalizer" in p for p in
               check_named_implementations_exist(_one(broken)))


def test_a_host_provided_normalizer_is_fine_once_registered():
    """有些 normalizer 天然属于宿主（地理编码是 I/O，kit 不做）。"""
    ok = _mutate_field(MINIMAL_SIGNALS["location_city"], 0,
                       normalizer="coarse_locality")
    assert check_named_implementations_exist(
        _one(ok), available_normalizers={"coarse_locality"}
    ) == []


@pytest.mark.parametrize("attr,bad", [
    ("storage_mode", "whatever"),
    ("identity_strategy", "vibes"),
    ("attribution_strategy", "yesterday_ish"),
    ("source_profile", "made_up"),
])
def test_catches_strategy_names_outside_the_vocabulary(attr, bad):
    broken = dataclasses.replace(MINIMAL_SIGNALS["steps"], **{attr: bad})
    assert any(attr in p for p in check_named_implementations_exist(_one(broken)))


# ---------------------------------------------------------------------------
# ④ 能唤醒的字段必须说清怎么算触发
# ---------------------------------------------------------------------------

def test_catches_a_wake_field_that_can_never_fire():
    """这是最典型的"上线了但功能没生效"：看起来配好了，实际是死的。
    最小 manifest 写第一版时就踩了这个，是这条检查抓出来的。"""
    broken = _mutate_field(MINIMAL_SIGNALS["steps"], 0, comparison_strategy="none")
    assert any("comparison_strategy" in p or "永远不会触发" in p
               for p in check_wake_eligible_fields_have_comparators(_one(broken)))


def test_catches_waking_the_agent_on_something_it_can_never_see():
    broken = _mutate_field(MINIMAL_SIGNALS["steps"], 0, query_visibility="never")
    assert check_wake_eligible_fields_have_comparators(_one(broken))


def test_occurrence_signals_are_wake_eligible_without_a_previous_value():
    """解锁、新增照片这类：观测到达本身就是事件，没有前后值可比。"""
    sig = MINIMAL_SIGNALS["presence_recovery"]
    recovered = sig.field_map()["recovered_at"]
    assert recovered.wake_eligible
    assert recovered.comparison_strategy == "occurrence"
    assert check_wake_eligible_fields_have_comparators(_one(sig)) == []


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------

def test_catches_a_key_that_disagrees_with_the_definition():
    sig = MINIMAL_SIGNALS["battery"]
    assert validate_manifest({"batery": sig})


def test_catches_a_duplicated_field():
    sig = MINIMAL_SIGNALS["battery"]
    dup = dataclasses.replace(sig, fields=sig.fields + (sig.fields[0],))
    assert any("重复" in p for p in validate_manifest(_one(dup)))


def test_validate_returns_every_problem_at_once():
    """一次看到全部缺口，比逐个修再重跑快得多。"""
    broken = dataclasses.replace(
        _mutate_field(MINIMAL_SIGNALS["steps"], 0, unit=None, comparison_strategy="none"),
        identity_strategy="vibes",
    )
    assert len(validate_manifest(_one(broken))) >= 3


def test_permanent_is_distinguishable_from_forgot_to_declare():
    """用 None 表示永久会和"忘了写"混淆——那正是这套检查要抓的东西。"""
    assert PERMANENT == -1
    assert MINIMAL_SIGNALS["steps"].keeps_history_forever
    assert not MINIMAL_SIGNALS["battery"].stores_history


# ---------------------------------------------------------------------------
# 明细保留期 vs 聚合保留期
# ---------------------------------------------------------------------------

def test_detail_and_aggregate_retention_are_two_separate_knobs():
    """典型形态是**明细短、聚合永久** —— 一个字段表达不了这件事。

    明细是聚合的几十倍体量，但能回答的问题正好反过来：
    「上周三下午你专注了多久」时间越久越没人问，
    「你今年专注时间比去年长了吗」时间越久越值钱。
    """
    focus = MINIMAL_SIGNALS["focus_state"]
    assert focus.history_retention_days == 365
    assert focus.keeps_aggregates_forever


def test_focus_and_motion_were_decided_together_so_they_must_match():
    """这两个信号的保留期是同一次决定定下来的。

    分开写就会漂 —— 先前 motion 是 365、focus 却是永久，
    两者本该一样，没有任何测试拦住这个不一致。
    """
    focus = MINIMAL_SIGNALS["focus_state"]
    motion = MINIMAL_SIGNALS["motion_state"]
    assert focus.history_retention_days == motion.history_retention_days
    assert focus.keeps_aggregates_forever == motion.keeps_aggregates_forever


def test_a_signal_that_does_not_say_falls_back_to_one_number():
    """没单独声明聚合保留期的，就跟明细一样 —— 不会凭空变成永久。"""
    weather = MINIMAL_SIGNALS["weather"]
    assert weather.aggregate_retention_days is None
    assert weather.effective_aggregate_retention_days == weather.history_retention_days


def test_catches_an_aggregate_that_dies_before_its_details():
    """日统计先于它依据的明细消失，历史上会出现一段有明细却查不到统计的窗口。"""
    from dataclasses import replace
    bad = replace(MINIMAL_SIGNALS["motion_state"],
                  history_retention_days=365, aggregate_retention_days=30)
    problems = validate_manifest({"motion_state": bad})
    assert any("比明细的" in p for p in problems)


def test_catches_permanent_details_with_expiring_aggregates():
    """留明细不留聚合是反的：既花了存储，又丢了长期趋势。"""
    from dataclasses import replace
    bad = replace(MINIMAL_SIGNALS["steps"],
                  history_retention_days=PERMANENT, aggregate_retention_days=90)
    problems = validate_manifest({"steps": bad})
    assert any("反了" in p for p in problems)


# ---------------------------------------------------------------------------
# Reference storage mapping（产品规范 §15 点名要的那份表）
# ---------------------------------------------------------------------------

def test_the_mapping_covers_every_signal_in_the_manifest():
    """漏一个信号，照着这份表建库的人就少建一份存储 —— 而且没人会发现。"""
    from perceptkit.manifest import reference_mapping
    rows = reference_mapping(MINIMAL_SIGNALS)
    assert {r["signal"] for r in rows} == set(MINIMAL_SIGNALS)


def test_every_storage_mode_knows_which_objects_it_writes_to():
    """新增一种 storage_mode 却忘了说它落到哪些对象，这份表会出现空行。"""
    from perceptkit.manifest import MODE_OBJECTS
    from perceptkit.manifest.types import STORAGE_MODES
    for mode in STORAGE_MODES:
        assert MODE_OBJECTS.get(mode), f"{mode} 没说它写到哪些逻辑对象"


def test_a_signal_that_diverges_from_the_spec_shows_up_in_the_rendered_table():
    """偏差写在 note 里就是为了跟着表一起被读到，不能只躺在源码里。"""
    from perceptkit.manifest import render_reference_mapping
    text = render_reference_mapping(MINIMAL_SIGNALS)
    assert "和产品规范有出入的地方" in text
    assert "proximity_anchor" in text and "focus_state" in text


def test_the_checked_in_table_is_still_what_the_manifest_produces():
    """表是生成的 —— 改了 manifest 忘了重新生成，这条会红。

    手写的表和代码之间没有任何东西拦着它们漂开，而这份表恰恰是给别人
    照着建库用的。
    """
    import pathlib
    from perceptkit.manifest import render_reference_mapping
    checked_in = pathlib.Path(__file__).resolve().parents[1] / "docs" / "reference-storage-mapping.md"
    assert checked_in.read_text(encoding="utf-8") == render_reference_mapping(MINIMAL_SIGNALS)


# ---------------------------------------------------------------------------
# app_usage：刻意只做能答准的那一半
# ---------------------------------------------------------------------------

def test_app_usage_counts_opens_but_never_totals_duration():
    """iOS 拿不到前台 app，数据全靠用户逐个 app 配快捷指令自动化。

    「今天用了多久」需要 close，而多数 session 根本没有结束事件 ——
    算出来的时长只反映「谁配得全」，错得还不明显。
    「今天打开了几次」只靠 open 就能答，所以配置不全也不影响。
    """
    fields = MINIMAL_SIGNALS["app_usage"].field_map()
    # `occurrence_count`（求和），**不是** `daily_total`（取 max）。
    # 后者是给「来源自己在数」的量用的（今日步数 8000 → 8300）；每次打开各贡献
    # 1 的话，max 永远是 1 —— 用户开了二十次，答案还是「1 次」。
    assert fields["open_count"].aggregation_strategy == "occurrence_count"
    # 没有任何字段在按时长聚合
    assert all(f.aggregation_strategy != "duration_by_state"
               for f in MINIMAL_SIGNALS["app_usage"].fields)


def test_app_usage_still_accepts_close_events():
    """收下 close，只是不据此算时长 —— 拒收会让已经配好的用户白配。"""
    action = MINIMAL_SIGNALS["app_usage"].field_map()["action"]
    assert set(action.enum or ()) == {"open", "close"}


def test_the_reason_for_the_narrower_scope_is_in_the_data_not_a_commit_message():
    """为什么砍掉时长统计，得让读 manifest 的人看得到 ——
    否则下一个人只会觉得「这里少了个字段」，顺手补上。"""
    note = MINIMAL_SIGNALS["app_usage"].note or ""
    assert "快捷指令" in note and "时长" in note


# ---------------------------------------------------------------------------
# 第五条检查：投影不漂移（产品规范 §8 列了五条，我们先前只有四条）
# ---------------------------------------------------------------------------

def test_the_minimal_manifest_has_no_projection_drift():
    from perceptkit.manifest import check_projections_do_not_drift
    assert check_projections_do_not_drift(MINIMAL_SIGNALS) == []


def test_catches_a_field_that_is_aggregated_but_never_readable():
    """每天算一遍、存一份，谁也读不到 —— 白干，而且不会有人发现。"""
    from dataclasses import replace
    from perceptkit.manifest import check_projections_do_not_drift
    sig = MINIMAL_SIGNALS["location_city"]
    coord = next(f for f in sig.fields if f.key == "coordinate")
    bad = replace(sig, fields=tuple(
        replace(f, aggregation_strategy="daily_total") if f is coord else f
        for f in sig.fields))
    problems = check_projections_do_not_drift({"location_city": bad})
    assert any("谁也读不到" in p for p in problems)


def test_catches_a_never_visible_field_that_can_wake():
    """🔴 这条抓的是泄漏：wake_eligible 的字段，前后值会被写进事件信封的
    previous/current，而信封会存下来、投出去、进模型上下文。

    写入边界把这个字段拦住了，它却能从事件这条路漏出去。
    """
    from dataclasses import replace
    from perceptkit.manifest import check_projections_do_not_drift
    sig = MINIMAL_SIGNALS["proximity_anchor"]
    raw = next(f for f in sig.fields if f.key == "raw_identifier")
    bad = replace(sig, fields=tuple(
        replace(f, wake_eligible=True) if f is raw else f for f in sig.fields))
    problems = check_projections_do_not_drift({"proximity_anchor": bad})
    assert any("漏出去" in p for p in problems)


def test_catches_a_never_visible_field_used_for_comparison():
    """声明了 never 的字段在写入边界就被丢掉了 —— 拿它做比较，
    判断依据根本不存在。"""
    from dataclasses import replace
    from perceptkit.manifest import check_projections_do_not_drift
    sig = MINIMAL_SIGNALS["location_city"]
    coord = next(f for f in sig.fields if f.key == "coordinate")
    bad = replace(sig, fields=tuple(
        replace(f, comparison_strategy="exact") if f is coord else f
        for f in sig.fields))
    assert any("判断依据根本不存在" in p
               for p in check_projections_do_not_drift({"location_city": bad}))


def test_catches_an_aggregation_on_a_signal_that_keeps_no_history():
    """聚合结果没有地方落。"""
    from dataclasses import replace
    from perceptkit.manifest import check_projections_do_not_drift
    sig = MINIMAL_SIGNALS["battery"]          # current_only
    lvl = next(f for f in sig.fields if f.key == "level_ratio")
    bad = replace(sig, fields=tuple(
        replace(f, aggregation_strategy="numeric_dist") if f is lvl else f
        for f in sig.fields))
    assert any("没有地方落" in p
               for p in check_projections_do_not_drift({"battery": bad}))


def test_an_interval_strategy_requires_the_signal_to_actually_send_intervals():
    """声明 split_at_midnight / episode_end 却没有 start_at / end_at 字段，
    管线每条观测都会去找一个不存在的区间、警告、再退回 —— 结果和 instant
    一模一样，但读 manifest 的人会以为跨午夜被处理了。

    六个信号先前正是这样错标的，真跑一份上报才暴露出来。
    """
    from perceptkit.manifest import check_projections_do_not_drift  # noqa: F401
    offenders = []
    for key, sig in MINIMAL_SIGNALS.items():
        if sig.attribution_strategy not in ("split_at_midnight", "episode_end"):
            continue
        if not {"start_at", "end_at"} & set(sig.field_map()):
            offenders.append(key)
    assert not offenders, f"这些信号声明了区间策略却不发区间：{offenders}"
