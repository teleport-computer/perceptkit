"""Precise Current ownership, including retained and legacy Fact evidence."""
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit, RetryableMutationError
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import ContractError
from perceptkit.contracts.records import DurableDedupeIdentity
from test_acceptance_regressions_0_9 import T, DAY, observation, ingest
from perceptkit.contracts.retraction import Retraction

SIGNAL = "proximity_anchor"


def resource(anchor):
    return ("20_current", "u", SIGNAL, f"{SIGNAL}\x1f{anchor}")


class InspectDimensions(InMemoryStorage):
    def __init__(self):
        super().__init__()
        self.required = set()
        self.requests = []

    @contextmanager
    def mutation_transaction(self):
        with super().mutation_transaction() as owner:
            storage = self
            class RecordedOwner:
                def acquire(self, keys):
                    storage.requests.append(tuple(keys))
                    owner.acquire(keys)
            yield RecordedOwner()

    def remember_identity(self, row):
        assert self.required <= self._mutation_locks.keys(), "Current dimensions missing before Fact write"
        return super().remember_identity(row)

    def append_observation(self, row):
        assert self.required <= self._mutation_locks.keys(), "Current dimensions missing before Observation write"
        return super().append_observation(row)

    def backfill_identity(self, row):
        assert self.required <= self._mutation_locks.keys(), "Current dimensions missing before evidence write"
        return super().backfill_identity(row)

    def record_retraction(self, row):
        assert self.required <= self._mutation_locks.keys(), "Current dimensions missing before tombstone"
        return super().record_retraction(row)


def kit_for(storage):
    return PerceptionKit(storage)


def anchor(kit, name, *, eid="fact", revision=1, at=T):
    return ingest(kit, [observation({"anchor_id": name, "anchor_type": "wifi", "is_connected": True},
                  signal=SIGNAL, eid=eid, revision=revision, at=at)],
                  report_id=f"{eid}-{revision}-{at.isoformat()}", at=at)


def test_different_anchor_owners_are_independent_and_use_exact_dimension_key():
    s = InspectDimensions()
    s.required = {resource("B")}
    with s.mutation_transaction() as first:
        first.acquire([resource("A")])
        result = anchor(kit_for(s), "B")
        assert len(result.applied) == 1
    assert ("20_current", "u", SIGNAL) not in {k for batch in s.requests for k in batch}


def test_same_anchor_dimension_contends_for_independent_facts():
    s = InspectDimensions()
    with s.mutation_transaction() as first:
        first.acquire([resource("A")])
        with pytest.raises(RetryableMutationError):
            anchor(kit_for(s), "A", eid="other-fact")
    assert not s.observations and not s.identity_records and not s.reports


@pytest.mark.parametrize("evidence", ["details", "current", "identity"])
def test_cross_dimension_correction_preplans_old_and_new_from_persisted_evidence(evidence):
    s = InspectDimensions()
    kit = kit_for(s)
    assert anchor(kit, "A").applied
    assert "dimension_key" in {f.name for f in fields(DurableDedupeIdentity)}, "missing durable dimension evidence"
    if evidence != "details":
        s.observations.clear()
    if evidence == "identity":
        s.current.clear()
    else:
        s.identity_records = {key: replace(row, dimension_key=None) for key, row in s.identity_records.items()}
    if evidence == "details":
        s.current.clear()  # Old projection no longer carries the evidence.
    s.required = {resource("A"), resource("B")}
    s.requests.clear()
    anchor(kit, "B", revision=2, at=T)
    acquired = [key for batch in s.requests for key in batch if key[0] == "20_current"]
    assert acquired == [resource("A"), resource("B")]
    assert all(row.dimension_key == f"{SIGNAL}\x1fA" for row in s.identity_records.values()
               if row.source_revision == 1)


