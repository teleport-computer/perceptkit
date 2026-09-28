"""Task 3 review round 2: incomparable revisions and legacy evidence precedence."""
from copy import deepcopy
from datetime import timedelta

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from test_acceptance_regressions_0_9 import T, DAY, ingest, observation, weight_rule, v080_storage


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_content", [False, True])
def test_incomparable_batch_revisions_never_choose_an_active_fact(reverse, same_content):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    a = observation({"weight_kg": 70}, eid="A", revision="etag-A")
    other = dict(a, source_revision="etag-B", value={"weight_kg": 70 if same_content else 72})
    sibling = observation({"weight_kg": 65}, eid="B", revision=1,
                          at=T + timedelta(minutes=1))
    batch = [a, sibling, other]
    if reverse:
        batch.reverse()
    out = ingest(kit, batch)
    assert out.receipt.status == "conflict" and not out.ok
    assert len(out.conflicts) == 2
    assert [item.stored.source_event_id for item in out.applied] == ["B"]
    assert {row.source_event_id for row in storage.observations.values()} == {"B"}
    assert all(row.source_event_id == "B" for row in storage.current.values())
    assert all(row.source_coverage["observations"] == 1 for row in storage.aggregates.values())
    assert all(state["previous_value"] == 65 for state in storage.rule_state.values())
    assert not storage.outbox


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_persisted_legacy_date_wins_over_exact_digest_with_changed_timezone(v080_storage):
    storage, original = v080_storage
    replay = deepcopy(original)
    replay["report_id"] = "changed-zone-replay"
    replay["observations"][0]["timezone"] = "Pacific/Honolulu"
    kit = PerceptionKit(storage)
    out = kit.ingest(replay, context=IngestContext("u", T))
    assert out.ok and len(out.duplicates) == 1 and not out.applied
    assert {row.effective_local_date for row in storage.identity_records.values()} == {DAY}
    correction = deepcopy(original)
    correction["report_id"] = "later-correction"
    correction["observations"][0].update(source_revision=1,
                                        value={"workout_type": "running", "duration_minutes": 20})
    corrected = kit.ingest(correction, context=IngestContext("u", T))
    assert corrected.ok and len(corrected.applied) == 1 and not corrected.rejected
    aggregate = next(iter(storage.aggregates.values()))
    assert aggregate.local_date == DAY
    assert aggregate.typed_aggregate["duration_minutes"]["total"] == 20


@pytest.mark.parametrize("v080_storage", ["details_expired"], indirect=True)
def test_opaque_exact_digest_does_not_invent_a_timezone_attribution_date(v080_storage):
    storage, original = v080_storage
    storage.current.clear()
    replay = deepcopy(original)
    replay["report_id"] = "opaque-exact-replay"
    replay["observations"][0]["timezone"] = "Pacific/Honolulu"
    out = PerceptionKit(storage).ingest(replay, context=IngestContext("u", T))
    assert out.ok and len(out.duplicates) == 1 and not out.applied
    assert len(storage.identity_records) == 1
    identity = next(iter(storage.identity_records.values()))
    assert identity.fact_key is not None
    assert identity.effective_local_date is None


@pytest.mark.parametrize("v080_storage", ["details_present"], indirect=True)
def test_restored_persisted_evidence_can_complete_an_opaque_legacy_date(v080_storage):
    storage, original = v080_storage
    old_detail = next(iter(storage.observations.values()))
    storage.observations.clear()
    storage.current.clear()
    kit = PerceptionKit(storage)
    replay = dict(original, report_id="opaque-first")
    assert len(kit.ingest(replay, context=IngestContext("u", T)).duplicates) == 1
    assert next(iter(storage.identity_records.values())).effective_local_date is None
    storage.append_observation(old_detail)
    correction = deepcopy(original)
    correction["report_id"] = "correction-after-evidence-restored"
    correction["observations"][0]["source_revision"] = 1
    out = kit.ingest(correction, context=IngestContext("u", T))
    assert out.ok and len(out.applied) == 1 and not out.rejected
    assert {row.effective_local_date for row in storage.identity_records.values()} == {DAY}
