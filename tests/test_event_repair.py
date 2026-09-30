"""Fact repair, deterministic replay and the durable pre-WakePort fence."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.receipt import WakeReceipt
from test_acceptance_regressions_0_9 import (
    T, RecordingWake, fired_weight, retract, weigh, weight_rule,
)


def state(storage, version=1, day=None):
    return storage.rule_state[("u", "weight", f"{day or T.date()}@v{version}")]


def gate(storage, entry, *, now=T + timedelta(hours=1)):
    start = getattr(storage, "begin_event_dispatch", None)
    assert callable(start), "StoragePort needs a durable pre-wake claim-token gate"
    return start(event_id=entry.event_id, claim_token=entry.claim_token, now=now)


def test_correction_rebuilds_before_later_crossing_and_preserves_sibling():
    s, kit = fired_weight()
    old = next(iter(s.outbox))
    weigh(kit, 69, eid="trigger", at=T + timedelta(hours=1), revision=2)
    assert (state(s)["previous_value"], state(s)["fired_in_scope"]) == (69, False)
    assert s.outbox[old].delivery_state == "invalidated"
    assert {o.source_event_id for o in s.observations.values()} == {"previous", "trigger"}
    new = weigh(kit, 73, eid="next", at=T + timedelta(hours=3))
    assert [(e.previous, e.current) for e in new.events] == [(69, 73)]
    assert len(s.outbox) == 2
    retry = weigh(kit, 69, eid="trigger", at=T + timedelta(hours=1), revision=2)
    assert retry.receipt.status == "duplicate" and len(s.outbox) == 2


def test_previous_reference_and_trigger_are_explicit_provenance():
    s, kit = fired_weight(previous_source="scale")
    entry = next(iter(s.outbox.values()))
    refs = getattr(entry, "fact_dependencies", ())
    assert {(r["role"], r["source"], r["source_event_id"]) for r in refs} == {
        ("current", "ios", "trigger"), ("previous", "scale", "previous")}
    assert all(r["fact_key"] and r["observation_id"] for r in refs)
    retract(kit, "previous", source="scale")
    repaired = s.outbox[entry.event_id]
    assert repaired.delivery_state == "invalidated"
    assert repaired.invalidated_at and repaired.invalidation_reason
    assert state(s)["previous_value"] == 72 and not state(s)["fired_in_scope"]


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 2, 0)])
def test_replay_uses_business_order_even_when_arrival_order_differs(order):
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[weight_rule()])
    for i in order:
        weigh(kit, [68, 70, 72][i], eid=f"item-{i}", at=T + timedelta(minutes=i))
    retract(kit, "item-2")
    assert (state(s)["previous_value"], state(s)["fired_in_scope"]) == (70, False)
    out = weigh(kit, 73, eid="next", at=T + timedelta(hours=3))
    assert [(e.previous, e.current) for e in out.events] == [(70, 73)]


def test_equal_time_replay_has_stable_fact_identity_tiebreaker():
    results = []
    for order in [("a", "b"), ("b", "a")]:
        s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[weight_rule()])
        for eid in order:
            weigh(kit, {"a": 68, "b": 70}[eid], eid=eid)
        weigh(kit, 72, eid="delete", at=T + timedelta(minutes=1))
        retract(kit, "delete")
        results.append(state(s))
    assert results[0] == results[1]
    assert results[0]["fired_in_scope"] is False


def test_replay_uses_archived_version_and_does_not_touch_other_scope():
    s, kit = fired_weight()
    kit.definitions = [replace(weight_rule(version=2), value=80)]
    weigh(kit, 70, eid="v2-base", at=T + timedelta(days=1))
    weigh(kit, 82, eid="v2-hit", at=T + timedelta(days=1, hours=1))
    next_day = deepcopy(state(s, version=2, day=(T + timedelta(days=1)).date()))
    retract(kit)
    assert state(s)["previous_value"] == 70 and not state(s)["fired_in_scope"]
    assert state(s, version=2, day=(T + timedelta(days=1)).date()) == next_day


def test_missing_history_is_explicit_incomplete_and_never_invents_a_crossing():
    s, kit = fired_weight()
    s.observations = {k: v for k, v in s.observations.items() if v.source_event_id != "previous"}
    retract(kit)
    assert state(s).get("completeness") == "incomplete"
    assert state(s)["previous_value"] is None and not state(s)["fired_in_scope"]
    assert not weigh(kit, 73, eid="next", at=T + timedelta(hours=3)).events


def test_started_delivery_invalidated_is_unknown_and_not_claimable():
    s, kit = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T + timedelta(hours=1), lease_seconds=60)
    assert gate(s, claim)
    retract(kit)
    entry = s.outbox[claim.event_id]
    assert entry.delivery_state == "unknown" and entry.dispatch_started_at is not None
    assert s.claim_pending_event(worker_id="w2", now=T + timedelta(days=1), lease_seconds=60) is None
    assert s.list_pending_events(subject_id="u") == []
    assert not gate(s, claim, now=T + timedelta(days=1))


def test_stale_or_repeated_token_cannot_start_delivery():
    s, _ = fired_weight()
    first = s.claim_pending_event(worker_id="w", now=T, lease_seconds=1)
    second = s.claim_pending_event(worker_id="w2", now=T + timedelta(seconds=2), lease_seconds=60)
    assert not gate(s, first, now=T + timedelta(seconds=2))
    assert gate(s, second, now=T + timedelta(seconds=2))
    assert not gate(s, second, now=T + timedelta(seconds=2))


def test_receipt_after_invalidation_preserves_delivery_audit_without_resending():
    s, kit = fired_weight()
    class RetractionDuringWake(RecordingWake):
        def wake(self, event, attempt):
            assert s.outbox[event.event_id].dispatch_started_at is not None
            retract(kit)
            assert s.outbox[event.event_id].delivery_state == "unknown"
            return super().wake(event, attempt)
    wake = RetractionDuringWake(); kit.wake = wake
    kit.dispatch_pending(worker_id="w", now=T + timedelta(hours=1))
    entry = next(iter(s.outbox.values()))
    assert entry.delivery_state == "delivered" and entry.invalidated_at
    before = deepcopy(s.receipts)
    retract(kit)
    kit.dispatch_pending(worker_id="w", now=T + timedelta(days=1))
    assert len(wake.delivered) == 1 and len(before) == 1 and s.receipts == before


def test_uncertain_exception_stays_unknown_without_fabricated_failure_receipt():
    s, kit = fired_weight()
    class UncertainWake:
        def wake(self, event, attempt):
            raise TimeoutError("request may have reached runtime")
    kit.wake = UncertainWake()
    kit.dispatch_pending(worker_id="w", now=T + timedelta(hours=1))
    assert next(iter(s.outbox.values())).delivery_state == "unknown"
    assert not s.receipts
    assert s.claim_pending_event(worker_id="w2", now=T + timedelta(days=1), lease_seconds=60) is None


def test_invalidated_started_event_cannot_be_reset_pending_by_failed_receipt():
    s, kit = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T + timedelta(hours=1), lease_seconds=60)
    assert gate(s, claim)
    retract(kit)
    receipt = WakeReceipt(claim.event_id, f"{claim.event_id}:1", "enqueue_failed", T)
    assert s.record_wake_receipt(receipt=receipt, next_state="pending", claim_token=claim.claim_token)
    assert s.outbox[claim.event_id].delivery_state == "invalidated"
    assert s.receipts == [receipt]


def test_scrub_discards_nested_and_derived_fact_data_without_numeric_replacement():
    s, kit = fired_weight(value=72.0)
    entry = next(iter(s.outbox.values()))
    snapshot = deepcopy(entry.fact_snapshot)
    snapshot["current"] = {"nested": [72, {"value": 72.0}]}
    snapshot["context"]["reason"] = "changed by two kilograms"
    snapshot["extension"] = {"values": [70, 72]}
    s.outbox[entry.event_id] = replace(entry, fact_snapshot=snapshot)
    retract(kit)
    after = s.outbox[entry.event_id].fact_snapshot
    assert after["current"] is None and after["previous"] is None
    assert "extension" not in after and "reason" not in after.get("context", {})


def test_required_event_invalidation_failure_rolls_back_entire_retraction():
    s, kit = fired_weight()
    before = deepcopy((s.retractions, s.current, s.aggregates, s.rule_state, s.outbox))
    def broken(**kwargs):
        raise NotImplementedError("adapter must implement event invalidation")
    s.scrub_event_snapshots = broken
    with pytest.raises(NotImplementedError):
        retract(kit)
    assert (s.retractions, s.current, s.aggregates, s.rule_state, s.outbox) == before


def test_cross_day_correction_establishes_new_scope_baseline():
    s, kit = fired_weight()
    weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(days=1))
    assert state(s, day=(T + timedelta(days=1)).date())["previous_value"] == 69
    out = weigh(kit, 73, eid="next-day", at=T + timedelta(days=1, hours=1))
    assert [(e.previous, e.current) for e in out.events] == [(69, 73)]


def test_missing_archived_definition_scrubs_state_and_marks_incomplete():
    s, kit = fired_weight()
    kit = PerceptionKit(s, definitions=[replace(weight_rule(version=2), value=80)])
    retract(kit)
    assert state(s)["completeness"] == "incomplete"
    assert state(s)["previous_value"] is None and not state(s)["fired_in_scope"]


def test_surviving_delivered_trigger_still_occupies_once_scope():
    s, kit = fired_weight()
    kit.wake = RecordingWake()
    kit.dispatch_pending(worker_id="w", now=T + timedelta(hours=1))
    weigh(kit, 68, eid="unrelated", at=T + timedelta(hours=2))
    retract(kit, "unrelated")
    assert state(s)["fired_in_scope"] is True
    assert state(s)["last_fired_at"] == (T + timedelta(hours=1)).isoformat()
    assert next(iter(s.outbox.values())).delivery_state == "delivered"


def test_event_resource_competes_with_retraction_and_pre_wake_start():
    from perceptkit.contracts.mutation import event_key, RetryableMutationError
    s, kit = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T + timedelta(hours=1), lease_seconds=60)
    before = deepcopy((s.retractions, s.rule_state, s.outbox))
    with s.mutation_transaction() as owner:
        owner.acquire((event_key("u", "health_weight"),))
        with pytest.raises(RetryableMutationError):
            retract(kit)
        with pytest.raises(RetryableMutationError):
            gate(s, claim)
        assert (s.retractions, s.rule_state, s.outbox) == before


def test_scheduled_event_depends_on_real_reference_and_repair_is_explicit_incomplete():
    from perceptkit.rules import EventDefinition
    absence = EventDefinition.parse({
        "id": "weight", "version": 1,
        "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": "absence", "value": 3600},
        "lifecycle": {"scope": "local_day", "fire": "once"},
        "event": {"type": "weight.absent"}})
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[absence])
    weigh(kit, 70, eid="previous")
    kit.evaluate_absence(subject_id="u", now=T + timedelta(days=1))
    entry = next(iter(s.outbox.values()))
    assert any(ref["source_event_id"] == "previous" for ref in entry.fact_dependencies)
    retract(kit, "previous")
    raw = state(s, day=(T + timedelta(days=1)).date())
    assert raw["completeness"] == "incomplete" and not raw["fired_in_scope"]
    assert s.outbox[entry.event_id].delivery_state == "invalidated"


def test_started_lease_expiry_requires_reconciliation_not_another_attempt():
    s, _ = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T, lease_seconds=1)
    assert gate(s, claim, now=T)
    assert s.claim_pending_event(worker_id="w2", now=T + timedelta(seconds=2), lease_seconds=60) is None
    assert s.outbox[claim.event_id].delivery_state == "unknown"


def test_unrelated_source_and_signal_do_not_lose_snapshot_or_receipt():
    s, kit = fired_weight()
    before = deepcopy(s.outbox)
    retract(kit, "trigger", source="other")
    assert s.outbox == before


def test_receipt_without_current_claim_token_cannot_overwrite_unknown():
    s, kit = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T, lease_seconds=60)
    assert gate(s, claim, now=T)
    retract(kit)
    receipt = WakeReceipt(claim.event_id, f"{claim.event_id}:1", "accepted", T)
    assert s.record_wake_receipt(receipt=receipt, next_state="delivered", claim_token=None) is False
    assert s.outbox[claim.event_id].delivery_state == "unknown"


def test_current_only_correction_invalidates_original_reference_without_detail():
    from perceptkit.manifest import MINIMAL_SIGNALS
    signals = dict(MINIMAL_SIGNALS)
    signals["health_weight"] = replace(signals["health_weight"], storage_mode="current_only", history_retention_days=0)
    s = InMemoryStorage(); kit = PerceptionKit(s, signals=signals, definitions=[weight_rule()])
    weigh(kit, 70, eid="previous")
    weigh(kit, 72, eid="trigger", revision=1, at=T + timedelta(hours=1))
    assert not s.observations
    result = weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(hours=1))
    assert result.applied
    assert next(iter(s.outbox.values())).delivery_state == "invalidated"
    assert state(s)["completeness"] == "incomplete"


def test_occurrence_does_not_depend_on_unrelated_previous_occurrence():
    from perceptkit.rules import EventDefinition
    rule = EventDefinition.parse({"id": "occ", "version": 1,
        "source": {"signal": "health_weight"}, "condition": {"type": "occurrence"},
        "lifecycle": {"scope": "local_day", "fire": "every"}, "event": {"type": "weight.recorded"}})
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[rule])
    weigh(kit, 70, eid="previous")
    result = weigh(kit, 72, eid="trigger", at=T + timedelta(hours=1))
    target = result.events[0].event_id
    retract(kit, "previous")
    assert s.outbox[target].delivery_state == "pending"


def test_correction_owns_all_old_and_new_rule_scopes_before_writes():
    from test_storage_concurrency_contract import AuditedStorage
    s = AuditedStorage(); kit = PerceptionKit(s)
    weigh(kit, 72, eid="trigger", revision=1)
    kit.definitions = [weight_rule()]
    out = weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(days=1))
    assert out.applied
    assert state(s)["previous_value"] is None
    assert state(s, day=(T + timedelta(days=1)).date())["previous_value"] == 69


@pytest.mark.parametrize("operation", ["retract", "correct"])
def test_rule_scope_created_between_planning_and_event_lock_forces_retry(operation):
    from contextlib import contextmanager
    from perceptkit.contracts.mutation import RetryableMutationError
    class NewlyVisibleScope(InMemoryStorage):
        armed = False
        def list_rule_states(self, **kwargs):
            rows = super().list_rule_states(**kwargs)
            if self.armed and any(k[0] == "50_events" for k in self._mutation_locks):
                rows.append(("weight", "forever@v99", {"signal": "health_weight", "previous_value": 72}))
            return rows
    s = NewlyVisibleScope(); kit = PerceptionKit(s, definitions=[weight_rule()])
    weigh(kit, 70, eid="previous")
    weigh(kit, 72, eid="trigger", revision=1, at=T + timedelta(hours=1))
    before = deepcopy((s.retractions, s.current, s.observations, s.rule_state, s.outbox))
    s.armed = True
    with pytest.raises(RetryableMutationError):
        if operation == "retract":
            retract(kit)
        else:
            weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(hours=1))
    assert (s.retractions, s.current, s.observations, s.rule_state, s.outbox) == before


def test_conformance_detects_adapter_that_allows_duplicate_dispatch_start():
    from perceptkit.conformance import run_storage_conformance
    class BrokenGate(InMemoryStorage):
        def begin_event_dispatch(self, *, event_id, claim_token, now):
            return self.outbox[event_id]
    assert any("dispatch fence" in problem for problem in run_storage_conformance(BrokenGate))


def test_conformance_detects_adapter_that_skips_invalidation():
    from perceptkit.conformance import run_storage_conformance
    class BrokenInvalidation(InMemoryStorage):
        def scrub_event_snapshots(self, **kwargs):
            return 0
    assert any("event invalidation" in problem for problem in run_storage_conformance(BrokenInvalidation))


def test_stale_claim_cannot_create_fake_delivery_audit_repeatedly():
    s, kit = fired_weight()
    claim = s.claim_pending_event(worker_id="w", now=T, lease_seconds=60)
    retract(kit)
    receipt = WakeReceipt(claim.event_id, f"{claim.event_id}:1", "accepted", T)
    for _ in range(2):
        assert s.record_wake_receipt(receipt=receipt, next_state="delivered", claim_token=claim.claim_token) is False
    assert len(s.receipts) == 1
    assert s.outbox[claim.event_id].delivery_state == "invalidated"


def test_dispatch_failed_receipt_after_invalidation_reports_terminal_not_retry():
    s, kit = fired_weight()
    class FailedDuringRetraction:
        def wake(self, event, attempt):
            retract(kit)
            return WakeReceipt(event.event_id, attempt.attempt_id, "enqueue_failed", T)
    kit.wake = FailedDuringRetraction()
    result = kit.dispatch_pending(worker_id="w", now=T)
    assert result.invalidated == list(s.outbox) and not result.retrying


def test_incomplete_replay_keeps_proven_surviving_delivered_once_trigger():
    s, kit = fired_weight()
    weigh(kit, 68, eid="unrelated", at=T + timedelta(hours=2))
    kit.wake = RecordingWake()
    kit.dispatch_pending(worker_id="w", now=T + timedelta(hours=2))
    s.observations = {key: row for key, row in s.observations.items() if row.source_event_id != "unrelated"}
    retract(kit, "unrelated")
    assert state(s)["completeness"] == "incomplete"
    assert state(s)["fired_in_scope"] is True
    assert state(s)["last_fired_at"] == (T + timedelta(hours=1)).isoformat()


def test_reclaim_rechecks_state_after_event_ownership():
    from contextlib import contextmanager
    class InvalidationWins(InMemoryStorage):
        armed = False
        @contextmanager
        def mutation_transaction(self):
            with super().mutation_transaction() as owner:
                storage = self
                class Proxy:
                    def acquire(self, keys):
                        owner.acquire(keys)
                        if storage.armed:
                            storage.armed = False
                            entry = next(iter(storage.outbox.values()))
                            storage.outbox[entry.event_id] = replace(entry, delivery_state="invalidated", invalidated_at=T)
                yield Proxy()
    s = InvalidationWins(); kit = PerceptionKit(s, definitions=[weight_rule()])
    weigh(kit, 70, eid="previous")
    weigh(kit, 72, eid="trigger", at=T + timedelta(hours=1))
    s.armed = True
    assert s.claim_pending_event(worker_id="w", now=T, lease_seconds=60) is None
    assert next(iter(s.outbox.values())).delivery_state == "invalidated"


def test_streak_with_expired_reference_invalidates_even_when_partial_refs_survive():
    from perceptkit.rules import EventDefinition
    rule = EventDefinition.parse({"id": "weight", "version": 1,
        "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": "streak", "operator": "lt", "value": 71, "params": {"periods": 2}},
        "lifecycle": {"scope": "local_day", "fire": "once"}, "event": {"type": "weight.streak"}})
    rule = replace(rule, params={"periods": 2})
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[rule])
    weigh(kit, 70, eid="first")
    weigh(kit, 69, eid="second", at=T + timedelta(days=1))
    s.observations = {key: row for key, row in s.observations.items() if row.source_event_id != "first"}
    out = kit.evaluate_daily(subject_id="u", local_date=(T + timedelta(days=1)).date(), now=T + timedelta(days=1))
    assert len(out.events) == 1
    retract(kit, "first")
    assert next(iter(s.outbox.values())).delivery_state == "invalidated"
    assert state(s, day=(T + timedelta(days=1)).date())["completeness"] == "incomplete"


def test_mismatched_external_receipt_does_not_modify_another_event():
    s, kit = fired_weight()
    entry = next(iter(s.outbox.values()))
    s.outbox["other"] = replace(entry, event_id="other", delivery_state="claimed", claim_token="w:1")
    class WrongReceipt:
        def wake(self, event, attempt):
            return WakeReceipt("other", "wrong-attempt", "accepted", T)
    kit.wake = WrongReceipt()
    result = kit.dispatch_pending(worker_id="w", now=T, limit=1)
    assert result.unknown == [entry.event_id]
    assert s.outbox["other"].delivery_state == "claimed" and not s.receipts


def test_legacy_date_recovered_from_detail_also_preowns_old_rule_scope():
    from test_storage_concurrency_contract import AuditedStorage
    s = AuditedStorage(); kit = PerceptionKit(s)
    weigh(kit, 72, eid="trigger", revision=1)
    s.identity_records = {key: replace(row, fact_key=None, effective_local_date=None, semantic_digest=None)
                          for key, row in s.identity_records.items()}
    kit.definitions = [weight_rule()]
    assert weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(days=1)).applied
    assert state(s)["previous_value"] is None
