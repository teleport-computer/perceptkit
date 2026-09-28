"""v0.10 public Current and bounded, complete user export contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import (
    AggregateGeneration, CalendarEventMirror, ConflictRecord, CurrentProjection, DailyAggregate,
    EventOutboxEntry, ReminderItemMirror, StoredObservation,
)
from perceptkit.queries import api

T = datetime(2026, 9, 6, 23, 30, tzinfo=timezone(timedelta(hours=8)))


def anchor(storage, dimension, *, subject="u", availability="observed", expires=None):
    storage.compare_and_put_current(CurrentProjection(
        subject_id=subject, signal="proximity_anchor", dimension_key=dimension,
        typed_value={"anchor_id": dimension, "label": dimension, "raw_identifier": "secret"},
        availability=availability, observed_at=T, received_at=T, expires_at=expires,
    ), expected_version=-1)


def test_current_preserves_independent_dimensions_order_privacy_and_subject():
    s = InMemoryStorage()
    anchor(s, "office", expires=T + timedelta(hours=1))
    anchor(s, "home", expires=T - timedelta(seconds=1))
    anchor(s, "gym", availability="unavailable")
    anchor(s, "other", subject="other")
    rows = PerceptionKit(s).get_current(subject_id="u", signals=["proximity_anchor"], now=T)["proximity_anchor"]
    assert isinstance(rows, list)
    assert [(r.dimension_key, r.state) for r in rows] == [("gym", "unavailable"), ("home", "stale"), ("office", "fresh")]
    assert rows[0].value is rows[1].value is None
    assert rows[2].value["anchor_id"] == "office"
    assert all("raw_identifier" not in r.last_known for r in rows)


def test_empty_unknown_and_no_current_are_empty_lists():
    kit = PerceptionKit(InMemoryStorage())
    assert kit.get_current(subject_id="u", signals=["steps", "unknown", "health_sleep"], now=T) == {
        "steps": [], "unknown": [], "health_sleep": []}
    assert kit.get_last_known(subject_id="u", signal="unknown") == []


def test_last_known_and_export_keep_all_dimensions():
    s = InMemoryStorage()
    anchor(s, "office")
    anchor(s, "home")
    kit = PerceptionKit(s)
    rows = kit.get_last_known(subject_id="u", signal="proximity_anchor")
    assert isinstance(rows, list)
    assert [r.dimension_key for r in rows] == ["home", "office"]
    assert all(r.state == "last_known" and r.value is None and r.as_of for r in rows)
    current = kit.export_subject(subject_id="u")["current"]["proximity_anchor"]
    assert isinstance(current, list)
    assert [r["dimension_key"] for r in current] == ["home", "office"]
    assert all(r["signal"] == "proximity_anchor" for r in current)


def seed(storage, collection, count, *, subject="u", at=T, prefix=""):
    for i in range(count):
        oid = f"{prefix}{subject}-{i}"
        obs = StoredObservation(observation_id=oid, subject_id=subject, signal="health_weight", signal_schema_version=1,
            source="ios", occurred_at=at, received_at=at, availability="observed",
            effective_local_date=at.date(), typed_value={"weight_kg": 70 + i}, source_event_id=oid)
        if collection == "health_weight":
            storage.append_observation(obs)
        elif collection == "calendar_events":
            storage.upsert_calendar_events(subject_id=subject, events=[CalendarEventMirror(
                subject, "ios", "a", "c", oid, {"title": oid, "start_at": at, "end_at": at + timedelta(minutes=1)})])
        elif collection == "reminders":
            storage.upsert_reminders(subject_id=subject, items=[ReminderItemMirror(
                subject, "ios", "a", "l", oid, {"title": oid, "is_completed": bool(i % 2)})])
        elif collection == "events":
            storage.enqueue_event(EventOutboxEntry(event_id=oid, subject_id=subject,
                definition_id="d", definition_version=1, event_type="sample", occurred_at=at,
                detected_at=at, fact_snapshot={}, delivery_state=("pending", "delivered", "invalidated")[i % 3]))
        elif collection == "conflicts":
            storage.put_conflict(ConflictRecord(oid, subject, "health_weight", "ios", oid,
                1, "semantic", "content", "revision", "different", obs, at, at))
        elif collection == "daily_aggregates:health_weight":
            storage.put_aggregate(DailyAggregate(subject, "health_weight", at.date() + timedelta(days=i),
                "daily", 1, {"weight_kg": {"latest": 70}}))
        elif collection == "aggregate_generations:health_weight":
            day = at.date() + timedelta(days=i)
            storage.put_aggregate_generation(AggregateGeneration(
                f"{prefix}failed-{subject}-{i}", subject, "health_weight", "daily", 2,
                day, day, status="failed", completeness="incomplete",
                failure_reason="build failed", created_at=at, updated_at=at))


COLLECTIONS = ["health_weight", "calendar_events", "reminders", "events", "conflicts",
               "daily_aggregates:health_weight", "aggregate_generations:health_weight"]


def exported_rows(dump, name):
    if name == "health_weight":
        return dump["observations"].get(name, [])
    if name.startswith("daily_aggregates:"):
        return dump["daily_aggregates"].get("health_weight", [])
    if name.startswith("aggregate_generations:"):
        return dump["aggregate_generations"].get("health_weight", [])
    return dump[name]


@pytest.mark.parametrize("collection", COLLECTIONS)
@pytest.mark.parametrize("count", [0, 2, 3, 4, 503])
def test_every_export_collection_has_exact_cap_and_full_drain(collection, count):
    s = InMemoryStorage()
    seed(s, collection, count)
    seed(s, collection, 1, subject="other")
    kit = PerceptionKit(s)
    capped = kit.export_subject(subject_id="u", per_signal_limit=3)
    assert len(exported_rows(capped, collection)) == min(count, 3)
    assert capped["truncated"] == ([collection] if count > 3 else [])
    full = kit.export_subject(subject_id="u")
    assert len(exported_rows(full, collection)) == count
    rows = exported_rows(full, collection)
    identities = {
        "health_weight": lambda r: r["value"]["weight_kg"],
        "calendar_events": lambda r: r["source_event_id"],
        "reminders": lambda r: r["source_reminder_id"],
        "events": lambda r: r["event_id"],
        "conflicts": lambda r: r["conflict_id"],
        "daily_aggregates:health_weight": lambda r: r["date"],
        "aggregate_generations:health_weight": lambda r: r["generation_id"],
    }
    assert len({identities[collection](r) for r in rows}) == count
    assert full["truncated"] == []
    assert "pending_events" not in full
    json.dumps(full)


def test_export_includes_every_delivery_state():
    from perceptkit.contracts.delivery import DELIVERY_STATES
    s = InMemoryStorage()
    for state in DELIVERY_STATES:
        s.enqueue_event(EventOutboxEntry(event_id=state, subject_id="u", definition_id="d",
            definition_version=1, event_type="example", occurred_at=T, detected_at=T,
            fact_snapshot={}, delivery_state=state))
    assert {e["delivery_state"] for e in PerceptionKit(s).export_subject(subject_id="u")["events"]} == DELIVERY_STATES


@pytest.mark.parametrize("name", COLLECTIONS)
def test_multi_page_cap_does_not_repeat_or_skip(name):
    s = InMemoryStorage()
    seed(s, name, 503)
    kit = PerceptionKit(s)
    full = exported_rows(kit.export_subject(subject_id="u"), name)
    capped = kit.export_subject(subject_id="u", per_signal_limit=501)
    assert exported_rows(capped, name) == full[:501]
    assert capped["truncated"] == [name]


def test_all_truncated_collection_names_are_sorted():
    s = InMemoryStorage()
    for name in COLLECTIONS:
        seed(s, name, 4)
    assert PerceptionKit(s).export_subject(subject_id="u", per_signal_limit=3)["truncated"] == sorted(COLLECTIONS)


def test_window_uses_local_boundary_dates_and_created_conflict_time():
    s = InMemoryStorage()
    for name in COLLECTIONS:
        seed(s, name, 1)
        seed(s, name, 1, prefix="outside-", at=T - timedelta(days=1))
    # Candidate inside, detection outside: window is created_at.
    conflict = s.conflicts[("u", "outside-u-0")]
    s.put_conflict(replace(conflict, conflict_id="candidate-inside", candidate=replace(conflict.candidate, occurred_at=T)))
    # End crosses local midnight while still the previous UTC day.
    end = T + timedelta(hours=1)
    s.put_aggregate(DailyAggregate("u", "health_weight", end.date(), "daily", 1, {}))
    dump = PerceptionKit(s).export_subject(subject_id="u", start=T, end=end)
    for name in ["health_weight", "events", "conflicts", "calendar_events"]:
        assert len(exported_rows(dump, name)) == 1
    assert [r["date"] for r in dump["daily_aggregates"]["health_weight"]] == [T.date().isoformat(), end.date().isoformat()]
    # The active attempt overlaps the window and the zero-row failed attempt is
    # audit data in its own right; neither depends on aggregate-row existence.
    assert {g["generation_id"] for g in dump["aggregate_generations"]["health_weight"]} == {
        "legacy-v1", "failed-u-0"}


@pytest.mark.parametrize("count,cap", [(3, 3), (4, 3), (503, 501), (503, None)])
def test_drain_reads_only_enough_and_does_not_duplicate_pages(count, cap):
    requests = []
    def fetch(cursor, limit):
        requests.append(limit)
        at = int(cursor or 0)
        rows = list(range(at, min(at + limit, count)))
        # A legal cursor can point at an empty final page.
        return rows, str(at + len(rows)) if rows else None
    rows, cut = api._drain(fetch, cap=cap)
    assert rows == list(range(count if cap is None else min(count, cap)))
    assert cut == (cap is not None and count > cap)
    assert max(requests) <= 500
    if cap is not None:
        # An opaque cursor may point at an empty page; a final one-row probe is
        # necessary then. Each request asks only for the remaining cap+1 budget.
        assert requests[0] == min(500, cap + 1)
        assert requests[-1] <= (cap + 1 if len(requests) == 1 else max(1, cap + 1 - min(500, count)))


def test_export_pushes_bounded_conflict_and_aggregate_queries_to_storage():
    class Spy(InMemoryStorage):
        def list_conflicts(self, **kwargs):
            assert kwargs.get("limit") <= 4
            assert kwargs.get("start") == T and kwargs.get("end") == T
            return super().list_conflicts(**kwargs)
        def get_aggregate(self, **kwargs):
            assert kwargs.get("limit") <= 4
            assert kwargs["start_date"] == kwargs["end_date"] == T.date()
            return super().get_aggregate(**kwargs)
        def list_aggregate_generations(self, **kwargs):
            assert kwargs.get("limit") <= 4
            assert kwargs["start_date"] == kwargs["end_date"] == T.date()
            return super().list_aggregate_generations(**kwargs)
    assert PerceptionKit(Spy()).export_subject(subject_id="u", start=T, end=T, per_signal_limit=3)["truncated"] == []


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_invalid_export_cap_is_rejected(cap):
    with pytest.raises(ValueError):
        PerceptionKit(InMemoryStorage()).export_subject(subject_id="u", per_signal_limit=cap)
