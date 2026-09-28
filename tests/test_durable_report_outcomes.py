from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.rules import EventDefinition


T = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)


def observation(*, count=10, eid="sample"):
    return {
        "signal": "steps", "signal_schema_version": 1,
        "occurred_at": T.isoformat(), "local_date": T.date().isoformat(),
        "availability": "observed", "source_event_id": eid,
        "source_revision": 1, "value": {"step_count": count},
    }


def report(report_id, observations):
    return {"schema_version": 1, "report_id": report_id, "producer": "ios",
            "observations": observations}


def occurrence_rule():
    return EventDefinition.parse({
        "id": "steps-seen", "version": 1,
        "source": {"signal": "steps"},
        "condition": {"type": "occurrence"},
        "event": {"type": "steps.seen"},
    })


def test_mixed_report_is_accepted_and_replay_returns_durable_item_failures():
    storage = InMemoryStorage()
    payload = report("mixed", [
        {"signal": "unknown_private_signal", "signal_schema_version": 1,
         "occurred_at": T.isoformat(), "availability": "observed", "value": {}},
        observation(),
    ])
    first = PerceptionKit(storage, definitions=[occurrence_rule()]).ingest(
        payload, context=IngestContext("u", T))

    assert first.receipt.status == "accepted"
    assert first.receipt.error_code is None
    assert first.receipt.observations_applied == 1
    assert [(r.index, r.problems) for r in first.receipt.observations_rejected] == [
        (0, ("unknown_private_signal: manifest 里没有这个信号",))]
    assert len(storage.observations) == len(storage.identities) == len(storage.outbox) == 1
    durable = storage.reports[("u", "ios", "mixed")]
    assert durable == first.receipt

    replay = PerceptionKit(storage, definitions=[occurrence_rule()]).ingest(
        payload, context=IngestContext("u", T))
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.observations_applied == 0
    assert replay.receipt.observations_rejected == first.receipt.observations_rejected
    assert replay.rejected == [(0, ("unknown_private_signal: manifest 里没有这个信号",))]
    assert len(storage.observations) == len(storage.identities) == len(storage.outbox) == 1


def test_all_invalid_report_is_accepted_with_zero_facts_and_replays_without_work():
    storage = InMemoryStorage()
    payload = report("invalid", [
        observation(count=-500_001, eid="secret-invalid-value"),
        {"signal": "unknown", "signal_schema_version": 1,
         "occurred_at": T.isoformat(), "availability": "observed", "value": {}},
    ])
    first = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T))
    assert first.receipt.status == "accepted"
    assert first.receipt.observations_applied == 0
    assert [r.index for r in first.receipt.observations_rejected] == [0, 1]
    assert "-500001" not in str(first.receipt.observations_rejected)
    assert not storage.observations and not storage.identities and not storage.outbox

    before = deepcopy(storage.reports)
    replay = PerceptionKit(storage).ingest(payload, context=IngestContext("u", T))
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.observations_rejected == first.receipt.observations_rejected
    assert storage.reports == before
    assert not storage.observations and not storage.identities and not storage.outbox


def test_whole_batch_preflight_failure_is_rejected_and_writes_no_report_or_fact():
    storage = InMemoryStorage()
    result = PerceptionKit(storage, max_observations=1).ingest(
        report("too-many", [observation(eid="a"), observation(eid="b")]),
        context=IngestContext("u", T))
    assert result.receipt.status == "rejected"
    assert result.receipt.error_code == "too_many_observations"
    assert result.receipt.observations_rejected == ()
    assert not storage.reports and not storage.observations and not storage.identities


def test_report_digest_conflict_and_fact_conflict_have_distinct_durable_outcomes():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    original = report("report-id", [observation(count=10)])
    assert kit.ingest(original, context=IngestContext("u", T)).receipt.status == "accepted"

    digest_conflict = kit.ingest(
        report("report-id", [observation(count=11)]), context=IngestContext("u", T))
    assert digest_conflict.receipt.status == "conflict"
    assert digest_conflict.receipt.error_code == "report_digest_conflict"
    assert digest_conflict.receipt.observations_rejected == ()

    fact_payload = report("new-report-id", [observation(count=12)])
    fact_conflict = kit.ingest(fact_payload, context=IngestContext("u", T))
    assert fact_conflict.receipt.status == "accepted"
    assert fact_conflict.receipt.error_code is None
    assert [(r.index, r.problems) for r in fact_conflict.receipt.observations_rejected] == [
        (0, ("fact_conflict",))]
    replay = PerceptionKit(storage).ingest(fact_payload, context=IngestContext("u", T))
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.observations_rejected == fact_conflict.receipt.observations_rejected


def test_report_outcome_and_sibling_facts_roll_back_together():
    class BrokenFinalize(InMemoryStorage):
        def finalize_report(self, receipt):
            super().finalize_report(receipt)
            raise RuntimeError("finalize crashed")

    storage = BrokenFinalize()
    with pytest.raises(RuntimeError, match="finalize crashed"):
        PerceptionKit(storage).ingest(
            report("mixed", [
                observation(count=-5, eid="bad"), observation(eid="good")]),
            context=IngestContext("u", T))
    assert not storage.reports and not storage.observations
    assert not storage.identities and not storage.current
    assert not storage.aggregates and not storage.outbox
