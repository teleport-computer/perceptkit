"""D02: immutable reports, stable Facts, and persisted migration evidence."""
from copy import deepcopy
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.errors import ContractError
from test_acceptance_regressions_0_9 import (
    T, DAY, aggregate, ingest, observation, weight_rule, v080_storage,
)


def test_report_map_order_and_transport_diagnostics_do_not_change_semantics():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"workout_type": "running", "duration_minutes": 30},
                         signal="health_workout")
    payload = dict(schema_version=1, producer="ios", report_id="r",
                   observations=[sample], debug_trace="first", reported_at=T.isoformat())
    assert kit.ingest(payload, context=IngestContext("u", T)).receipt.status == "accepted"
    payload["observations"] = [dict(reversed(list(sample.items())))]
    payload["observations"][0]["value"] = dict(reversed(list(sample["value"].items())))
    payload.update(debug_trace="retry", reported_at=(T + timedelta(hours=1)).isoformat())
    out = kit.ingest(payload, context=IngestContext("u", T + timedelta(hours=1)))
    assert out.receipt.status == "duplicate"
    assert len(storage.observations) == 1
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 30


@pytest.mark.parametrize("change", [
    {"value": {"weight_kg": 71}}, {"units": {"weight_kg": "lb"}},
    {"signal_schema_version": 2}, {"availability": "unavailable"},
    {"source_event_id": "other"}, {"occurred_at": (T + timedelta(minutes=1)).isoformat()},
])
def test_report_semantic_changes_never_apply_any_fact(change):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample])
    before = deepcopy((storage.observations, storage.current, storage.aggregates))
    out = ingest(kit, [dict(sample, **change)])
    assert out.receipt.status == "conflict"
    assert out.receipt.error_code is not None
    assert not out.ok
    assert (storage.observations, storage.current, storage.aggregates) == before


def test_unit_only_report_change_is_a_conflict():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, units={"weight_kg": "kg"})
    ingest(kit, [sample])
    out = ingest(kit, [dict(sample, units={"weight_kg": "lb"})])
    assert out.receipt.status == "conflict"


def test_correction_replaces_one_workout_and_preserves_old_batch_sibling():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    first = observation({"workout_type": "running", "duration_minutes": 30},
                        signal="health_workout", eid="A", revision=1)
    sibling = observation({"workout_type": "running", "duration_minutes": 45},
                          signal="health_workout", eid="B", revision=1,
                          at=T + timedelta(minutes=1))
    ingest(kit, [first, sibling], report_id="batch")
    out = ingest(kit, [dict(first, source_revision=2,
                           value={"workout_type": "running", "duration_minutes": 20})],
                 report_id="correction")
    assert len(out.applied) == 1
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 65
    assert aggregate(storage, "health_workout").source_coverage["observations"] == 2
    kit.recompute_aggregates(subject_id="u", signal="health_workout", start=DAY, end=DAY, now=T)
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 65
    assert any(row.source_event_id == "B" for row in storage.observations.values())


@pytest.mark.parametrize("expire_details", [False, True])
@pytest.mark.parametrize("change", [
    {"value": {"weight_kg": 75}}, {"timezone": "Asia/Shanghai"},
    {"units": {"weight_kg": "lb"}},
    {"occurred_at": (T + timedelta(minutes=1)).isoformat()},
])
def test_same_fact_revision_conflict_never_advances_any_projection(expire_details, change):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample])
    # Move Current elsewhere: revision authority cannot be inferred from Current.
    ingest(kit, [observation({"weight_kg": 69}, eid="B", at=T + timedelta(minutes=2))],
           report_id="other")
    if expire_details:
        storage.delete_observations(subject_id="u")
    before = deepcopy((storage.observations, storage.current, storage.aggregates,
                       storage.rule_state, storage.outbox))
    out = ingest(PerceptionKit(storage, definitions=[weight_rule()]),
                 [dict(sample, **change)], report_id="conflicting")
    assert len(out.conflicts) == 1 and not out.applied and not out.duplicates
    assert (storage.observations, storage.current, storage.aggregates,
            storage.rule_state, storage.outbox) == before


def test_identical_fact_revision_under_new_report_is_duplicate_after_details_expire():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [sample])
    storage.delete_observations(subject_id="u")
    out = ingest(PerceptionKit(storage), [sample], report_id="retry")
    assert len(out.duplicates) == 1 and not out.applied


@pytest.mark.parametrize("v080_storage", ["details_present", "details_expired"], indirect=True)
def test_legacy_replay_uses_persisted_evidence_and_remains_durable(v080_storage):
    storage, payload = v080_storage
    payload = deepcopy(payload)
    payload["report_id"] = "first-migration-replay"
    payload["observations"][0]["occurred_at"] = (T + timedelta(hours=1)).isoformat()
    out = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T + timedelta(hours=1)))
    assert len(out.duplicates) == 1 and not out.applied
    storage.delete_observations(subject_id="u")
    storage.current.clear()
    payload["report_id"] = "second-migration-replay"
    payload["observations"][0]["occurred_at"] = (T + timedelta(hours=2)).isoformat()
    again = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T + timedelta(hours=2)))
    assert len(again.duplicates) == 1 and not again.applied
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 30


