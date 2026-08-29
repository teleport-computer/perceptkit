"""事件信封 —— kit 交给宿主的唯一产物，所以它必须稳。

产品规范 §3.2 要的是「**稳定的** PerceptionEvent envelope」。稳定不是靠人自觉，
要有东西拦住它漂移：宿主接的是信封不是规则，信封多一个键少一个键，
所有接入方都得改代码。

之前这个信封只在端到端测试里被顺带用过，没有一条测试钉住它的形状。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit.contracts.event import (
    ALLOWED_CONTEXT_KEYS,
    MAX_CONTEXT_VALUE_CHARS,
    EventCondition,
    PerceptionEvent,
    safe_context,
)

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 28, 9, 0, tzinfo=SH)


def event(**over) -> PerceptionEvent:
    base = dict(
        event_id="e1", definition_id="d1", definition_version=1, subject_id="u1",
        type="health.step_goal", signal="steps", occurred_at=NOW, received_at=NOW,
        condition=EventCondition(type="threshold_crossing", operator="gte", value=3000),
        field_name="step_count", previous=2999, current=3000,
    )
    base.update(over)
    return PerceptionEvent(**base)


# ---------------------------------------------------------------------------
# 形状
# ---------------------------------------------------------------------------

def test_the_envelope_has_exactly_these_keys():
    """加键会让老宿主收到看不懂的东西，删键会让老宿主直接崩。

    这条测试红了不代表你改错了 —— 代表你在改一个**所有接入方都要跟着改**
    的契约，请连同 schema_version 一起想清楚。
    """
    assert set(event().to_dict()) == {
        "event_id", "definition_id", "definition_version", "subject_id",
        "type", "signal", "field", "occurred_at", "received_at",
        "previous", "current", "condition", "context", "schema_version",
    }


def test_the_envelope_says_which_version_it_is():
    assert event().to_dict()["schema_version"] >= 1


def test_times_go_out_as_strings_not_as_python_objects():
    """宿主要把它序列化成 JSON 投出去 —— 留个 datetime 在里面就是当场炸。"""
    d = event().to_dict()
    assert isinstance(d["occurred_at"], str) and d["occurred_at"].startswith("2026-08-28")
    assert isinstance(d["received_at"], str)


def test_an_occurrence_event_has_no_field_and_no_before_after():
    """有些事件是「这条信号到了」本身，没有具体字段、也没有前后值。"""
    d = event(field_name=None, previous=None, current=None).to_dict()
    assert d["field"] is None and d["previous"] is None and d["current"] is None


def test_the_condition_snapshot_travels_with_the_event():
    """存判据快照而不是规则引用：规则会被改被删，而事件是已经发生的事实，
    不该因为规则后来变了就解释不通。"""
    d = event().to_dict()
    assert d["condition"] == {"type": "threshold_crossing",
                              "operator": "gte", "value": 3000}


def test_a_condition_without_an_operator_omits_it_instead_of_sending_null():
    d = event(condition=EventCondition(type="occurrence")).to_dict()
    assert d["condition"] == {"type": "occurrence"}


def test_the_envelope_never_tells_the_host_what_to_say():
    """戳醒不等于该开口。信封不表达 agent 该说什么、要不要通知、用哪个模型。"""
    keys = set(event().to_dict())
    for forbidden in ("message", "text", "prompt", "notify", "model",
                      "runtime", "channel", "priority"):
        assert forbidden not in keys


# ---------------------------------------------------------------------------
# context 的白名单和上限
# ---------------------------------------------------------------------------

def test_context_keeps_only_the_keys_on_the_allow_list():
    out = safe_context({"scope": "2026-08-28", "reason": "跨过 3000",
                        "typed_value": {"整个存储 doc": "…"}, "raw_bssid": "aa:bb"})
    assert set(out) == {"scope", "reason"}


def test_an_unknown_key_is_dropped_rather_than_raising():
    """一条规则多带了个字段，不该让整个事件消失。"""
    assert safe_context({"nope": 1}) == {}


def test_a_runaway_reason_is_truncated_and_says_so():
    """`reason` 来自 evaluator，而宿主可以注册自己的 evaluator ——
    返回什么完全不受我们控制，不设上限就是让宿主能撑爆模型上下文。"""
    out = safe_context({"reason": "很长" * 5000})
    assert len(out["reason"]) <= MAX_CONTEXT_VALUE_CHARS + len("…(截断)")
    assert out["reason"].endswith("…(截断)")


def test_a_normal_reason_is_left_exactly_as_it_was():
    assert safe_context({"reason": "跨过 3000"})["reason"] == "跨过 3000"


def test_no_context_at_all_is_fine():
    assert safe_context(None) == {}


@pytest.mark.parametrize("key", ALLOWED_CONTEXT_KEYS)
def test_every_allow_listed_key_actually_survives(key):
    assert key in safe_context({key: 1})


def test_the_scheduled_rules_context_keys_are_on_the_list():
    """streak / absence 这两条时钟驱动的规则会往 context 里放自己的计数。
    忘了加进白名单的话，它们的事件会安静地少掉解释信息。"""
    assert "streak_length" in ALLOWED_CONTEXT_KEYS
    assert "silent_seconds" in ALLOWED_CONTEXT_KEYS
