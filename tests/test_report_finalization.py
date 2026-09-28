"""Task 3 review: retries preserve truth; batch order cannot choose a conflict winner."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from test_acceptance_regressions_0_9 import T, ingest, observation, weight_rule, v080_storage


@pytest.mark.parametrize("kind", ["fact_conflict", "correction_incomplete", "legacy_conflict"])
def test_report_retry_preserves_failed_fact_outcome(kind):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample], report_id="original")
    changed = dict(sample, value={"weight_kg": 72})
    if kind == "correction_incomplete":
        storage.delete_observations(subject_id="u")
        changed["source_revision"] = 2
    elif kind == "legacy_conflict":
        # Simulate a host upgrading while only released identity+Current remain.
        storage.identity_records.clear()
        storage.delete_observations(subject_id="u")
    first = ingest(kit, [changed], report_id="failed")
    before = deepcopy((storage.observations, storage.current, storage.aggregates,
                       storage.rule_state, storage.outbox))
    second = ingest(PerceptionKit(storage), [changed], report_id="failed")
    assert not first.ok and not second.ok
    assert first.receipt.status == "accepted"
    assert second.receipt.status == "duplicate"
    assert first.receipt.error_code is second.receipt.error_code is None
    assert first.receipt.observations_rejected
    assert second.receipt.observations_rejected == first.receipt.observations_rejected
    assert second.rejected == [(item.index, item.problems)
                               for item in first.receipt.observations_rejected]
    assert not second.applied and not second.duplicates
    assert (storage.observations, storage.current, storage.aggregates,
            storage.rule_state, storage.outbox) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_intra_report_conflict_is_preflighted_before_any_fact_write(reverse):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    a = observation({"weight_kg": 70}, eid="A", revision=1)
    conflict = dict(a, value={"weight_kg": 72})
    sibling = observation({"weight_kg": 65}, eid="B", revision=1,
                          at=T + timedelta(minutes=1))
    batch = [a, sibling, conflict]
    if reverse:
        batch.reverse()
    out = ingest(kit, batch)
    assert not out.ok and len(out.conflicts) == 2
    assert [item.stored.source_event_id for item in out.applied] == ["B"]
    assert {row.source_event_id for row in storage.observations.values()} == {"B"}
    assert all(row.source_event_id == "B" for row in storage.current.values())
    assert all(row.source_coverage["observations"] == 1 for row in storage.aggregates.values())
    assert not storage.outbox
    assert all(state["previous_value"] == 65 for state in storage.rule_state.values())


@pytest.mark.parametrize("v080_storage", ["details_expired"], indirect=True)
def test_opaque_old_identity_does_not_block_an_unrelated_new_fact(v080_storage):
    storage, payload = v080_storage
    storage.current.clear()
    payload = deepcopy(payload)
    payload["report_id"] = "new-fact"
    payload["observations"][0]["source_event_id"] = "brand-new-workout"
    out = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T))
    assert out.ok and len(out.applied) == 1


@pytest.mark.parametrize("v080_storage", ["details_expired"], indirect=True)
def test_exact_released_legacy_digest_replay_is_duplicate_without_detail(v080_storage):
    storage, payload = v080_storage
    storage.current.clear()
    payload = deepcopy(payload)
    payload["report_id"] = "exact-replay"
    kit = PerceptionKit(storage)
    first = kit.ingest(payload, context=IngestContext("u", T))
    second = kit.ingest(payload, context=IngestContext("u", T))
    assert first.ok and len(first.duplicates) == 1 and not first.applied
    assert second.ok and second.receipt.status == "duplicate"
    assert not storage.observations


def test_report_observation_order_is_semantic_for_legal_ordered_observations():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    samples = [observation({"weight_kg": 70}, eid="A"),
               observation({"weight_kg": 72}, eid="B", at=T + timedelta(minutes=1))]
    assert ingest(kit, samples).ok
    before = deepcopy(storage.current)
    out = ingest(kit, list(reversed(samples)))
    assert out.receipt.status == "conflict" and not out.ok
    assert storage.current == before


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_old_report_failure_has_stable_action_and_new_id_recovery(v080_storage):
    storage, payload = v080_storage
    kit = PerceptionKit(storage)
    for _ in range(2):
        out = kit.ingest(payload, context=IngestContext("u", T))
        assert not out.ok and out.receipt.status == "conflict"
        assert out.receipt.error_code == "legacy_report_semantics_unverifiable"
        assert out.receipt.recovery_action == "migrate_original_envelope_or_use_new_report_id"
        assert not out.receipt.retryable
    out = kit.ingest(dict(payload, report_id="safe-recovery"), context=IngestContext("u", T))
    assert out.ok and len(out.duplicates) == 1 and not out.applied


def test_receipt_backfill_requires_matching_original_digest_and_is_idempotent():
    storage = InMemoryStorage()
    old = storage.claim_report(subject_id="u", producer="ios", report_id="r",
                               payload_digest="old", received_at=T)
    method = getattr(storage, "backfill_report_digest", None)
    assert callable(method), "host needs an explicit guarded receipt migration port"
    args = dict(subject_id="u", producer="ios", report_id="r", payload_digest="v2:new")
    assert not method(**args, expected_digest="wrong")
    assert storage.reports[("u", "ios", "r")] == old
    assert method(**args, expected_digest="old")
    assert method(**args, expected_digest="old")
    assert not method(**dict(args, payload_digest="v2:other"), expected_digest="old")
    assert not method(**dict(args, payload_digest="v2:other"), expected_digest="v2:new")
    assert storage.claim_report(**args, received_at=T).status == "duplicate"


def test_correction_completeness_uses_fact_evidence_not_observation_id_spelling():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample])
    old = next(iter(storage.observations.values()))
    storage.observations.clear()
    storage.append_observation(replace(old, observation_id="host-assigned-observation-id"))
    out = ingest(kit, [dict(sample, value={"weight_kg": 71}, source_revision=2)], report_id="correction")
    assert len(out.applied) == 1 and not out.rejected


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_legacy_backfill_uses_fact_evidence_with_custom_observation_id(v080_storage):
    storage, payload = v080_storage
    old = next(iter(storage.observations.values()))
    storage.observations.clear()
    storage.current.clear()
    storage.append_observation(replace(old, observation_id="custom-legacy-id"))
    payload = deepcopy(payload)
    payload["report_id"] = "custom-id-replay"
    payload["observations"][0]["occurred_at"] = (T + timedelta(hours=1)).isoformat()
    out = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T + timedelta(hours=1)))
    assert len(out.duplicates) == 1 and not out.applied


def test_incomplete_report_stays_terminal_after_restore_and_new_id_can_apply():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample], report_id="original")
    old = next(iter(storage.observations.values()))
    storage.delete_observations(subject_id="u")
    correction = dict(sample, value={"weight_kg": 71}, source_revision=2)
    failed = ingest(kit, [correction], report_id="failed")
    assert failed.receipt.recovery_action == "restore_fact_evidence_and_use_new_report_id"
    storage.append_observation(old)
    assert not ingest(kit, [correction], report_id="failed").ok
    recovered = ingest(kit, [correction], report_id="recovered")
    assert recovered.ok and len(recovered.applied) == 1


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_host_with_original_envelope_can_backfill_then_replay_same_old_id(v080_storage):
    from perceptkit.contracts.report import ReportEnvelope
    from perceptkit.processing.pipeline import _batch_digest
    storage, original_envelope = v080_storage
    key = ("u", "ios", original_envelope["report_id"])
    old = storage.reports[key]
    assert storage.backfill_report_digest(
        subject_id="u", producer="ios", report_id=original_envelope["report_id"],
        expected_digest=old.payload_digest,
        payload_digest=_batch_digest(ReportEnvelope.parse(original_envelope)))
    out = PerceptionKit(storage).ingest(original_envelope, context=IngestContext("u", T))
    assert out.ok and out.receipt.status == "duplicate" and not out.applied
    assert len(storage.observations) == 1