def test_unsupported_retraction_preserves_every_actual_dimension_when_details_expired():
    from copy import deepcopy
    s = InspectDimensions()
    kit = kit_for(s)
    anchor(kit, "A")
    original = next(iter(s.current.values()))
    anchor(kit, "B", revision=2, at=T)
    # Existing legacy/corrected Current rows may reference the same Fact across
    # dimensions. All rows being reselected must be owned before the tombstone.
    s.current[("u", SIGNAL, original.dimension_key)] = original
    assert {r.dimension_key for r in s.current.values()} == {f"{SIGNAL}\x1fA", f"{SIGNAL}\x1fB"}
    s.observations.clear()
    before = deepcopy((s.current, s.identity_records, s.retractions))
    s.required = {resource("A"), resource("B")}
    s.requests.clear()
    with pytest.raises(ContractError, match="retraction_identity_unsupported"):
        kit.apply_retractions([Retraction("u", SIGNAL, "fact", "ios", T)], now=T)
    assert not s.requests
    assert (s.current, s.identity_records, s.retractions) == before


def test_legacy_unknown_dimension_is_explicit_and_never_guessed_from_correction():
    s = InspectDimensions()
    kit = kit_for(s)
    anchor(kit, "A")
    assert "dimension_key" in {f.name for f in fields(DurableDedupeIdentity)}, "missing durable dimension evidence"
    s.current.clear()
    s.observations.clear()
    s.identity_records = {key: replace(row, dimension_key=None) for key, row in s.identity_records.items()}
    reports = dict(s.reports)
    with pytest.raises(ContractError, match="current_dimension_evidence_incomplete"):
        anchor(kit, "B", revision=2)
    assert s.reports == reports and not s.current


def test_correction_owns_retained_dimension_after_other_fact_replaces_current():
    s = InspectDimensions()
    kit = kit_for(s)
    anchor(kit, "A")
    anchor(kit, "A", eid="replacement", at=T + timedelta(hours=1))
    assert all(row.source_event_id == "replacement" for row in s.current.values())
    s.observations.clear()
    with s.mutation_transaction() as competing:
        competing.acquire([resource("A")])
        with pytest.raises(RetryableMutationError):
            anchor(kit, "B", eid="new-optional-ref", revision=2, at=T)
    assert all(row.source_event_id == "replacement" for row in s.current.values())


def test_dimension_backfill_only_fills_unknown_and_preserves_identity_content():
    s = InspectDimensions()
    anchor(kit_for(s), "A")
    row = next(iter(s.identity_records.values()))
    assert getattr(row, "dimension_key", None) == f"{SIGNAL}\x1fA"
    key = next(iter(s.identity_records))
    s.identity_records[key] = replace(row, dimension_key=None)
    s.backfill_identity(row)
    assert s.identity_records[key] == row
    with pytest.raises(ValueError):
        s.backfill_identity(replace(row, dimension_key=f"{SIGNAL}\x1fB"))
    with pytest.raises(ValueError):
        s.backfill_identity(replace(row, semantic_digest="other"))


def test_dimension_batch_acquisition_order_ignores_observation_order():
    requests = []
    for names in (("B", "A"), ("A", "B")):
        s = InspectDimensions()
        s.required = {resource("A"), resource("B")}
        ingest(kit_for(s), [observation({"anchor_id": name, "anchor_type": "wifi", "is_connected": True},
                       signal=SIGNAL, eid=name) for name in names])
        requests.append([batch for batch in s.requests if batch and batch[0][0] == "20_current"])
    assert requests == [[(resource("A"), resource("B"))]] * 2


def test_contracts_exports_public_retryable_error():
    from perceptkit import contracts
    assert getattr(contracts, "RetryableMutationError", None) is RetryableMutationError


def test_conformance_catches_dropped_dimension_backfill():
    from perceptkit.conformance import run_storage_conformance
    class Broken(InMemoryStorage):
        def backfill_identity(self, identity):
            return super().backfill_identity(replace(identity, dimension_key=None))
    assert any("dimension backfill" in p for p in run_storage_conformance(Broken))


def test_default_fallback_fact_revision_owns_both_anchor_dimensions_before_writes():
    s = InspectDimensions()
    kit = PerceptionKit(s)  # Real MINIMAL_SIGNALS, including deterministic_digest.
    assert kit.signals[SIGNAL].identity_strategy == "deterministic_digest"
    assert anchor(kit, "A", eid=None, revision=1).applied
    original_fact = next(iter(s.identity_records.values())).fact_key
    s.required = {resource("A"), resource("B")}
    s.requests.clear()
    # Existing fallback Fact = subject/source/signal/occurred_at; revision and
    # content belong to Delivery identity. Same time, higher revision is real.
    assert anchor(kit, "B", eid=None, revision=2, at=T).applied
    assert {row.fact_key for row in s.identity_records.values()} == {original_fact}
    assert [key for batch in s.requests for key in batch if key[0] == "20_current"] == [resource("A"), resource("B")]


