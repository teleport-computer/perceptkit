from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import AggregateGeneration, DailyAggregate, IngestContext
from perceptkit.contracts.retraction import Retraction
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.queries import api
from perceptkit.rules import EventDefinition


T = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)


def _daily(day: date, version: int, total: int, *, generation_id: str | None = None,
           completeness: str = "complete") -> DailyAggregate:
    return DailyAggregate(
        subject_id="u", signal="steps", local_date=day,
        aggregation_kind="daily", aggregation_version=version,
        typed_aggregate={"step_count": {"total": total}},
        generation_id=generation_id, completeness=completeness,
    )


def _generation(gid: str, version: int, start: date, end: date,
                *, status: str = "building", accounted=()) -> AggregateGeneration:
    return AggregateGeneration(
        generation_id=gid, subject_id="u", signal="steps",
        aggregation_kind="daily", aggregation_version=version,
        requested_start_date=start, requested_end_date=end, status=status,
        completeness="complete" if status in {"complete", "active"} else "unknown",
        accounted_dates=tuple(accounted), created_at=T, updated_at=T,
    )


def test_candidate_is_invisible_until_atomic_activation_and_range_is_coherent():
    s = InMemoryStorage()
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    s.put_aggregate(_daily(d1, 2, 100))
    s.put_aggregate(_daily(d2, 2, 200))
    old = s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                             aggregation_kind="daily")
    assert old is not None

    candidate = _generation("rebuild-3", 3, d1, d2)
    s.put_aggregate_generation(candidate)
    s.put_aggregate(_daily(d1, 3, 1000, generation_id="rebuild-3"))
    s.put_aggregate(_daily(d2, 3, 2000, generation_id="rebuild-3"))

    before = api.get_daily_aggregates(s, subject_id="u", signal="steps",
                                      start_date=d1, end_date=d2)
    assert [row.value["step_count"]["total"] for row in before] == [100, 200]

    complete = replace(candidate, status="complete", completeness="complete",
                       accounted_dates=(d1, d2), updated_at=T + timedelta(minutes=1))
    s.update_aggregate_generation(complete)
    assert s.activate_aggregate_generation(
        subject_id="u", signal="steps", aggregation_kind="daily",
        generation_id="rebuild-3", expected_active_generation_id=old.generation_id,
        activated_at=T + timedelta(minutes=2),
    )
    after = api.get_daily_aggregates(s, subject_id="u", signal="steps",
                                     start_date=d1, end_date=d2)
    assert [row.value["step_count"]["total"] for row in after] == [1000, 2000]


def test_partial_or_failed_candidate_cannot_replace_active_generation():
    s = InMemoryStorage()
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    s.put_aggregate(_daily(d1, 2, 100))
    old = s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                             aggregation_kind="daily")
    candidate = _generation("partial", 3, d1, d2, status="complete", accounted=(d1,))
    s.put_aggregate_generation(candidate)
    s.put_aggregate(_daily(d1, 3, 999, generation_id="partial"))
    assert not s.activate_aggregate_generation(
        subject_id="u", signal="steps", aggregation_kind="daily",
        generation_id="partial", expected_active_generation_id=old.generation_id,
        activated_at=T,
    )
    assert s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                              aggregation_kind="daily") == old


def test_narrow_candidate_cannot_collapse_broader_active_history():
    s = InMemoryStorage()
    start = date(2026, 6, 1)
    end = date(2026, 8, 29)
    s.put_aggregate(_daily(start, 2, 100))
    s.put_aggregate(_daily(end, 2, 200))
    old = s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                             aggregation_kind="daily")
    narrow = _generation("one-day", 3, end, end, status="complete", accounted=(end,))
    s.put_aggregate_generation(narrow)
    s.put_aggregate(_daily(end, 3, 999, generation_id="one-day"))
    assert not s.activate_aggregate_generation(
        subject_id="u", signal="steps", aggregation_kind="daily",
        generation_id="one-day", expected_active_generation_id=old.generation_id,
        activated_at=T)
    still_visible = api.get_daily_aggregates(
        s, subject_id="u", signal="steps", start_date=start, end_date=end)
    assert [(row.date, row.value["step_count"]["total"]) for row in still_visible] == [
        (start.isoformat(), 100), (end.isoformat(), 200)]


def test_scheduled_streak_reads_only_the_active_complete_generation():
    s = InMemoryStorage()
    start = date(2026, 8, 26)
    for i in range(3):
        day = start + timedelta(days=i)
        s.put_aggregate(DailyAggregate(
            "u", "health_sleep", day, "daily", 2,
            {"duration_minutes": {"total": 300}}))
    candidate = AggregateGeneration(
        "sleep-v3", "u", "health_sleep", "daily", 3,
        start, start + timedelta(days=2), created_at=T, updated_at=T)
    s.put_aggregate_generation(candidate)
    for i in range(3):
        day = start + timedelta(days=i)
        s.put_aggregate(DailyAggregate(
            "u", "health_sleep", day, "daily", 3,
            {"duration_minutes": {"total": 480}}, generation_id="sleep-v3"))
    rule = EventDefinition.parse({
        "id": "short-3", "version": 1,
        "source": {"signal": "health_sleep", "field": "duration_minutes"},
        "condition": {"type": "streak", "operator": "lt", "value": 360,
                      "params": {"periods": 3}},
        "event": {"type": "sleep.short"},
    })
    out = PerceptionKit(s, definitions=[rule]).evaluate_daily(
        subject_id="u", local_date=start + timedelta(days=2), now=T)
    assert len(out.events) == 1