@pytest.mark.parametrize("v080_storage", ["details_expired"], indirect=True)
def test_unmapped_legacy_changed_timestamp_replay_exposes_migration_gap(v080_storage):
    storage, payload = v080_storage
    storage.current.clear()
    payload = deepcopy(payload)
    payload["report_id"] = "unknown-legacy"
    payload["observations"][0]["occurred_at"] = (T + timedelta(hours=1)).isoformat()
    out = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T + timedelta(hours=1)))
    # With only an opaque hash, this is observationally indistinguishable from
    # a new Fact. It must not lock the whole source. Hosts must complete legacy
    # evidence backfill before claiming arbitrary changed-timestamp replay safety.
    assert any("changed_timestamp_replay_unverifiable" in w for w in out.warnings)


def test_revision_that_moves_day_removes_the_old_days_contribution():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"workout_type": "running", "duration_minutes": 30},
                         signal="health_workout", revision=1)
    ingest(kit, [sample])
    tomorrow = T + timedelta(days=1)
    out = ingest(kit, [dict(sample, source_revision=2, occurred_at=tomorrow.isoformat())],
                 report_id="moved", at=tomorrow)
    assert len(out.applied) == 1
    assert aggregate(storage, "health_workout").source_coverage["observations"] == 0
    newer = storage.get_aggregate(subject_id="u", signal="health_workout",
                                 start_date=tomorrow.date(), end_date=tomorrow.date())[0]
    assert newer.typed_aggregate["duration_minutes"]["total"] == 30


def test_correction_cannot_rebuild_a_day_with_an_expired_sibling():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"workout_type": "running", "duration_minutes": 30},
                         signal="health_workout", eid="A", revision=1)
    sibling = dict(sample, source_event_id="B", occurred_at=(T - timedelta(minutes=1)).isoformat())
    ingest(kit, [sibling, sample])
    storage.delete_observations(subject_id="u", before=T)
    out = ingest(kit, [dict(sample, source_revision=2,
                           value={"workout_type": "running", "duration_minutes": 20})],
                 report_id="correction")
    assert not out.applied and out.rejected
    assert "incomplete" in str(out.rejected)
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 60


def test_correction_can_move_current_fact_back_before_a_sibling():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    older = observation({"weight_kg": 69}, eid="B", at=T - timedelta(minutes=1), revision=1)
    sample = observation({"weight_kg": 70}, revision=1)
    ingest(kit, [older, sample])
    out = ingest(kit, [dict(sample, source_revision=2, value={"weight_kg": 68},
                           occurred_at=(T - timedelta(minutes=2)).isoformat())], report_id="corrected")
    assert len(out.applied) == 1
    assert kit.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"].value == {"weight_kg": 69}


def test_unseen_older_revision_cannot_restore_an_outdated_fact():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"workout_type": "running", "duration_minutes": 45},
                         signal="health_workout", revision=2)
    ingest(kit, [sample])
    out = ingest(kit, [dict(sample, source_revision=1,
                           value={"workout_type": "running", "duration_minutes": 30})], report_id="late")
    assert not out.applied and out.rejected
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 45


def test_semantic_digest_never_hashes_python_repr_for_non_wire_content():
    storage = InMemoryStorage()
    class Arbitrary:
        def __str__(self):
            return "same-as-some-other-object"
    with pytest.raises(ContractError):
        ingest(PerceptionKit(storage), [observation({"weight_kg": 70, "unknown": Arbitrary()})])
    assert not storage.reports and not storage.observations


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_old_report_receipt_without_semantic_evidence_is_a_typed_conflict(v080_storage):
    storage, payload = v080_storage
    before = deepcopy((storage.reports, storage.observations, storage.current, storage.aggregates))
    out = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T))
    assert out.receipt.status == "conflict" and not out.ok
    assert out.receipt.error_code == "legacy_report_semantics_unverifiable"
    assert (storage.reports, storage.observations, storage.current, storage.aggregates) == before


def test_fact_revision_authority_rolls_back_with_failed_projection(monkeypatch):
    from perceptkit.contracts.errors import RetryableProjectionError
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    write = storage.compare_and_put_current
    monkeypatch.setattr(storage, "compare_and_put_current", lambda *args, **kwargs: False)
    with pytest.raises(RetryableProjectionError):
        ingest(kit, [sample])
    assert not storage.identity_records and not storage.identities
    monkeypatch.setattr(storage, "compare_and_put_current", write)
    assert len(ingest(kit, [sample]).applied) == 1


def test_purge_subject_removes_fact_authority_without_affecting_another_subject():
    storage = InMemoryStorage()
    sample = observation({"weight_kg": 70}, revision=1)
    payload = dict(schema_version=1, producer="ios", report_id="r", observations=[sample])
    kit = PerceptionKit(storage)
    for subject in ("u", "other"):
        kit.ingest(payload, context=IngestContext(subject, T))
    storage.purge_subject(subject_id="u")
    assert {row.subject_id for row in storage.identity_records.values()} == {"other"}
    assert len(kit.ingest(payload, context=IngestContext("u", T)).applied) == 1