def test_fallback_uses_durable_dimension_after_detail_and_current_expire():
    s = InspectDimensions()
    kit = PerceptionKit(s)
    anchor(kit, "A", eid=None)
    s.observations.clear()
    s.current.clear()
    s.required = {resource("A"), resource("B")}
    assert anchor(kit, "B", eid=None, revision=2).applied


@pytest.mark.parametrize("evidence", ["detail", "current", "missing"])
def test_fallback_legacy_dimension_requires_matching_persisted_evidence(evidence):
    s = InspectDimensions()
    kit = PerceptionKit(s)
    anchor(kit, "A", eid=None)
    original_key = next(iter(s.identity_records))
    s.identity_records[original_key] = replace(s.identity_records[original_key], dimension_key=None)
    # Same source/event_id=None is not a Fact identity. This unrelated row must
    # neither supply the missing partition nor expand this mutation's lock set.
    anchor(kit, "unrelated", eid=None, at=T + timedelta(minutes=1))
    original = s.identity_records[original_key]
    s.identity_records[original_key] = replace(original, dimension_key=None)
    if evidence != "detail":
        s.observations = {k: row for k, row in s.observations.items() if row.occurred_at != T}
    if evidence != "current":
        s.current = {k: row for k, row in s.current.items() if row.observed_at != T}
    s.requests.clear()
    if evidence == "missing":
        with pytest.raises(ContractError, match="current_dimension_evidence_incomplete"):
            anchor(kit, "B", eid=None, revision=2)
        assert s.identity_records[original_key].dimension_key is None
    else:
        s.required = {resource("A"), resource("B")}
        assert anchor(kit, "B", eid=None, revision=2).applied
        assert s.identity_records[original_key].dimension_key == f"{SIGNAL}\x1fA"
        assert [key for batch in s.requests for key in batch if key[0] == "20_current"] == [resource("A"), resource("B")]


def test_unrelated_unmapped_legacy_dimension_does_not_block_new_fallback_fact():
    s = InspectDimensions()
    kit = PerceptionKit(s)
    s.remember_identity(DurableDedupeIdentity(
        subject_id="u", signal=SIGNAL, source="ios", source_event_identity_digest="unrelated-opaque",
        first_applied_at=T))
    s.required = {resource("B")}
    assert anchor(kit, "B", eid=None).applied
    assert [key for batch in s.requests for key in batch if key[0] == "20_current"] == [resource("B")]


def test_default_fallback_ignores_optional_source_ids_for_fact_ownership():
    s = InspectDimensions()
    kit = PerceptionKit(s)
    anchor(kit, "A", eid="ignored-A")
    first_fact = next(key for batch in s.requests for key in batch if key[0] == "10_fact")
    canonical = next(iter(s.identity_records.values())).fact_key
    assert first_fact == ("10_fact", "u", SIGNAL, "ios", "fallback", canonical)
    s.required = {resource("A"), resource("B")}
    s.requests.clear()
    anchor(kit, "B", eid="ignored-B", revision=2, at=T)
    assert {row.fact_key for row in s.identity_records.values()} == {canonical}
    assert next(key for batch in s.requests for key in batch if key[0] == "10_fact") == first_fact


@pytest.mark.parametrize("evidence", ["detail", "current"])
def test_optional_id_changes_do_not_hide_canonical_fallback_legacy_evidence(evidence):
    s = InspectDimensions()
    kit = PerceptionKit(s)
    anchor(kit, "A", eid="ignored-A")
    original_key = next(iter(s.identity_records))
    s.identity_records[original_key] = replace(s.identity_records[original_key], dimension_key=None)
    if evidence == "detail":
        s.current.clear()
    else:
        s.observations.clear()
    s.required = {resource("A"), resource("B")}
    assert anchor(kit, "B", eid="ignored-B", revision=2, at=T).applied
    assert s.identity_records[original_key].dimension_key == f"{SIGNAL}\x1fA"