def test_rebuild_attempt_identity_is_not_algorithm_version_and_retry_is_distinct():
    s = InMemoryStorage()
    kit = PerceptionKit(s)
    for suffix in ("a", "b"):
        kit.ingest({
            "schema_version": 1, "report_id": suffix, "producer": "ios",
            "observations": [{"signal": "steps", "signal_schema_version": 1,
                              "occurred_at": T.isoformat(), "local_date": "2026-08-28",
                              "availability": "observed", "source_event_id": suffix,
                              "value": {"step_count": 10}}]},
            context=IngestContext("u", T))
    first = kit.recompute_aggregates(subject_id="u", signal="steps", start=T.date(),
                                     end=T.date(), now=T, version=3)
    second = kit.recompute_aggregates(subject_id="u", signal="steps", start=T.date(),
                                      end=T.date(), now=T, version=3)
    assert first.generation_id != second.generation_id
    assert first.activated and second.activated


def test_allow_incomplete_creates_audit_candidate_but_never_activates_it():
    s = InMemoryStorage()
    s.put_aggregate(DailyAggregate("u", "focus_state", date(2024, 1, 1), "daily", 2,
                                   {"duration_minutes": {"total": 480}}))
    old = s.get_active_aggregate_generation(subject_id="u", signal="focus_state",
                                             aggregation_kind="daily")
    out = PerceptionKit(s).recompute_aggregates(
        subject_id="u", signal="focus_state", start=date(2024, 1, 1),
        end=date(2024, 1, 1), now=T, version=3, allow_incomplete=True)
    assert not out.ok and not out.activated
    assert out.incomplete == [(date(2024, 1, 1), "detail_retention_expired")]
    assert s.get_active_aggregate_generation(subject_id="u", signal="focus_state",
                                              aggregation_kind="daily") == old
    audit = [r for r in s.get_aggregate(subject_id="u", signal="focus_state",
                                        start_date=date(2024, 1, 1), end_date=date(2024, 1, 1))
             if r.generation_id == out.generation_id]
    assert audit and audit[0].completeness == "incomplete"


def test_retraction_after_detail_retention_marks_the_impacted_day_incomplete():
    old = datetime(2024, 1, 1, 9, tzinfo=timezone.utc)
    s = InMemoryStorage()
    signals = dict(MINIMAL_SIGNALS)
    signals["steps"] = replace(signals["steps"], history_retention_days=1)
    kit = PerceptionKit(s, signals=signals)
    kit.ingest({
        "schema_version": 1, "report_id": "old", "producer": "ios",
        "observations": [{"signal": "steps", "signal_schema_version": 1,
                          "occurred_at": old.isoformat(), "local_date": "2024-01-01",
                          "availability": "observed", "source_event_id": "focus-1",
                          "value": {"step_count": 70}}]},
        context=IngestContext("u", old))
    s.delete_observations(subject_id="u", signal="steps", before=T)

    kit.apply_retractions([Retraction("u", "steps", "focus-1", "ios", T)], now=T)
    rows = kit.get_daily(subject_id="u", signal="steps",
                         start=old.date(), end=old.date())
    assert rows and rows[0].completeness == "incomplete"
    assert "detail_retention_expired" in rows[0].incomplete_reasons

    trend = kit.get_trend(subject_id="u", signal="steps", field="step_count",
                          start=old.date(), end=old.date())
    assert trend["days_with_data"] == 0
    assert trend["days_incomplete"] == 1


def test_correction_after_detail_retention_rejects_and_marks_old_projection_incomplete():
    old = datetime(2024, 1, 1, 9, tzinfo=timezone.utc)
    s = InMemoryStorage()
    signals = dict(MINIMAL_SIGNALS)
    signals["steps"] = replace(signals["steps"], history_retention_days=1)
    kit = PerceptionKit(s, signals=signals)
    def report(report_id, revision, count):
        return {"schema_version": 1, "report_id": report_id, "producer": "ios",
                "observations": [{"signal": "steps", "signal_schema_version": 1,
                                  "occurred_at": old.isoformat(),
                                  "local_date": old.date().isoformat(),
                                  "availability": "observed", "source_event_id": "fact",
                                  "source_revision": revision,
                                  "value": {"step_count": count}}]}
    assert kit.ingest(report("v1", 1, 100), context=IngestContext("u", old)).ok
    s.delete_observations(subject_id="u", signal="steps", before=T)
    outcome = kit.ingest(report("v2", 2, 200), context=IngestContext("u", T))
    assert not outcome.ok and outcome.receipt.error_code == "fact_revision_details_incomplete"
    rows = kit.get_daily(subject_id="u", signal="steps", start=old.date(), end=old.date())
    assert rows and rows[0].completeness == "incomplete"
    assert "fact_revision_details_incomplete" in rows[0].incomplete_reasons


def test_generation_and_activation_roll_back_together():
    class BrokenActivation(InMemoryStorage):
        def activate_aggregate_generation(self, **kwargs):
            raise RuntimeError("activation crashed")

    s = BrokenActivation()
    s.put_aggregate(_daily(T.date(), 2, 100))
    old = s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                             aggregation_kind="daily")
    kit = PerceptionKit(s)
    with pytest.raises(RuntimeError, match="activation crashed"):
        kit.recompute_aggregates(subject_id="u", signal="steps", start=T.date(),
                                 end=T.date(), now=T, version=3)
    assert s.get_active_aggregate_generation(subject_id="u", signal="steps",
                                              aggregation_kind="daily") == old
    failed = [g for g in s.list_aggregate_generations(
        subject_id="u", signal="steps", aggregation_kind="daily")
              if g.aggregation_version == 3]
    assert len(failed) == 1 and failed[0].status == "failed"
