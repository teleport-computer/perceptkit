"""Precise Current ownership, including retained and legacy Fact evidence."""
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit, RetryableMutationError
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import ContractError
from perceptkit.contracts.records import DurableDedupeIdentity
from perceptkit.manifest import MINIMAL_SIGNALS
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

    def record_retraction(self, row):
        assert self.required <= self._mutation_locks.keys(), "Current dimensions missing before tombstone"
        return super().record_retraction(row)


def kit_for(storage, *, history=True):
    sig = replace(MINIMAL_SIGNALS[SIGNAL], identity_strategy="source_event_id",
                  history_retention_days=7 if history else 0)
    return PerceptionKit(storage, signals={SIGNAL: sig})


def anchor(kit, name, *, eid="fact", revision=1, at=T):
    return ingest(kit, [observation({"anchor_id": name, "anchor_type": "wifi", "is_connected": True},
                  signal=SIGNAL, eid=eid, revision=revision, at=at)],
                  report_id=f"{eid}-{revision}-{at.isoformat()}", at=at)


def test_different_anchor_owners_are_independent_and_use_exact_dimension_key():
    s = InspectDimensions()
    s.required = {resource("B")}
    with s.mutation_transaction() as first:
        first.acquire([resource("A")])
        result = anchor(kit_for(s, history=False), "B")
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
    kit = kit_for(s, history=False if evidence == "current" else True)
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
    anchor(kit, "B", revision=2, at=T + timedelta(hours=1))
    acquired = [key for batch in s.requests for key in batch if key[0] == "20_current"]
    assert acquired == [resource("A"), resource("B")]
    assert all(row.dimension_key == f"{SIGNAL}\x1fA" for row in s.identity_records.values()
               if row.source_revision == 1)


def test_retraction_locks_every_actual_dimension_even_when_details_expired():
    s = InspectDimensions()
    kit = kit_for(s, history=False)
    anchor(kit, "A")
    original = next(iter(s.current.values()))
    anchor(kit, "B", revision=2, at=T + timedelta(hours=1))
    # Existing legacy/corrected Current rows may reference the same Fact across
    # dimensions. All rows being reselected must be owned before the tombstone.
    s.current[("u", SIGNAL, original.dimension_key)] = original
    assert {r.dimension_key for r in s.current.values()} == {f"{SIGNAL}\x1fA", f"{SIGNAL}\x1fB"}
    s.required = {resource("A"), resource("B")}
    s.requests.clear()
    kit.apply_retractions([Retraction("u", SIGNAL, "fact", "ios", T)], now=T)
    acquired = [key for batch in s.requests for key in batch if key[0] == "20_current"]
    assert acquired == [resource("A"), resource("B")]
    assert all(row.typed_value is None for row in s.current.values())


def test_legacy_unknown_dimension_is_explicit_and_never_guessed_from_correction():
    s = InspectDimensions()
    kit = kit_for(s, history=False)
    anchor(kit, "A")
    assert "dimension_key" in {f.name for f in fields(DurableDedupeIdentity)}, "missing durable dimension evidence"
    s.current.clear()
    s.identity_records = {key: replace(row, dimension_key=None) for key, row in s.identity_records.items()}
    reports = dict(s.reports)
    with pytest.raises(ContractError, match="current_dimension_evidence_incomplete"):
        anchor(kit, "B", revision=2)
    assert s.reports == reports and not s.current


def test_retraction_owns_retained_dimension_after_other_fact_replaces_current():
    s = InspectDimensions()
    kit = kit_for(s, history=False)
    anchor(kit, "A")
    anchor(kit, "A", eid="replacement", at=T + timedelta(hours=1))
    assert all(row.source_event_id == "replacement" for row in s.current.values())
    with s.mutation_transaction() as competing:
        competing.acquire([resource("A")])
        with pytest.raises(RetryableMutationError):
            kit.apply_retractions([Retraction("u", SIGNAL, "fact", "ios", T)], now=T)
    assert not s.retractions


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
