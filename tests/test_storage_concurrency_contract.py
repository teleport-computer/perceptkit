"""Deterministic ownership protocol tests; not PostgreSQL isolation evidence."""
from contextlib import contextmanager
from datetime import timedelta
from importlib import import_module

import pytest

from perceptkit import PerceptionKit, IngestContext
from perceptkit.conformance import InMemoryStorage, run_storage_conformance
from test_acceptance_regressions_0_9 import T, DAY, weigh, retract, observation, ingest, weight_rule


def api():
    assert callable(getattr(InMemoryStorage, "mutation_transaction", None)), "missing mutation ownership port"
    return import_module("perceptkit.contracts.mutation")


class AuditedStorage(InMemoryStorage):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.held = set()

    @contextmanager
    def mutation_transaction(self):
        with super().mutation_transaction() as owner:
            storage = self
            class AuditOwner:
                def acquire(self, keys):
                    storage.requests.append(tuple(keys))
                    assert tuple(keys) == tuple(sorted(set(keys)))
                    owner.acquire(keys)
                    storage.held.update(keys)
            try:
                yield AuditOwner()
            finally:
                self.held.clear()

    def list_retractions(self, **kw):
        if kw.get("source_event_ids") == ["A"]:
            assert api().fact_key("u", "health_weight", "ios", "A") in self.held
        return super().list_retractions(**kw)

    def append_observation(self, row):
        assert api().fact_key(row.subject_id, row.signal, row.source, row.source_event_id) in self.held
        assert api().aggregate_key(row.subject_id, row.signal, row.effective_local_date, "daily", 2) in self.held
        return super().append_observation(row)

    def compare_and_put_current(self, projection, *, expected_version):
        assert api().current_key(projection.subject_id, projection.signal, projection.dimension_key) in self.held
        return super().compare_and_put_current(projection, expected_version=expected_version)

    def compare_and_put_aggregate(self, aggregate, *, expected_version):
        assert api().aggregate_key(aggregate.subject_id, aggregate.signal, aggregate.local_date,
                                   aggregate.aggregation_kind, aggregate.aggregation_version) in self.held
        assert self.transaction_depth > 0
        return super().compare_and_put_aggregate(aggregate, expected_version=expected_version)

    def put_aggregate(self, aggregate):
        assert api().aggregate_key(aggregate.subject_id, aggregate.signal, aggregate.local_date,
                                   aggregate.aggregation_kind, aggregate.aggregation_version) in self.held
        return super().put_aggregate(aggregate)

    def record_retraction(self, row):
        assert api().fact_key(row.subject_id, row.signal, row.source, row.source_event_id) in self.held
        return super().record_retraction(row)

    def get_rule_state(self, **kw):
        assert api().rule_key(kw["subject_id"], kw["definition_id"], kw["scope_key"]) in self.held
        return super().get_rule_state(**kw)

    def put_rule_state(self, **kw):
        assert api().rule_key(kw["subject_id"], kw["definition_id"], kw["scope_key"]) in self.held
        return super().put_rule_state(**kw)

    def enqueue_event(self, entry):
        assert self.transaction_depth > 0 and self.held
        assert api().fact_key("u", "health_weight", "ios", entry.source_event_id) in self.held
        return super().enqueue_event(entry)


def test_ingest_correction_retraction_hold_identical_fact_through_all_writes():
    api()
    storage = AuditedStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    weigh(kit, 70, revision=1)
    first = storage.requests[0]
    storage.requests.clear()
    weigh(kit, 72, revision=2)
    assert first == storage.requests[0]
    storage.requests.clear()
    retract(kit, "A")
    assert first == storage.requests[0]
    assert not storage.held
    assert kit.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"].value is None


def test_batch_resources_canonical_independent_of_input_order():
    api()
    rows = [observation({"weight_kg": 70}, eid="B"), observation({"weight_kg": 71}, eid="A")]
    requests = []
    for batch in (rows, rows[::-1]):
        s = AuditedStorage()
        ingest(PerceptionKit(s, definitions=[weight_rule()]), batch)
        requests.append(s.requests)
    assert requests[0] == requests[1]
    assert sum(k[0] == "10_fact" for keys in requests[0] for k in keys) == 2


