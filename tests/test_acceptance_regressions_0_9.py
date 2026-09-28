"""Fixed A01–A21 gates for the 2026-09-28 consistency contract.

These are intentionally ordinary assertions, not xfail. The Task 1 deliverable
is RED. Select the unchanged baseline with ``-k 'not test_acceptance_'``.
Kit interleavings are deterministic port-level reproductions, not proof of
PostgreSQL transaction isolation. No production compatibility API is added.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import CurrentProjection, DailyAggregate, StoredObservation
from perceptkit.contracts.receipt import IngestReceipt, WakeReceipt
from perceptkit.contracts.retraction import Retraction
from perceptkit.rules import EventDefinition

T = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
DAY = T.date()


def observation(value, *, signal="health_weight", eid="A", at=T, revision=None,
                source_timezone="UTC", **extra):
    return dict(signal=signal, signal_schema_version=1, occurred_at=at.isoformat(),
                availability="observed", timezone=source_timezone,
                source_event_id=eid, source_revision=revision, value=value, **extra)


def ingest(kit, observations, *, report_id="report", source="ios", at=T):
    return kit.ingest(dict(schema_version=1, report_id=report_id, producer=source,
                           observations=observations), context=IngestContext("u", at))


def weigh(kit, value, *, eid="A", at=T, revision=None, report_id=None, source="ios"):
    out = ingest(kit, [observation({"weight_kg": value}, eid=eid, at=at,
                                  revision=revision)],
                 report_id=report_id or f"{source}-{eid}-{revision}", source=source, at=at)
    assert not out.rejected, out.rejected
    return out


def aggregate(storage, signal):
    rows = storage.get_aggregate(subject_id="u", signal=signal,
                                 start_date=DAY, end_date=DAY)
    assert len(rows) == 1, rows
    return rows[0]


def weight_rule(*, version=1):
    return EventDefinition.parse({
        "id": "weight", "version": version,
        "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": "threshold_crossing", "operator": "gte", "value": 71},
        "lifecycle": {"scope": "local_day", "fire": "once"},
        "event": {"type": "health.weight_over"},
    })


def fired_weight(*, value=72, previous=70, previous_source="ios", trigger_source="ios"):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    weigh(kit, previous, eid="previous", source=previous_source)
    out = weigh(kit, value, eid="trigger", source=trigger_source,
                at=T + timedelta(hours=1), revision=1)
    assert len(out.events) == 1, out
    return storage, kit


def retract(kit, eid="trigger", *, source="ios"):
    return kit.apply_retractions(
        [Retraction("u", "health_weight", eid, source, T + timedelta(hours=2))],
        now=T + timedelta(hours=2))


class RecordingWake:
    """Capture the real dispatch boundary without an external runtime."""
    def __init__(self):
        self.delivered = []

    def wake(self, event, attempt):
        self.delivered.append(event)
        return WakeReceipt(event.event_id, attempt.attempt_id, "accepted", T + timedelta(hours=2))


@pytest.mark.parametrize("arrangement", ["same_batch", "split", "out_of_order", "retransmit"])
def test_acceptance_A01_A02_sleep_facts_are_all_aggregated(arrangement):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    # Same upload timestamp is the real iOS shape in R1, distinct sample ids
    # and episode times are distinct business facts.
    samples = [observation({"stage": "core", "duration_minutes": duration,
                            "start_at": (T + timedelta(hours=i)).isoformat(),
                            "end_at": (T + timedelta(hours=i, minutes=duration)).isoformat()},
                           signal="health_sleep", eid=f"sleep-{i}",
                           at=T + timedelta(hours=2))
               for i, duration in enumerate((30, 45))]
    batches = [samples] if arrangement in ("same_batch", "retransmit") else [[s] for s in samples]
    if arrangement == "out_of_order":
        batches.reverse()
    results = [ingest(kit, batch, report_id=f"sleep-{i}", at=T + timedelta(hours=2))
               for i, batch in enumerate(batches)]
    if arrangement == "retransmit":
        replay = ingest(kit, samples, report_id="sleep-0", at=T + timedelta(hours=3))
        assert replay.receipt.status == "duplicate"
    timeline, _ = kit.list_timeline(subject_id="u", signal="health_sleep")
    before = aggregate(storage, "health_sleep").typed_aggregate["duration_minutes"]["total"]
    kit.recompute_aggregates(subject_id="u", signal="health_sleep", start=DAY, end=DAY,
                             now=T + timedelta(hours=3))
    after = aggregate(storage, "health_sleep").typed_aggregate["duration_minutes"]["total"]
    assert (len(timeline), before, after, sum(len(x.conflicts) for x in results)) == (2, 75, 75, 0)


def test_acceptance_A02_sleep_current_does_not_present_one_segment_as_the_night():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"stage": "core", "duration_minutes": 30,
                          "start_at": T.isoformat(),
                          "end_at": (T + timedelta(minutes=30)).isoformat()},
                         signal="health_sleep")
    assert len(ingest(kit, [sample]).applied) == 1
    view = kit.get_current(subject_id="u", signals=["health_sleep"], now=T).get("health_sleep")
    # Do not prescribe a replacement API: omitted/no-data/empty entries all
    # avoid falsely offering a raw segment as a meaningful sleep Current.
    entries = view if isinstance(view, list) else ([] if view is None else [view])
    assert all(x.value is None and x.last_known is None for x in entries), view


@pytest.fixture
def v080_storage(request):
    raw = json.loads((Path(__file__).parent / "fixtures/v080_workout_state.json").read_text())
    storage = InMemoryStorage()
    storage.identities = {tuple(x) for x in raw["identities"]}
    for collection, cls in (("observations", StoredObservation), ("current", CurrentProjection),
                            ("aggregates", DailyAggregate), ("receipts", IngestReceipt)):
        for source in raw[collection]:
            row = dict(source)
            for name in ("occurred_at", "received_at", "observed_at", "expires_at", "updated_at", "created_at"):
                if row.get(name) is not None:
                    row[name] = datetime.fromisoformat(row[name])
            for name in ("effective_local_date", "local_date"):
                if row.get(name) is not None:
                    row[name] = date.fromisoformat(row[name])
            record = cls(**row)
            if collection == "observations":
                storage.append_observation(record)
            elif collection == "current":
                storage.compare_and_put_current(record, expected_version=-1)
            elif collection == "aggregates":
                storage.put_aggregate(record)
            else:
                storage.reports[(record.subject_id, record.producer, record.report_id)] = record
    if request.param == "details_expired":
        storage.delete_observations(subject_id="u")
    return storage, raw["report"]


@pytest.mark.parametrize("v080_storage", ["details_present", "details_expired"], indirect=True)
def test_acceptance_A03_real_v080_state_replay_does_not_double_count(v080_storage):
    storage, report = v080_storage
    assert aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"] == 30
    report = deepcopy(report)
    report["report_id"] = "new-upload"
    report["observations"][0]["occurred_at"] = (T + timedelta(hours=1)).isoformat()
    out = PerceptionKit(storage).ingest(report, context=IngestContext("u", T + timedelta(hours=1)))
    assert (len(out.applied), len(out.duplicates),
            aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"]) == (0, 1, 30)


@pytest.mark.parametrize("field,replacement", [("source_revision", 2), ("timezone", "Asia/Shanghai")],
                         ids=["A04-revision", "A05-timezone"])
def test_acceptance_A04_A05_report_semantic_change_is_conflict(field, replacement):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    sample = observation({"weight_kg": 70}, revision=1)
    assert ingest(kit, [sample]).receipt.status == "accepted"
    before = deepcopy(storage.__dict__)
    changed = dict(sample, **{field: replacement})
    out = ingest(kit, [changed])
    assert out.receipt.status == "conflict", out
    assert storage.observations == before["observations"]
    assert storage.current == before["current"]
    assert storage.aggregates == before["aggregates"]


def test_acceptance_A07_interleaved_workouts_do_not_lose_an_aggregate_update():
    class InterleavedAggregateRead(InMemoryStorage):
        after_read = None

        def get_aggregate(self, **kwargs):
            rows = super().get_aggregate(**kwargs)
            callback, self.after_read = self.after_read, None
            if callback:
                callback()
            return rows

    storage = InterleavedAggregateRead()
    first, second = PerceptionKit(storage), PerceptionKit(storage)
    def workout(kit, minutes, eid, at):
        return ingest(kit, [observation({"workout_type": "running", "duration_minutes": minutes},
                                       signal="health_workout", eid=eid, at=at)], report_id=eid, at=at)
    # Pause A after its aggregate read; B reads and commits before A's write.
    storage.after_read = lambda: workout(second, 45, "B", T + timedelta(minutes=1))
    workout(first, 30, "A", T)
    before = aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"]
    first.recompute_aggregates(subject_id="u", signal="health_workout", start=DAY, end=DAY,
                               now=T + timedelta(hours=1))
    after = aggregate(storage, "health_workout").typed_aggregate["duration_minutes"]["total"]
    assert (len(storage.observations), before, after) == (2, 75, 75)


def test_acceptance_A08_retraction_between_fact_check_and_write_cannot_resurrect():
    class InterleavedRetractionRead(InMemoryStorage):
        after_read = None

        def list_retractions(self, **kwargs):
            rows = super().list_retractions(**kwargs)
            callback, self.after_read = self.after_read, None
            if callback:
                callback()
            return rows

    storage = InterleavedRetractionRead()
    first, second = PerceptionKit(storage), PerceptionKit(storage)
    storage.after_read = lambda: retract(second, "A")
    weigh(first, 70)
    assert storage.list_retractions(subject_id="u", signal="health_weight")
    view = second.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"]
    assert view.value is None and view.last_known is None, view
    aggregates = storage.get_aggregate(subject_id="u", signal="health_weight", start_date=DAY, end_date=DAY)
    assert all(not a.typed_aggregate.get("weight_kg") for a in aggregates), aggregates


def test_acceptance_A14_relative_jump_does_not_become_current_and_survives_retry():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    weigh(kit, 70)
    out = weigh(kit, 150, eid="jump", at=T + timedelta(hours=1))
    assert len(out.conflicts) == 1 and not out.rejected, out
    assert kit.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"].value == {"weight_kg": 70}
    # A new Kit/report cannot silently relabel the unresolved candidate applied.
    retry = weigh(PerceptionKit(storage), 150, eid="jump", at=T + timedelta(hours=1), report_id="retry-jump")
    assert len(retry.conflicts) == 1 and not retry.applied and not retry.duplicates, retry


@pytest.mark.parametrize("unit,value,canonical", [("lb", 154, 69.85322498), ("g", 70000, 70)])
def test_acceptance_A15_units_convert_before_validation(unit, value, canonical):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    weigh(kit, 70)
    out = ingest(kit, [observation({"weight_kg": value}, eid="converted", at=T + timedelta(hours=1),
                                  units={"weight_kg": unit})], report_id="converted", at=T + timedelta(hours=1))
    assert not out.rejected and not out.conflicts and len(out.applied) == 1, out
    current = kit.get_current(subject_id="u", signals=["health_weight"], now=T + timedelta(hours=1))["health_weight"]
    assert current.value["weight_kg"] == pytest.approx(canonical)


@pytest.mark.parametrize("units", [None, {}], ids=["omitted", "field_omitted"])
def test_acceptance_A15_unspecified_unit_means_canonical(units):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    extras = {} if units is None else {"units": units}
    out = ingest(kit, [observation({"weight_kg": 70}, **extras)])
    assert len(out.applied) == 1 and not out.rejected
    assert kit.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"].value == {"weight_kg": 70}


def test_acceptance_A16_invalid_explicit_timezone_rejects_only_that_observation():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, timezone_fallback="UTC")
    out = ingest(kit, [observation({"weight_kg": 70}, source_timezone="Invalid/Nowhere"),
                      observation({"weight_kg": 71}, eid="valid", at=T + timedelta(minutes=1))])
    assert ([i for i, _ in out.rejected], [x.stored.source_event_id for x in out.applied]) == ([0], ["valid"])
    assert {o.source_event_id for o in storage.observations.values()} == {"valid"}


@pytest.mark.parametrize("after", ["upgraded", "deleted"])
def test_acceptance_A21_persistent_provider_explains_event_after_kit_restart(tmp_path, after):
    class SqliteDefinitions:
        """Real durable test provider exercising the existing public port."""
        def __init__(self, path):
            self.connection = sqlite3.connect(path)
            self.connection.execute("CREATE TABLE IF NOT EXISTS definitions (id TEXT, version INTEGER, body TEXT, active INTEGER, PRIMARY KEY(id, version))")

        def put(self, definition):
            from dataclasses import asdict
            self.connection.execute("UPDATE definitions SET active=0 WHERE id=?", (definition.definition_id,))
            self.connection.execute("INSERT INTO definitions VALUES (?, ?, ?, 1)",
                                    (definition.definition_id, definition.version, json.dumps(asdict(definition))))
            self.connection.commit()

        def definitions_for(self, subject_id):
            return tuple(self._load(row[0]) for row in self.connection.execute("SELECT body FROM definitions WHERE active=1"))

        def definition_at(self, definition_id, version):
            row = self.connection.execute("SELECT body FROM definitions WHERE id=? AND version=?", (definition_id, version)).fetchone()
            return self._load(row[0]) if row else None

        @staticmethod
        def _load(body):
            from perceptkit.rules.types import Lifecycle
            payload = json.loads(body)
            payload["lifecycle"] = Lifecycle(**payload["lifecycle"])
            return EventDefinition(**payload)

    path = tmp_path / "definitions.sqlite"
    provider = SqliteDefinitions(path)
    provider.put(weight_rule())
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=provider)
    weigh(kit, 70)
    assert len(weigh(kit, 72, eid="B", at=T + timedelta(hours=1)).events) == 1
    if after == "upgraded":
        provider.put(weight_rule(version=2))
    else:
        provider.connection.execute("UPDATE definitions SET active=0")
        provider.connection.commit()
    provider.connection.close()
    restarted_provider = SqliteDefinitions(path)
    restarted = PerceptionKit(storage, definitions=restarted_provider)
    event = next(iter(storage.outbox.values()))
    old = restarted.definition_at(event.definition_id, event.definition_version)
    assert old is not None and old.version == 1 and old.value == 71
    assert [d.version for d in restarted.definitions_for("u")] == ([2] if after == "upgraded" else [])
    restarted_provider.connection.close()
