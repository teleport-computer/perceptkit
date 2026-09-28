"""D07–D09: durable quarantined candidates and canonical attribution."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from test_acceptance_regressions_0_9 import T, ingest, observation, weigh, weight_rule


def records(kit, **kw):
    return kit.list_conflicts(subject_id="u", **kw)


def candidate(kit, value=150, revision=1, report_id="candidate", **kw):
    return ingest(kit, [observation({"weight_kg": value}, eid="jump", revision=revision,
                                   at=T + timedelta(hours=1), **kw)],
                  report_id=report_id, at=T + timedelta(hours=1))


def prepared(storage=None):
    storage = storage or InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    weigh(kit, 70)
    return storage, kit


def test_pending_conflict_durable_stable_and_has_no_fact_side_effects():
    storage, kit = prepared()
    before = deepcopy((storage.observations, storage.identities, storage.current,
                       storage.aggregates, storage.rule_state, storage.outbox))
    out = candidate(kit)
    assert out.conflicts and not out.applied and not out.rejected
    first = records(kit)
    assert len(first) == 1 and first[0].status == "pending"
    assert first[0].kind == "relative_jump"
    assert first[0].candidate.typed_value == {"weight_kg": 150}
    retry = candidate(PerceptionKit(storage), report_id="new-report")
    assert retry.conflicts and not retry.applied and not retry.duplicates
    assert records(kit) == first
    assert (storage.observations, storage.identities, storage.current,
            storage.aggregates, storage.rule_state, storage.outbox) == before


def test_higher_safe_revision_resolves_with_audit_and_idempotency():
    storage, kit = prepared()
    candidate(kit)
    original = records(kit)[0]
    out = candidate(kit, 71, revision=2, report_id="correction")
    assert len(out.applied) == 1 and not out.conflicts
    resolved = records(kit)[0]
    assert resolved.status == "resolved"
    assert resolved.conflict_id == original.conflict_id
    assert resolved.candidate == original.candidate
    assert resolved.resolution_revision == 2
    assert resolved.resolution_semantic_digest == out.applied[0].semantic_digest
    assert resolved.resolved_at is not None
    retry = candidate(kit, 71, revision=2, report_id="correction-retry")
    assert retry.duplicates and records(kit) == [resolved]


@pytest.mark.parametrize("revision", [1, 0, "opaque"])
def test_not_strictly_higher_revision_cannot_apply_or_resolve_pending(revision):
    storage, kit = prepared()
    candidate(kit)
    out = candidate(kit, 71, revision=revision, report_id="unsafe-correction")
    assert out.conflicts and not out.applied
    assert all(r.status == "pending" for r in records(kit))
    assert len(storage.observations) == 1


def test_identity_content_conflict_is_durable_and_same_batch_conflicts_are_durable():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    candidate(kit, 70)
    out = candidate(kit, 71, report_id="collision")
    assert out.conflicts and len(records(kit)) == 1
    first = records(kit)
    candidate(kit, 71, report_id="collision-again")
    assert records(kit) == first
    sibling = PerceptionKit(InMemoryStorage())
    out = ingest(sibling, [observation({"weight_kg": 70}, revision=1),
                           observation({"weight_kg": 71}, revision=1)])
    assert len(out.conflicts) == len(records(sibling)) == 2


def test_pending_conflict_does_not_relabel_retry_of_previously_applied_revision():
    kit = PerceptionKit(InMemoryStorage())
    candidate(kit, 70)
    candidate(kit, 71, report_id="bad-same-revision")
    out = candidate(kit, 70, report_id="valid-original-retry")
    assert out.duplicates and not out.conflicts and not out.applied
    assert len(records(kit, status="pending")) == 1


def test_conflict_mutations_share_canonical_fact_owner():
    from perceptkit.contracts.mutation import fact_key
    class Owned(InMemoryStorage):
        def put_conflict(self, record):
            key = fact_key(record.subject_id, record.signal, record.source, record.candidate.source_event_id)
            assert self.transaction_depth and key in self._mutation_locks
            return super().put_conflict(record)
        def resolve_conflict(self, **kw):
            record = self.conflicts[(kw["subject_id"], kw["conflict_id"])]
            key = fact_key(record.subject_id, record.signal, record.source, record.candidate.source_event_id)
            assert self.transaction_depth and key in self._mutation_locks
            return super().resolve_conflict(**kw)
    storage, kit = prepared(Owned())
    candidate(kit)
    assert candidate(kit, 71, revision=2, report_id="fix").applied


def test_converted_event_and_aggregate_use_canonical_value():
    storage, kit = prepared()
    out = candidate(kit, 160, units={"weight_kg": "lb"})
    canonical = pytest.approx(72.5747792)
    assert out.events[0].current == canonical
    assert next(iter(storage.aggregates.values())).typed_aggregate["weight_kg"] == canonical


@pytest.mark.parametrize("resolving", [False, True])
def test_final_report_failure_rolls_back_insert_or_resolution(resolving):
    class FailFinalize(InMemoryStorage):
        fail = False
        def finalize_report(self, receipt):
            super().finalize_report(receipt)
            if self.fail:
                raise RuntimeError("final write failed")
    storage, kit = prepared(FailFinalize())
    if resolving:
        candidate(kit)
    before = deepcopy(storage.__dict__)
    storage.fail = True
    with pytest.raises(RuntimeError, match="final write"):
        candidate(kit, 71 if resolving else 150, revision=2 if resolving else 1,
                  report_id="failing")
    for key in ("conflicts", "reports", "observations", "identities", "identity_records",
                "current", "aggregates", "rule_state", "outbox"):
        assert getattr(storage, key) == before[key], key


def test_conflict_query_is_scoped_filterable_deterministic_exported_and_purged():
    storage, kit = prepared()
    candidate(kit)
    row = records(kit)[0]
    storage.put_conflict(replace(row, subject_id="other", conflict_id="other"))
    assert records(kit, status="pending", signal="health_weight") == [row]
    assert records(kit, status="resolved") == []
    assert records(kit, signal="health_height") == []
    exported = kit.export_subject(subject_id="u")
    assert [r["conflict_id"] for r in exported["conflicts"]] == [row.conflict_id]
    assert storage.purge_subject(subject_id="u")["conflicts"] == 1
    assert not records(kit)
    assert len(storage.list_conflicts(subject_id="other")) == 1


@pytest.mark.parametrize("units", [{"weight_kg": "lb"}, {"weight_kg": "g"},
                                   {"weight_kg": "kg"}, {}])
def test_units_canonical_values_and_source_evidence(units):
    value = {"lb": 154, "g": 70000}.get(units.get("weight_kg"), 70)
    storage, kit = prepared()
    out = candidate(kit, value, units=units)
    assert out.applied and not out.conflicts
    stored = out.applied[0].stored
    assert stored.typed_value["weight_kg"] == pytest.approx(69.85322498 if value == 154 else 70)
    assert stored.source_units == units
    assert stored.source_values == ({"weight_kg": value} if units else {})
    assert storage.current[("u", "health_weight", "health_weight")].typed_value == stored.typed_value


@pytest.mark.parametrize("units", [None, [], "lb", {"weight_kg": []},
                                   {"unknown": "lb"}, {"weight_kg": "stone"}])
def test_malformed_units_reject_only_observation(units):
    kit = PerceptionKit(InMemoryStorage())
    out = ingest(kit, [observation({"weight_kg": 70}, units=units),
                      observation({"weight_kg": 71}, eid="valid")])
    assert len(out.rejected) == 1 and len(out.applied) == 1
    assert "invalid_units" in str(out.rejected)


def test_unit_on_non_numeric_field_is_rejected():
    kit = PerceptionKit(InMemoryStorage())
    out = ingest(kit, [observation({"state": "core", "duration_minutes": 20},
                                  signal="health_sleep", units={"state": "kg"})])
    assert "invalid_units" in str(out.rejected)


def test_conversion_precedes_range_and_jump_and_conflict_audit_keeps_wire_value():
    storage, kit = prepared()
    jump = candidate(kit, 330, units={"weight_kg": "lb"})
    assert jump.conflicts and not jump.rejected
    row = records(kit)[0]
    assert row.candidate.source_values == {"weight_kg": 330}
    assert row.candidate.source_units == {"weight_kg": "lb"}
    assert row.candidate.typed_value["weight_kg"] == pytest.approx(149.6854821)
    out = candidate(kit, 1000000, revision=2, report_id="range", units={"weight_kg": "g"})
    assert out.rejected and not out.applied


@pytest.mark.parametrize("zone", ["Invalid/Nowhere", "+08:00", "", " UTC ", 3, None])
def test_invalid_explicit_timezone_never_falls_back_or_poison_batch(zone):
    kit = PerceptionKit(InMemoryStorage(), timezone_fallback="UTC")
    out = ingest(kit, [observation({"weight_kg": 70}, source_timezone=zone),
                      observation({"weight_kg": 71}, eid="valid")])
    assert len(out.rejected) == 1 and len(out.applied) == 1
    assert "invalid_timezone" in str(out.rejected)


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_valid_zone_preserves_attribution_source(zone):
    kit = PerceptionKit(InMemoryStorage())
    out = candidate(kit, 70, source_timezone=zone)
    assert out.applied[0].stored.timezone == zone
    assert out.applied[0].stored.timezone_source == "observation"


def test_invalid_null_timezone_report_does_not_claim_omitted_timezone_semantics():
    kit = PerceptionKit(InMemoryStorage(), timezone_fallback="UTC")
    explicit_null = observation({"weight_kg": 70}, source_timezone=None)
    out = ingest(kit, [explicit_null])
    assert out.rejected
    del explicit_null["timezone"]
    retry = ingest(kit, [explicit_null])
    assert retry.receipt.status == "conflict"


@pytest.mark.parametrize("fallback,quality", [("America/New_York", "host_fallback"), (None, "missing")])
def test_missing_timezone_records_fallback_quality(fallback, quality):
    kit = PerceptionKit(InMemoryStorage(), timezone_fallback=fallback)
    wire = observation({"weight_kg": 70})
    del wire["timezone"]
    out = ingest(kit, [wire])
    assert out.applied[0].stored.timezone_source == quality
    assert out.applied[0].stored.timezone == fallback


def test_invalid_host_timezone_is_configuration_error_with_no_report_claim():
    storage = InMemoryStorage()
    with pytest.raises(ValueError, match="timezone_fallback"):
        kit = PerceptionKit(storage, timezone_fallback="Invalid/Zone")
        ingest(kit, [observation({"weight_kg": 70})])
    assert not storage.reports


@pytest.mark.parametrize("changes", [{"units": {"weight_kg": "kg"}},
                                      {"source_timezone": "Asia/Shanghai"}])
def test_wire_unit_or_timezone_change_same_revision_is_durable_conflict(changes):
    kit = PerceptionKit(InMemoryStorage())
    candidate(kit, 70)
    out = candidate(kit, 70, report_id="changed-wire", **changes)
    assert out.conflicts and len(records(kit)) == 1
    retry = candidate(kit, 70, report_id="changed-wire-retry", **changes)
    assert retry.conflicts and len(records(kit)) == 1


def test_current_only_unit_and_timezone_audit_persists():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, timezone_fallback="UTC")
    wire = observation({"height_cm": 1.75}, signal="health_height", units={"height_cm": "m"})
    del wire["timezone"]
    out = ingest(kit, [wire])
    assert out.applied and not storage.observations
    current = storage.get_current(subject_id="u", signals=["health_height"])["health_height"][0]
    assert current.typed_value == {"height_cm": 175}
    assert current.source_units == {"height_cm": "m"}
    assert current.source_values == {"height_cm": 1.75}
    assert current.timezone == "UTC" and current.timezone_source == "host_fallback"


def test_late_sample_checks_its_chronological_predecessor():
    storage, kit = prepared()
    weigh(kit, 71, eid="later", at=T + timedelta(hours=2))
    out = candidate(kit)
    assert out.conflicts and records(kit)[0].kind == "relative_jump"


def test_conformance_detects_conflicts_that_do_not_persist():
    from perceptkit.conformance import run_storage_conformance
    class Broken(InMemoryStorage):
        def put_conflict(self, record):
            return record
    assert any("conflict" in p for p in run_storage_conformance(Broken))


def test_conformance_detects_resolution_that_does_not_persist():
    from perceptkit.conformance import run_storage_conformance
    class Broken(InMemoryStorage):
        def resolve_conflict(self, **kw):
            return True
    assert any("conflict" in p for p in run_storage_conformance(Broken))