@pytest.mark.parametrize("expired", [False, True])
def test_deterministic_retraction_is_typed_unsupported_before_transaction_or_write(expired):
    from copy import deepcopy
    import perceptkit
    s = InspectDimensions()
    kit = PerceptionKit(s)
    anchor(kit, "A", eid="optional-ref")
    canonical = next(iter(s.identity_records.values())).fact_key
    if expired:
        s.observations.clear()
    before = deepcopy((s.retractions, s.current, s.aggregates, s.identity_records))
    # Holding canonical ownership must not allow deletion to sneak through
    # under a different raw-source-ID key. Unsupported is explicit, not success.
    with s.mutation_transaction() as owner:
        owner.acquire([("10_fact", "u", SIGNAL, "ios", "fallback", canonical)])
        depth = s.transactions_opened
        with pytest.raises(ContractError) as caught:
            kit.apply_retractions([Retraction("u", SIGNAL, "optional-ref", "ios", T + timedelta(days=1))],
                                  now=T + timedelta(days=1))
        assert type(caught.value) is getattr(perceptkit, "UnsupportedRetractionIdentityError", None)
        assert type(caught.value) is perceptkit.contracts.UnsupportedRetractionIdentityError
        assert caught.value.code == "retraction_identity_unsupported"
        assert caught.value.retryable is False
        assert caught.value.recovery_action == "upgrade_retraction_identity_contract"
        assert s.transactions_opened == depth
    assert (s.retractions, s.current, s.aggregates, s.identity_records) == before


@pytest.mark.parametrize("deletion_ref", ["optional-A", "optional-B"])
def test_singleton_retraction_cannot_create_raw_id_tombstone_or_change_current(deletion_ref):
    from copy import deepcopy
    from perceptkit import UnsupportedRetractionIdentityError
    s = InspectDimensions()
    kit = PerceptionKit(s)
    out = ingest(kit, [observation({"changed": True}, signal="screen_change",
                                  eid="optional-A", revision=1)], report_id="singleton-1")
    assert out.applied
    canonical = next(iter(s.identity_records.values())).fact_key
    before = deepcopy((s.retractions, s.current, s.aggregates, s.identity_records))
    transactions = s.transactions_opened
    s.requests.clear()
    with pytest.raises(UnsupportedRetractionIdentityError) as caught:
        kit.apply_retractions([Retraction("u", "screen_change", deletion_ref, "ios", T)], now=T)
    assert caught.value.identity_strategy == "singleton"
    assert caught.value.code == "retraction_identity_unsupported"
    assert caught.value.retryable is False
    assert s.transactions_opened == transactions and not s.requests
    assert (s.retractions, s.current, s.aggregates, s.identity_records) == before
    revised = ingest(kit, [observation({"changed": False}, signal="screen_change",
                                      eid="optional-B", revision=2)], report_id="singleton-2")
    assert len(revised.applied) == 1 and not revised.retracted
    assert not s.retractions
    assert {row.fact_key for row in s.identity_records.values()} == {canonical}
    assert next(iter(s.current.values())).typed_value == {"changed": False}


@pytest.mark.parametrize("unsupported_signal", [SIGNAL, "screen_change"])
def test_mixed_retraction_batch_rejects_unsupported_strategy_before_supported_sibling_write(unsupported_signal):
    from copy import deepcopy
    from perceptkit import UnsupportedRetractionIdentityError
    s = InspectDimensions()
    kit = PerceptionKit(s)
    assert ingest(kit, [observation({"weight_kg": 70}, eid="known")]).applied
    before = deepcopy((s.retractions, s.current, s.aggregates, s.identity_records))
    transactions = s.transactions_opened
    s.requests.clear()
    with pytest.raises(UnsupportedRetractionIdentityError):
        kit.apply_retractions([Retraction("u", "health_weight", "known", "ios", T),
                               Retraction("u", unsupported_signal, "unknown", "ios", T)], now=T)
    assert (s.retractions, s.current, s.aggregates, s.identity_records) == before
    assert s.transactions_opened == transactions and not s.requests