@pytest.mark.parametrize("first", ["ingest", "retract"])
def test_same_fact_contender_retries_and_both_serial_orders_delete(first):
    m = api()
    s = InMemoryStorage()
    kit = PerceptionKit(s)
    with s.mutation_transaction() as owner:
        owner.acquire([m.fact_key("u", "health_weight", "ios", "A")])
        with pytest.raises(m.RetryableMutationError):
            (weigh(kit, 70) if first == "retract" else retract(kit, "A"))
    if first == "ingest":
        weigh(kit, 70)
        retract(kit, "A")
    else:
        retract(kit, "A")
        assert weigh(kit, 70).retracted
    rows = s.get_current(subject_id="u", signals=["health_weight"]).get("health_weight", ())
    assert all(row.typed_value is None for row in rows)


def test_aggregate_key_version_subject_and_fallback_identity_do_not_collide():
    m = api()
    keys = [m.aggregate_key("u", "x", DAY, "daily", 2), m.aggregate_key("u", "x", DAY, "daily", 3),
            m.aggregate_key("v", "x", DAY, "daily", 2), m.fact_key("u", "x", "ios", "id"),
            m.fact_key("v", "x", "ios", "id"), m.fact_key("u", "x", "ios", None, fallback="id")]
    assert len(set(keys)) == len(keys)
    assert m.rule_key("u", "d1", "scope") != m.rule_key("u", "d2", "scope")
    assert m.rule_key("u", "d1", "scope") != m.rule_key("u", "d1", "other")


def test_rule_contention_rolls_back_report_facts_and_aggregate_then_retry_fires_once():
    m = api()
    s = InMemoryStorage()
    kit = PerceptionKit(s, definitions=[weight_rule()])
    weigh(kit, 70, eid="baseline")
    with s.mutation_transaction() as owner:
        owner.acquire([m.rule_key("u", "weight", f"{DAY.isoformat()}@v1")])
        with pytest.raises(m.RetryableMutationError):
            weigh(kit, 72, eid="B", at=T + timedelta(hours=1))
        assert len(s.observations) == len(s.reports) == 1
        assert not s.outbox
    assert len(weigh(kit, 72, eid="B", at=T + timedelta(hours=1)).events) == 1
    assert not weigh(kit, 73, eid="C", at=T + timedelta(hours=2)).events


def test_recompute_serializes_before_reading_detail_and_putting_aggregate():
    m = api()
    s = InMemoryStorage()
    kit = PerceptionKit(s)
    weigh(kit, 70)
    with s.mutation_transaction() as owner:
        owner.acquire([m.aggregate_key("u", "health_weight", DAY, "daily", 2)])
        with pytest.raises(m.RetryableMutationError):
            kit.recompute_aggregates(subject_id="u", signal="health_weight", start=DAY, end=DAY, now=T)


def test_owner_reuse_is_explicit_and_fence_cannot_escape_transaction():
    m = api()
    s = InMemoryStorage()
    key = m.fact_key("u", "x", "ios", "A")
    with s.mutation_transaction() as owner:
        owner.acquire([key])
        owner.acquire([key])  # Same explicit capability is idempotent.
        with pytest.raises(m.RetryableMutationError):
            with s.mutation_transaction() as other:
                other.acquire([key])  # Nested public operation is a new owner.
    with pytest.raises(m.RetryableMutationError):
        owner.acquire([key])


def test_lock_order_violation_aborts_even_if_caught_inside_transaction():
    m = api()
    s = InMemoryStorage()
    with pytest.raises(m.RetryableMutationError):
        with s.mutation_transaction() as owner:
            owner.acquire([m.rule_key("u", "d", "scope")])
            s.put_rule_state(subject_id="u", definition_id="d", scope_key="scope", state={"written": True})
            with pytest.raises(m.RetryableMutationError):
                owner.acquire([m.fact_key("u", "x", "ios", "A")])
    assert not s.rule_state


