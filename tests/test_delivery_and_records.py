"""投递状态机 + 当前值的更新判定。

这两块是"可插拔"真正的落点：状态和转移规则由 kit 定死，宿主只实现存取。
留给宿主自己发挥的话，同一个 kit 装到四个宿主上会有四种可靠性。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit.contracts import delivery, records
from perceptkit.contracts.delivery import IllegalTransition
from perceptkit.contracts.receipt import (
    WAKE_ACCEPTED,
    WAKE_DUPLICATE,
    WAKE_ENQUEUE_FAILED,
    WAKE_REJECTED,
    WAKE_SUPPRESSED,
)
from perceptkit.contracts.records import (
    CONFLICT,
    IGNORE,
    REPLACE,
    CurrentProjection,
    EventOutboxEntry,
    decide_current_update,
)
from perceptkit.ports import StoragePort, WakePort

SH = timezone(timedelta(hours=8))
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=SH)


# ---------------------------------------------------------------------------
# 端口的方法面
# ---------------------------------------------------------------------------

def test_storage_port_covers_every_method_the_spec_requires():
    """产品规范 §8 列了 17 个方法。少一个，就有一类语义没人实现。"""
    required = {
        "claim_report", "append_observation", "get_current",
        "compare_and_put_current", "list_observations", "get_aggregate",
        "put_aggregate", "upsert_calendar_events", "upsert_reminders",
        "apply_source_snapshot", "get_sync_state", "put_sync_state",
        "get_rule_state", "put_rule_state", "enqueue_event",
        "claim_pending_event", "record_wake_receipt",
    }
    assert required <= {m for m in dir(StoragePort) if not m.startswith("_")}


def test_storage_port_adds_what_the_consistency_guarantees_need():
    """规范的端口清单里没有这几个，但它自己的一致性保证离开它们没法实现：
    第 4 条（永久聚合不因重放重复累计）和第 9 条（清理不误删 dedupe 身份）
    要 identity 相关方法；第 5 条（原子边界）要 transaction；
    第 10 条（按 subject 删除）要 purge_subject。"""
    for m in ("transaction", "remember_identity", "has_seen_identity",
              "delete_observations", "purge_subject"):
        assert hasattr(StoragePort, m), m


def test_wake_port_is_deliberately_one_method():
    assert [m for m in dir(WakePort) if not m.startswith("_")] == ["wake"]


# ---------------------------------------------------------------------------
# 投递状态机
# ---------------------------------------------------------------------------

def test_an_event_can_only_be_claimed_from_pending():
    assert delivery.can_transition(delivery.PENDING, delivery.CLAIMED)
    assert not delivery.can_transition(delivery.PENDING, delivery.DELIVERED)


def test_a_claimed_event_can_go_back_to_pending():
    """两条路都合法：投递失败主动放回，或租约到期被别人接管
    （原持有者可能已经死了）。"""
    assert delivery.can_transition(delivery.CLAIMED, delivery.PENDING)


def test_terminal_states_never_move_again():
    for state in delivery.TERMINAL_STATES:
        for target in delivery.DELIVERY_STATES:
            assert not delivery.can_transition(state, target), f"{state} -> {target}"


def test_illegal_transitions_raise_rather_than_being_tolerated():
    """这类错误不该被 catch 掉当边界情况 —— 它意味着投递逻辑有 bug，
    继续走只会让状态更乱。"""
    with pytest.raises(IllegalTransition):
        delivery.assert_transition(delivery.DELIVERED, delivery.PENDING)
    with pytest.raises(IllegalTransition):
        delivery.assert_transition("made_up", delivery.PENDING)


@pytest.mark.parametrize("status,expected", [
    (WAKE_ACCEPTED, delivery.DELIVERED),
    (WAKE_DUPLICATE, delivery.DELIVERED),
    (WAKE_SUPPRESSED, delivery.SUPPRESSED),
    (WAKE_REJECTED, delivery.REJECTED),
    (WAKE_ENQUEUE_FAILED, delivery.PENDING),
])
def test_each_receipt_status_maps_to_exactly_one_next_state(status, expected):
    assert delivery.next_state_for_receipt(status, attempts_left=True) == expected


def test_duplicate_counts_as_delivered_not_as_something_to_retry():
    """runtime 认得这个 event_id，说明之前那次其实成了、只是回执没存下来。
    当成功处理，别再投第三次。"""
    assert delivery.next_state_for_receipt(
        WAKE_DUPLICATE, attempts_left=True) == delivery.DELIVERED


def test_suppressed_is_not_retried():
    """事件已经送达，是 runtime 自己选择不响应。重投等于打扰第二次。"""
    assert delivery.next_state_for_receipt(
        WAKE_SUPPRESSED, attempts_left=True) == delivery.SUPPRESSED


def test_retries_eventually_give_up_instead_of_looping_forever():
    """无限重试会让一个投不出去的事件永远占着 worker。"""
    assert delivery.next_state_for_receipt(
        WAKE_ENQUEUE_FAILED, attempts_left=False) == delivery.DEAD_LETTER


def test_only_delivery_consumes_the_cooldown_budget():
    """用户那一轮该说的话没说出去、额度却被吃掉了，是最难查的一类问题：
    没有报错、没有日志，只有"它今天怎么不说话"。"""
    assert delivery.consumes_budget(delivery.DELIVERED)
    for state in (delivery.PENDING, delivery.CLAIMED, delivery.SUPPRESSED,
                  delivery.REJECTED, delivery.DEAD_LETTER):
        assert not delivery.consumes_budget(state)


def test_attempt_identity_keeps_the_event_id_stable_across_retries():
    """event_id 变了就等于每次重试都是一个新事件，runtime 的幂等失效，
    用户被重复打扰。"""
    first = delivery.DeliveryAttempt("evt_1", "attempt_a", 1)
    second = delivery.DeliveryAttempt("evt_1", "attempt_b", 2)
    assert first.event_id == second.event_id
    assert first.attempt_id != second.attempt_id


def test_outbox_entry_refuses_a_state_outside_the_machine():
    with pytest.raises(ValueError):
        EventOutboxEntry(
            event_id="e", subject_id="u", definition_id="d", definition_version=1,
            event_type="t", occurred_at=T0, detected_at=T0, fact_snapshot={},
            delivery_state="probably_fine",
        )


def test_a_freshly_enqueued_event_starts_pending_and_is_not_terminal():
    """先落地再投递：走到这条记录被提交那一刻，事件就丢不了了。"""
    entry = EventOutboxEntry(
        event_id="e", subject_id="u", definition_id="d", definition_version=1,
        event_type="t", occurred_at=T0, detected_at=T0, fact_snapshot={},
    )
    assert entry.delivery_state == delivery.PENDING
    assert not entry.is_terminal
    assert entry.budget_reservation_id is None      # 还没占额度


# ---------------------------------------------------------------------------
# 当前值的更新判定
# ---------------------------------------------------------------------------

def _current(**over):
    base = dict(
        subject_id="u", signal="steps", dimension_key="steps",
        typed_value={"step_count": 3000}, availability="observed",
        observed_at=T0, received_at=T0, content_digest="d1",
    )
    base.update(over)
    return CurrentProjection(**base)


def test_first_value_always_lands():
    assert decide_current_update(
        new_occurred_at=T0, new_revision=None, new_digest="d1", existing=None
    ) == REPLACE


def test_a_newer_measurement_replaces_the_current_value():
    assert decide_current_update(
        new_occurred_at=T0 + timedelta(minutes=5), new_revision=None,
        new_digest="d2", existing=_current(),
    ) == REPLACE


def test_late_arriving_old_data_goes_to_history_not_to_current():
    """离线补传的旧数据该进历史，但不能把当前值改回过去。"""
    assert decide_current_update(
        new_occurred_at=T0 - timedelta(hours=1), new_revision=None,
        new_digest="d0", existing=_current(),
    ) == IGNORE


def test_a_higher_revision_at_the_same_instant_is_a_correction_and_wins():
    """这一条是产品规范的缺口：§7.3 只说"occurred_at 更新才能覆盖"，
    但 §4.2/§5.5 又要求支持 source_revision 修订 —— 组合起来，
    用户在健康 App 里改掉的误录值永远覆盖不了当前值。见 OPEN-QUESTIONS B15。"""
    assert decide_current_update(
        new_occurred_at=T0, new_revision=2, new_digest="d2",
        existing=_current(source_revision=1),
    ) == REPLACE


def test_a_stale_revision_at_the_same_instant_loses():
    assert decide_current_update(
        new_occurred_at=T0, new_revision=1, new_digest="d1",
        existing=_current(source_revision=2),
    ) == IGNORE


def test_a_plain_retransmission_is_ignored_not_rewritten():
    assert decide_current_update(
        new_occurred_at=T0, new_revision=None, new_digest="d1", existing=_current(),
    ) == IGNORE


def test_same_instant_same_revision_different_content_is_a_conflict():
    """静默挑一个覆盖，会让"到底哪份数据生效了"永远说不清。"""
    assert decide_current_update(
        new_occurred_at=T0, new_revision=None, new_digest="d2", existing=_current(),
    ) == CONFLICT


def test_missing_digests_are_treated_as_a_conflict_not_as_a_retransmission():
    """摘要缺失时不敢断言是重传——宁可让宿主显式处理，也不要静默覆盖。"""
    assert decide_current_update(
        new_occurred_at=T0, new_revision=None, new_digest=None,
        existing=_current(content_digest=None),
    ) == CONFLICT


def test_string_and_integer_revisions_are_not_compared_against_each_other():
    """不同来源的 revision 形态不同，硬比会得出无意义的顺序。"""
    assert decide_current_update(
        new_occurred_at=T0, new_revision="etag-aaa", new_digest="d2",
        existing=_current(source_revision=99),
    ) == REPLACE      # 字符串 rank 高于整数,但这只是一个稳定的约定,不是"更大"


def test_a_revision_beats_no_revision_at_the_same_instant():
    assert decide_current_update(
        new_occurred_at=T0, new_revision=1, new_digest="d2",
        existing=_current(source_revision=None),
    ) == REPLACE
