"""契约层：三态、时间戳、四个信封、可信上下文。

这些测试盯住的是**协议承诺**，不是实现细节：换一种解析写法它们该照样绿，
改掉一条协议规则它们该立刻红。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit import contracts
from perceptkit.contracts import (
    ContractError,
    IngestContext,
    IngestReceipt,
    Observation,
    ReportEnvelope,
    WakeReceipt,
    availability,
)
from perceptkit.contracts._time import TimestampError, parse_timestamp

SH = timezone(timedelta(hours=8))


def _obs(**over):
    base = {
        "signal": "steps",
        "signal_schema_version": 1,
        "occurred_at": "2026-08-27T10:30:00+08:00",
        "availability": "observed",
        "value": {"step_count": 3012},
    }
    base.update(over)
    return base


def _report(**over):
    base = {
        "schema_version": 1,
        "report_id": "report_01J",
        "producer": "ios",
        "observations": [_obs()],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 三态
# ---------------------------------------------------------------------------

def test_protocol_has_exactly_three_states():
    """协议层只有三态。多一个状态就是多一处每个消费方都可能漏判的地方。"""
    assert availability.AVAILABILITY_STATES == {"observed", "no_data", "unavailable"}


def test_zero_is_an_ordinary_observed_value_not_its_own_state():
    """零是值域里的普通取值，不是一种观测结果。"""
    obs = Observation.parse(_obs(value={"step_count": 0}))
    assert obs.availability == availability.OBSERVED
    assert obs.is_observed and obs.updates_current and obs.enters_trend


def test_legacy_four_state_vocabulary_still_maps_in():
    """旧宿主发四态过来不该炸——平滑过渡，不是硬切。"""
    assert availability.normalize("observed_zero") == availability.OBSERVED
    assert availability.normalize("no_observation") == availability.NO_DATA


def test_unknown_state_degrades_to_unavailable_not_observed():
    """看不懂的状态宁可当"现在没有"，也不能当有效观测混进趋势。"""
    assert availability.normalize("weird_new_state") == availability.UNAVAILABLE


def test_no_data_is_not_zero():
    """没戴表 ≠ 睡了 0 分钟。这条错了，十四天里两天缺数据就能把均值拉垮。"""
    obs = Observation.parse(_obs(availability="no_data", value=None))
    assert not obs.enters_trend
    assert not obs.updates_current


def test_unavailable_does_not_overwrite_the_last_reliable_value():
    obs = Observation.parse(_obs(availability="unavailable", value=None))
    assert not obs.updates_current


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "2026-08-27T10:30:00+08:00",
    "2026-08-27T10:30:00Z",                 # 3.10 的 fromisoformat 不认，得自己兜
    "2026-08-27T10:30:00z",
    "2026-08-27T10:30:00.123456+08:00",
    "2026-08-27T10:30+08:00",               # 少写秒
    "2026-08-27 10:30:00+08:00",            # 空格分隔
])
def test_accepts_the_iso8601_shapes_real_producers_send(raw):
    assert parse_timestamp(raw).tzinfo is not None


@pytest.mark.parametrize("raw", [
    "2026-08-27T10:30:00",      # 没有偏移
    "2026-08-27",
    "not a timestamp",
    "",
    None,
    12345,
])
def test_rejects_anything_without_a_usable_offset(raw):
    """naive 时间戳在这套系统里没有意义——归属到哪一天只能靠猜，猜错静默写进历史。"""
    with pytest.raises(TimestampError):
        parse_timestamp(raw)


def test_naive_datetime_object_is_rejected_too():
    with pytest.raises(TimestampError):
        parse_timestamp(datetime(2026, 8, 27, 10, 30))


# ---------------------------------------------------------------------------
# ReportEnvelope
# ---------------------------------------------------------------------------

def test_parses_a_well_formed_report():
    env = ReportEnvelope.parse(_report())
    assert env.report_id == "report_01J"
    assert env.producer == "ios"
    assert len(env.observations) == 1
    assert list(env.signals()) == ["steps"]


def test_empty_observation_list_is_valid():
    """"我还活着，这轮没有新东西"也是有效信息，不该当错误。"""
    assert ReportEnvelope.parse(_report(observations=[])).observations == ()


def test_unknown_schema_version_is_refused_outright():
    """猜出来的语义会静默污染历史，比拒收贵得多。"""
    with pytest.raises(ContractError) as exc:
        ReportEnvelope.parse(_report(schema_version=99))
    assert "99" in str(exc.value)


def test_unknown_top_level_fields_are_kept_not_rejected():
    """向前兼容：producer 可以先发新字段，宿主后升级，两边不用同步发版。"""
    env = ReportEnvelope.parse(_report(battery_hint="low"))
    assert env.extensions == {"battery_hint": "low"}


def test_all_field_errors_are_reported_at_once():
    """逐个试错要往返很多次；adapter 该一次拿到完整清单。"""
    with pytest.raises(ContractError) as exc:
        ReportEnvelope.parse({"schema_version": 1, "observations": []})
    assert len(exc.value.errors) == 2          # report_id + producer
    assert any("report_id" in e for e in exc.value.errors)
    assert any("producer" in e for e in exc.value.errors)


def test_observation_errors_are_reported_with_their_index():
    with pytest.raises(ContractError) as exc:
        ReportEnvelope.parse(_report(observations=[_obs(), _obs(signal="")]))
    assert any(e.startswith("observations[1].signal") for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

def test_observed_without_a_value_is_rejected():
    """说"我观测到了"却不说观测到什么，是矛盾的。"""
    with pytest.raises(ContractError):
        Observation.parse(_obs(value=None))


def test_empty_value_object_is_allowed_when_observed():
    """有些信号的 payload 本来就是空的；`{}` 和 None 不是一回事。"""
    assert Observation.parse(_obs(value={})).is_observed


def test_source_event_id_and_revision_survive_parsing():
    obs = Observation.parse(_obs(source_event_id="hk-A1B2", source_revision=3))
    assert obs.source_event_id == "hk-A1B2"
    assert obs.source_revision == 3


def test_timezone_is_optional_but_must_be_a_real_string_when_present():
    assert Observation.parse(_obs(timezone="Asia/Shanghai")).timezone == "Asia/Shanghai"
    with pytest.raises(ContractError):
        Observation.parse(_obs(timezone="  "))


def test_unknown_signal_specific_fields_land_in_extensions():
    obs = Observation.parse(_obs(quality="estimated"))
    assert obs.extensions == {"quality": "estimated"}


# ---------------------------------------------------------------------------
# IngestContext —— 绝不能从信封里读的东西
# ---------------------------------------------------------------------------

def test_context_requires_a_subject():
    """信客户端自报身份 = 任何人都能往别人账号里写观测。"""
    with pytest.raises(ContractError):
        IngestContext(subject_id="", received_at=datetime.now(SH))


def test_context_rejects_a_naive_clock():
    with pytest.raises(TimestampError):
        IngestContext(subject_id="user_1", received_at=datetime(2026, 8, 27, 10, 30))


def test_auth_scope_none_means_unrestricted_but_empty_means_nothing():
    """`None`(宿主已把过关)和 `frozenset()`(权限全关)是两回事，不能混。"""
    now = datetime.now(SH)
    assert IngestContext("u", now, None).allows("health_sleep")
    assert not IngestContext("u", now, frozenset()).allows("health_sleep")
    assert IngestContext("u", now, frozenset({"steps"})).allows("steps")


# ---------------------------------------------------------------------------
# 回执
# ---------------------------------------------------------------------------

def test_only_accepted_consumes_the_wake_budget():
    """被压制/入队失败却把额度吃掉了，是最难查的一类问题。"""
    now = datetime.now(SH)
    def receipt(status):
        return WakeReceipt("evt_1", "attempt_1", status, now)

    assert receipt(contracts.WAKE_ACCEPTED).consumes_budget
    for status in (contracts.WAKE_DUPLICATE, contracts.WAKE_SUPPRESSED,
                   contracts.WAKE_ENQUEUE_FAILED, contracts.WAKE_REJECTED):
        assert not receipt(status).consumes_budget


def test_only_enqueue_failure_is_retried():
    """`conversation_suppressed` 是"送到了但 runtime 不想响应"，重投等于打扰第二次。"""
    now = datetime.now(SH)
    assert WakeReceipt("e", "a", contracts.WAKE_ENQUEUE_FAILED, now).should_retry
    for status in (contracts.WAKE_ACCEPTED, contracts.WAKE_DUPLICATE,
                   contracts.WAKE_SUPPRESSED, contracts.WAKE_REJECTED):
        assert not WakeReceipt("e", "a", status, now).should_retry


def test_receipts_refuse_statuses_outside_the_vocabulary():
    now = datetime.now(SH)
    with pytest.raises(ContractError):
        WakeReceipt("e", "a", "kind_of_worked", now)
    with pytest.raises(ContractError):
        IngestReceipt("u", "ios", "r", "digest", now, "kind_of_worked")


def test_ingest_receipt_carries_the_identity_that_makes_retries_idempotent():
    now = datetime.now(SH)
    r = IngestReceipt("user_1", "ios", "report_9", "sha256:abc", now,
                      contracts.INGEST_DUPLICATE)
    assert (r.subject_id, r.producer, r.report_id) == ("user_1", "ios", "report_9")
    assert r.observations_applied == 0
