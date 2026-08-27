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


def test_the_five_signals_cover_the_shapes_the_pipeline_has_to_handle():
    """选这五个的意义就在覆盖面——少一种形态，管线就有一条路没走过。"""
    modes = {s.storage_mode for s in MINIMAL_SIGNALS.values()}
    identities = {s.identity_strategy for s in MINIMAL_SIGNALS.values()}
    attributions = {s.attribution_strategy for s in MINIMAL_SIGNALS.values()}
    assert modes == {"current_only", "current_timeline_aggregate"}
    assert identities == {"singleton", "source_event_id", "deterministic_digest"}
    assert "split_at_midnight" in attributions      # 跨午夜的路要有信号走过
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