def test_lost_fence_at_commit_rolls_back_and_does_not_release_new_owner():
    m = api()
    s = InMemoryStorage()
    key = m.fact_key("u", "x", "ios", "A")
    successor = object()
    with pytest.raises(m.RetryableMutationError):
        with s.mutation_transaction() as owner:
            owner.acquire([key])
            s.put_rule_state(subject_id="u", definition_id="d", scope_key="s", state={"written": True})
            # Deterministic fencing fault, not a thread/database isolation test.
            s._mutation_locks[key] = successor
    assert not s.rule_state
    assert s._mutation_locks[key] is successor


def test_disjoint_resources_for_one_subject_do_not_contend():
    m = api()
    s = InMemoryStorage()
    with s.mutation_transaction() as first:
        first.acquire([m.fact_key("u", "x", "ios", "A")])
        with s.mutation_transaction() as second:
            second.acquire([m.fact_key("u", "x", "ios", "B")])
            second.acquire([m.aggregate_key("u", "x", DAY, "daily", 2)])


def test_cross_day_correction_owns_both_dates_before_writing():
    m = api()
    s = AuditedStorage()
    kit = PerceptionKit(s)
    weigh(kit, 70, revision=1)
    s.requests.clear()
    weigh(kit, 71, revision=2, at=T + timedelta(days=1))
    aggregate_keys = [k for keys in s.requests for k in keys if k[0] == "30_aggregate"]
    assert aggregate_keys == [m.aggregate_key("u", "health_weight", day, "daily", 2)
                              for day in (DAY, DAY + timedelta(days=1))]


def test_retraction_cas_exhaustion_rolls_back_tombstone_and_recompute():
    from perceptkit import RetryableProjectionError
    from copy import deepcopy
    class FailReselect(InMemoryStorage):
        fail = False
        def compare_and_put_current(self, projection, *, expected_version):
            return False if self.fail else super().compare_and_put_current(projection, expected_version=expected_version)
    s = FailReselect()
    kit = PerceptionKit(s)
    weigh(kit, 70)
    before = deepcopy(s.aggregates)
    s.fail = True
    with pytest.raises(RetryableProjectionError):
        retract(kit, "A")
    assert not s.retractions and s.aggregates == before
    s.fail = False
    retract(kit, "A")
    assert all(row.typed_value is None for row in s.current.values())


def test_adapter_conformance_detects_broken_aggregate_cas():
    class Broken(InMemoryStorage):
        def compare_and_put_aggregate(self, aggregate, *, expected_version):
            self.put_aggregate(aggregate)
            return True
    assert any("aggregate CAS" in p for p in run_storage_conformance(Broken))


@pytest.mark.parametrize("kind", ["absence", "streak"])
def test_scheduled_rule_batch_owns_all_scopes_before_first_read_until_commit(kind):
    m = api()
    from perceptkit.rules import EventDefinition
    definitions = [EventDefinition.parse({
        "id": name, "version": 1, "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": kind, "value": 3600},
        "lifecycle": {"scope": "local_day", "fire": "once"},
        "event": {"type": "weight.absent"},
    }) for name in ("z", "a")]
    class CheckScheduled(InMemoryStorage):
        def get_aggregate(self, **kw):
            assert all(m.rule_key("u", name, f"{DAY}@v1") in self._mutation_locks
                       for name in ("a", "z")), "scheduled aggregate read outside batch ownership"
            return super().get_aggregate(**kw)
        def get_current(self, **kw):
            assert all(m.rule_key("u", name, f"{DAY}@v1") in self._mutation_locks
                       for name in ("a", "z")), "scheduled read outside batch ownership"
            return super().get_current(**kw)
    kit = PerceptionKit(CheckScheduled(), definitions=definitions)
    if kind == "absence":
        kit.evaluate_absence(subject_id="u", now=T)
    else:
        kit.evaluate_daily(subject_id="u", local_date=DAY, now=T)
