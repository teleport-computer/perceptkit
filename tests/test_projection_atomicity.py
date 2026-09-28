"""D01/D03 reference behavior; no production database isolation claim."""
from copy import deepcopy
from datetime import timedelta

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import CurrentProjection
from perceptkit.rules import EventDefinition
from test_acceptance_regressions_0_9 import T, ingest, observation, weight_rule


@pytest.mark.parametrize("second_at", [T, T - timedelta(minutes=1)])
def test_independent_workout_occurrences_ignore_current_order(second_at):
    rule = EventDefinition.parse({
        "id": "workout", "version": 1,
        "source": {"signal": "health_workout"},
        "condition": {"type": "occurrence"},
        "lifecycle": {"scope": "forever", "fire": "every"},
        "event": {"type": "workout.recorded"},
    })
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[rule])
    results = []
    for eid, minutes, at in [("A", 30, T), ("B", 45, second_at)]:
        results.append(ingest(kit, [observation(
            {"workout_type": "running", "duration_minutes": minutes},
            signal="health_workout", eid=eid, at=at)], report_id=eid))
    assert sum(len(out.events) for out in results) == 2
    assert next(iter(storage.aggregates.values())).typed_aggregate["duration_minutes"]["total"] == 75


@pytest.mark.parametrize("projection", ["current", "aggregate"])
def test_reference_rolls_back_entire_batch_on_projection_contention(projection):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    ingest(kit, [observation({"weight_kg": 69}, eid="before")], report_id="before")
    persisted = ("reports", "observations", "identities", "current", "aggregates",
                 "calendar", "reminders", "sync_state", "rule_state", "outbox",
                 "receipts", "retractions")
    before = {key: deepcopy(getattr(storage, key)) for key in persisted}
    method = "compare_and_put_" + projection
    original = getattr(storage, method, None)
    calls = 0

    def fail_after_one(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return False
        return original(*args, **kwargs)

    setattr(storage, method, fail_after_one)
    samples = [observation({"weight_kg": kg}, eid=eid, at=T + timedelta(minutes=i))
               for i, (eid, kg) in enumerate([("A", 72), ("B", 73)], start=1)]
    with pytest.raises(RuntimeError) as caught:
        ingest(kit, samples, report_id="retry")
    assert caught.value.retryable is True
    assert {key: getattr(storage, key) for key in persisted} == before
    setattr(storage, method, original)
    retried = ingest(kit, samples, report_id="retry")
    assert len(retried.applied) == 2 and len(retried.events) == 1


def test_sleep_queries_hide_legacy_segment_current():
    storage = InMemoryStorage()
    storage.compare_and_put_current(CurrentProjection(
        subject_id="u", signal="health_sleep", dimension_key="health_sleep\x1fcore",
        typed_value={"stage": "core", "duration_minutes": 30}, availability="observed",
        observed_at=T, received_at=T), expected_version=-1)
    kit = PerceptionKit(storage)
    view = kit.get_current(subject_id="u", signals=["health_sleep"], now=T)["health_sleep"]
    assert view.value is None and view.last_known is None
    last = kit.get_last_known(subject_id="u", signal="health_sleep")
    assert last.value is None and last.last_known is None


def test_sleep_reference_mapping_does_not_promise_current_storage():
    from perceptkit.manifest import MINIMAL_SIGNALS, reference_mapping
    row = next(row for row in reference_mapping(MINIMAL_SIGNALS)
               if row["signal"] == "health_sleep")
    assert row["objects"] == ("StoredObservation", "DailyAggregate")
