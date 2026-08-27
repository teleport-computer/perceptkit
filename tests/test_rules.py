"""九种规则 + 生命周期。

产品规范 §22 的第 8~11 条是这一层的验收标准：
2999→3000 只触发一次 / 4999→5000 独立触发 / 同天 5001 不重复 / 次日重新武装。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit.contracts import ContractError
from perceptkit.rules import EventDefinition, Lifecycle, RuleState, evaluate, scope_key

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 27, 10, 30, tzinfo=SH)


def steps_rule(threshold=3000, **over):
    base = dict(
        definition_id=f"daily_steps_{threshold}", version=1, signal="steps",
        field_name="step_count", condition_type="threshold_crossing",
        operator="gte", value=threshold, event_type="activity.step_goal_reached",
    )
    base.update(over)
    return EventDefinition(**base)


def run(rule, state, current, *, now=NOW, **ctx):
    return evaluate(rule, state, current, now=now, context=ctx)


# ---------------------------------------------------------------------------
# threshold_crossing —— 最容易写错的一条
# ---------------------------------------------------------------------------

def test_crossing_the_line_fires_once():
    r = run(steps_rule(), RuleState(previous_value=2999), 3000)
    assert r.fired and r.previous == 2999 and r.current == 3000


def test_staying_past_the_line_does_not_fire_again():
    """`current >= 3000` 会让 3001、3010、3100 每次上报都触发 ——
    用户走一天路能被提醒几十次。"""
    for value in (3001, 3010, 3100):
        assert not run(steps_rule(), RuleState(previous_value=3000), value).fired


def test_being_past_the_line_on_the_very_first_observation_does_not_fire():
    """用户中午才装上 app，那时步数已经过万，不该立刻收到"你走够 3000 步了"。"""
    assert not run(steps_rule(), RuleState(), 12000).fired


def test_two_thresholds_are_two_independent_rules():
    """规范 §22-9：4999→5000 要能独立触发，不受 3000 那条影响。"""
    state = RuleState(previous_value=4999)
    assert run(steps_rule(5000), state, 5000).fired
    assert not run(steps_rule(3000), state, 5000).fired    # 早就跨过了


@pytest.mark.parametrize("op,prev,now,fires", [
    ("gte", 2999, 3000, True),
    ("gt", 3000, 3001, True),
    ("gt", 2999, 3000, False),
    ("lte", 3001, 3000, True),
    ("lt", 3000, 2999, True),
])
def test_all_four_comparison_directions(op, prev, now, fires):
    r = run(steps_rule(3000, operator=op), RuleState(previous_value=prev), now)
    assert r.fired is fires


def test_non_numeric_values_never_fire_a_threshold_rule():
    assert not run(steps_rule(), RuleState(previous_value="a"), "b").fired
    assert not run(steps_rule(), RuleState(previous_value=1), None).fired


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def test_the_same_scope_only_fires_once():
    """规范 §22-10：同一天后续的 5001 不重复触发。"""
    first = run(steps_rule(), RuleState(previous_value=2999), 3000)
    assert first.fired
    assert not run(steps_rule(), first.state, 3500).fired


def test_a_new_scope_rearms_without_any_scheduled_job():
    """换一天就是一条新状态 —— 不需要定时任务去重置。"""
    today = scope_key(steps_rule(), local_date=date(2026, 8, 27))
    tomorrow = scope_key(steps_rule(), local_date=date(2026, 8, 28))
    assert today != tomorrow
    # 新 scope 拿到的是一条干净状态
    assert run(steps_rule(), RuleState(previous_value=2999), 3000).fired


def test_fire_every_ignores_the_once_per_scope_limit():
    rule = steps_rule(lifecycle=Lifecycle(fire="every"))
    first = run(rule, RuleState(previous_value=2999), 3000)
    second = run(rule, RuleState(previous_value=2999, fired_in_scope=True), 3000)
    assert first.fired and second.fired


def test_cooldown_blocks_then_releases():
    rule = steps_rule(lifecycle=Lifecycle(rearm="cooldown", cooldown_seconds=300))
    fired = RuleState(previous_value=2999, fired_in_scope=True,
                      last_fired_at=NOW.isoformat())
    assert not run(rule, fired, 3000, now=NOW + timedelta(seconds=60)).fired
    assert run(rule, fired, 3000, now=NOW + timedelta(seconds=301)).fired


def test_a_disabled_rule_never_fires():
    assert not run(steps_rule(enabled=False), RuleState(previous_value=2999), 3000).fired


def test_an_unknown_condition_type_is_reported_not_crashed():
    """规则是用户配的。看不懂的类型不该让整条管线炸掉。"""
    r = run(steps_rule(condition_type="telepathy"), RuleState(), 1)
    assert not r.fired and "telepathy" in r.reason


# ---------------------------------------------------------------------------
# 状态推进
# ---------------------------------------------------------------------------

def test_previous_value_advances_even_when_nothing_fires():
    """不推进的话，crossing 永远拿不到正确的前值 —— 规则会变成死的。"""
    r = run(steps_rule(), RuleState(previous_value=100), 200)
    assert not r.fired
    assert r.state.previous_value == 200


def test_state_survives_a_round_trip_through_a_dict():
    """宿主把它存进数据库再读回来，形状不能变。"""
    s = RuleState(previous_value=42, fired_in_scope=True,
                  last_fired_at=NOW.isoformat(), seen_keys=("a", "b"))
    assert RuleState.from_dict(s.to_dict()) == s


# ---------------------------------------------------------------------------
# 其余八种
# ---------------------------------------------------------------------------

def _rule(kind, **over):
    base = dict(definition_id="r", version=1, signal="s",
                condition_type=kind, event_type="t")
    base.update(over)
    return EventDefinition(**base)


def test_changed_ignores_the_very_first_observation():
    """否则用户刚装上 app，所有信号会一起触发。"""
    assert not run(_rule("changed"), RuleState(), "walking").fired
    assert run(_rule("changed"), RuleState(previous_value="still"), "walking").fired
    assert not run(_rule("changed"), RuleState(previous_value="walking"), "walking").fired


def test_enters_only_fires_at_the_moment_of_crossing_in():
    rule = _rule("enters", value="home")
    assert run(rule, RuleState(previous_value="work"), "home").fired
    assert not run(rule, RuleState(previous_value="home"), "home").fired


def test_leaves_mirrors_enters():
    rule = _rule("leaves", value="home")
    assert run(rule, RuleState(previous_value="home"), "work").fired
    assert not run(rule, RuleState(previous_value="work"), "cafe").fired


def test_equals_does_not_need_a_previous_value():
    assert run(_rule("equals", value=True), RuleState(), True).fired


def test_delta_fires_on_magnitude_in_either_direction():
    rule = _rule("delta", value=10)
    assert run(rule, RuleState(previous_value=100), 111).fired
    assert run(rule, RuleState(previous_value=100), 89).fired
    assert not run(rule, RuleState(previous_value=100), 105).fired


def test_occurrence_dedupes_on_the_source_event_id():
    rule = _rule("occurrence")
    first = run(rule, RuleState(), None, source_event_id="evt_1")
    assert first.fired
    assert not run(rule, first.state, None, source_event_id="evt_1").fired
    assert run(rule, first.state, None, source_event_id="evt_2").fired


def test_occurrence_without_a_dedupe_key_skips_rather_than_risking_a_double_alert():
    """宁可漏一次，也不要因为客户端重传让用户被同一件事提醒两次。"""
    assert not run(_rule("occurrence"), RuleState(), None).fired


def test_occurrence_keeps_the_seen_key_list_bounded():
    """不设上限的话，高频信号会让这条状态无限膨胀 —— 而它每次求值都要被读出来。"""
    from perceptkit.rules.types import MAX_SEEN_KEYS
    state = RuleState(seen_keys=tuple(f"k{i}" for i in range(MAX_SEEN_KEYS)))
    r = run(_rule("occurrence"), state, None, source_event_id="new")
    assert r.fired and len(r.state.seen_keys) == MAX_SEEN_KEYS
    assert "new" in r.state.seen_keys and "k0" not in r.state.seen_keys


def test_streak_is_edge_triggered_not_level_triggered():
    """"连续三天睡不好"应该提醒一次，不是从第三天起天天念叨。"""
    rule = _rule("streak", value=3)
    assert run(rule, RuleState(previous_value=2), None, streak_length=3).fired
    assert not run(rule, RuleState(previous_value=3), None, streak_length=4).fired


def test_absence_fires_once_the_silence_is_long_enough():
    rule = _rule("absence", value=3600)
    assert run(rule, RuleState(), None, silent_seconds=4000).fired
    assert not run(rule, RuleState(), None, silent_seconds=100).fired


# ---------------------------------------------------------------------------
# 定义的解析
# ---------------------------------------------------------------------------

def test_a_definition_parses_from_a_plain_dict():
    """kit 零依赖，不解析 YAML。宿主想用 YAML 就自己 safe_load 成 dict。"""
    d = EventDefinition.parse({
        "id": "daily_steps_3000", "version": 1,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
        "lifecycle": {"scope": "local_day", "fire": "once", "rearm": "next_scope"},
        "event": {"type": "activity.step_goal_reached"},
        "wake": {"enabled": True, "cooldown_seconds": 0},
    })
    assert d.definition_id == "daily_steps_3000"
    assert d.value == 3000 and d.lifecycle.scope == "local_day"


def test_a_definition_missing_required_parts_reports_all_of_them():
    with pytest.raises(ContractError) as exc:
        EventDefinition.parse({"version": 1})
    assert len(exc.value.errors) >= 3       # id / source.signal / condition.type / event.type


def test_a_bogus_lifecycle_is_refused():
    with pytest.raises(ContractError):
        Lifecycle(scope="whenever")
    with pytest.raises(ContractError):
        Lifecycle(cooldown_seconds=-1)


def test_custom_evaluators_can_be_registered():
    """宿主可以处理内置模板覆盖不了的逻辑 —— 但那要求宿主自己信任那段代码，
    普通用户配置仍然只能用声明式模板。"""
    from perceptkit.rules.types import RuleResult

    def always(d, s, current, ctx):
        return RuleResult(True, s, reason="custom")

    r = evaluate(_rule("my_thing"), RuleState(), 1, now=NOW,
                 extra_evaluators={"my_thing": always})
    assert r.fired and r.reason == "custom"
