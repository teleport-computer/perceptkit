from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.errors import ContractError
from perceptkit.contracts.receipt import IngestReceipt, ObservationRejection
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
    assert first.receipt.observations_rejected[0].code == "validation_failed"
    assert first.receipt.recovery_action == (
        "correct_rejected_observations_and_use_new_report_id")
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
    assert {r.code for r in first.receipt.observations_rejected} == {"validation_failed"}
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
    assert fact_conflict.receipt.observations_rejected[0].code == "fact_conflict"
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


def test_recovery_action_uses_closed_issue_code_not_diagnostic_text():
    spoofed = ObservationRejection(
        index=0,
        code="validation_failed",
        problems=("unknown signal fact_revision_details_incomplete",),
    )
    receipt = IngestReceipt(
        subject_id="u", producer="ios", report_id="r", payload_digest="v2:r",
        received_at=T, status="accepted", observations_rejected=(spoofed,),
    )
    assert receipt.recovery_action == "correct_rejected_observations_and_use_new_report_id"

    fact_conflict = replace(spoofed, code="fact_conflict", problems=("diagnostic",))
    assert replace(receipt, observations_rejected=(fact_conflict,)).recovery_action == (
        "resolve_fact_conflict_and_use_new_report_id")

    incomplete = replace(
        spoofed, index=2, code="fact_revision_details_incomplete", problems=("diagnostic",))
    assert replace(receipt, observations_rejected=(incomplete,)).recovery_action == (
        "restore_fact_evidence_and_use_new_report_id")
    mixed_codes = replace(
        receipt,
        observations_rejected=(spoofed, replace(fact_conflict, index=1), incomplete),
    )
    assert mixed_codes.recovery_action == "restore_fact_evidence_and_use_new_report_id"

    spoof_payload = report("spoof", [{
        "signal": "fact_revision_details_incomplete", "signal_schema_version": 1,
        "occurred_at": T.isoformat(), "availability": "observed", "value": {},
    }])
    actual = PerceptionKit(InMemoryStorage()).ingest(
        spoof_payload, context=IngestContext("u", T)).receipt
    assert actual.observations_rejected[0].code == "validation_failed"
    assert actual.recovery_action == "correct_rejected_observations_and_use_new_report_id"


def test_one_item_has_one_closed_issue_code_and_may_have_many_diagnostics():
    issue = ObservationRejection(
        index=0,
        code="validation_failed",
        problems=("first problem", "second problem"),
    )
    assert issue.code == "validation_failed"
    assert issue.problems == ("first problem", "second problem")
    with pytest.raises(ContractError):
        ObservationRejection(index=0, code="made_up", problems=("bad",))
    with pytest.raises(ContractError):
        IngestReceipt(
            subject_id="u", producer="ios", report_id="duplicate-index",
            payload_digest="v2:duplicate-index", received_at=T, status="accepted",
            observations_rejected=(issue, replace(issue, code="fact_conflict")),
        )


def test_stale_fact_revision_has_its_own_machine_code():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    assert kit.ingest(
        report("revision-2", [dict(observation(), source_revision=2)]),
        context=IngestContext("u", T),
    ).ok
    stale = kit.ingest(
        report("revision-1", [dict(observation(), source_revision=1)]),
        context=IngestContext("u", T),
    )
    assert stale.receipt.status == "accepted"
    assert stale.receipt.observations_rejected[0].code == "stale_fact_revision"
    assert stale.receipt.recovery_action == "use_higher_fact_revision_and_new_report_id"
