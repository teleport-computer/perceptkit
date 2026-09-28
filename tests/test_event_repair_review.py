"""Review regressions: durable lifecycle evidence and complete repair planning."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.rules import Lifecycle
from test_acceptance_regressions_0_9 import T, ingest, observation, weigh, weight_rule, retract
from test_event_repair import state


def delayed(kit, value, eid, *, occurred, received):
    result = ingest(kit, [observation({"weight_kg": value}, eid=eid, at=occurred)],
                    report_id=eid, at=received)
    assert result.applied
    return result


@pytest.mark.parametrize("delivery_state", ["pending", "delivered", "not_dispatched"])
def test_surviving_trigger_occupies_once_even_if_late_fact_changes_replayed_crossing(delivery_state):
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[weight_rule()])
    weigh(kit, 70, eid="base")
    original = weigh(kit, 72, eid="trigger", at=T + timedelta(hours=2)).events[0]
    s.outbox[original.event_id] = replace(s.outbox[original.event_id], delivery_state=delivery_state)
    weigh(kit, 73, eid="late", at=T + timedelta(hours=1))
    weigh(kit, 69, eid="unrelated", at=T + timedelta(hours=3))
    retract(kit, "unrelated")
    assert s.outbox[original.event_id].invalidated_at is None
    assert state(s)["fired_in_scope"] is True
    weigh(kit, 69, eid="new-low", at=T + timedelta(hours=4))
    assert not weigh(kit, 74, eid="new-high", at=T + timedelta(hours=5)).events
    assert len(s.outbox) == 1


@pytest.mark.parametrize("multiple", [False, True])
def test_cooldown_uses_latest_surviving_detection_time_not_fact_time(multiple):
    rule = replace(weight_rule(), lifecycle=Lifecycle(fire="every", rearm="cooldown", cooldown_seconds=3600))
    s = InMemoryStorage(); kit = PerceptionKit(s, definitions=[rule])
    delayed(kit, 70, "base", occurred=T, received=T + timedelta(hours=6))
    assert delayed(kit, 72, "first", occurred=T + timedelta(hours=1), received=T + timedelta(hours=7)).events
    latest = T + timedelta(hours=7)
    if multiple:
        delayed(kit, 69, "second-base", occurred=T + timedelta(hours=2), received=T + timedelta(hours=8))
        latest = T + timedelta(hours=9)
        assert delayed(kit, 73, "second", occurred=T + timedelta(hours=3), received=latest).events
    delayed(kit, 68, "unrelated", occurred=T + timedelta(hours=4), received=latest + timedelta(minutes=1))
    retract(kit, "unrelated")
    assert state(s)["last_fired_at"] == latest.isoformat()
    delayed(kit, 68, "fresh-low", occurred=T + timedelta(hours=5), received=latest + timedelta(minutes=2))
    out = delayed(kit, 74, "fresh-high", occurred=T + timedelta(hours=6), received=latest + timedelta(minutes=3))
    assert not out.events
    assert len(s.outbox) == (2 if multiple else 1)


@pytest.mark.parametrize("operation", ["retract", "correct"])
def test_legacy_state_without_signal_or_archive_uses_original_outbox_attribution(operation):
    s = InMemoryStorage(); old = PerceptionKit(s, definitions=[weight_rule()])
    weigh(old, 70, eid="base")
    weigh(old, 72, eid="trigger", revision=1, at=T + timedelta(hours=1))
    for raw in s.rule_state.values():
        raw.pop("signal")
    kit = PerceptionKit(s, definitions=[replace(weight_rule(version=2), value=80)])
    if operation == "retract":
        retract(kit)
    else:
        weigh(kit, 69, eid="trigger", revision=2, at=T + timedelta(hours=1))
    assert state(s)["previous_value"] is None
    assert state(s)["fired_in_scope"] is False
    assert state(s)["completeness"] == "incomplete"
    assert state(s)["signal"] == "health_weight"


@pytest.mark.parametrize("operation", ["retract", "correct"])
def test_unattributable_legacy_state_requires_migration_before_any_write(operation):
    class NoWrites(InMemoryStorage):
        armed = False
        def claim_report(self, **kwargs):
            assert not self.armed, "migration must be identified before report write"
            return super().claim_report(**kwargs)
        def record_retraction(self, row):
            assert not self.armed, "migration must be identified before tombstone write"
            return super().record_retraction(row)
    s = NoWrites(); kit = PerceptionKit(s)
    weigh(kit, 72, eid="trigger", revision=1)
    s.rule_state[("u", "legacy-lost", "2026-09-06@v1")] = {
        "previous_value": 72, "fired_in_scope": True, "last_fired_at": T.isoformat()}
    before = deepcopy((s.rule_state, s.current, s.observations, s.reports, s.retractions))
    s.armed = True
    try:
        if operation == "retract":
            retract(kit)
        else:
            weigh(kit, 69, eid="trigger", revision=2)
    except Exception as exc:
        assert getattr(exc, "code", None) == "rule_state_attribution_incomplete", repr(exc)
        assert getattr(exc, "retryable", None) is False
        assert getattr(exc, "recovery_action", None) == "restore_rule_state_attribution"
    else:
        pytest.fail("unattributable legacy state cannot be silently left with an invalid value")
    assert (s.rule_state, s.current, s.observations, s.reports, s.retractions) == before


@pytest.mark.parametrize("operation", ["retract", "correct"])
def test_broad_legacy_invalidation_also_preowns_and_repairs_old_day_scope(operation):
    from test_storage_concurrency_contract import AuditedStorage
    s = AuditedStorage(); kit = PerceptionKit(s, definitions=[weight_rule()])
    weigh(kit, 70, eid="base")
    old = weigh(kit, 72, eid="trigger", at=T + timedelta(hours=1)).events[0]
    s.outbox[old.event_id] = replace(s.outbox[old.event_id], fact_dependencies=(), fact_dependencies_complete=False)
    weigh(kit, 69, eid="day2", revision=1, at=T + timedelta(days=1))
    s.requests.clear()
    if operation == "retract":
        retract(kit, "day2")
    else:
        weigh(kit, 68, eid="day2", revision=2, at=T + timedelta(days=1))
    assert s.outbox[old.event_id].delivery_state == "invalidated"
    assert state(s)["fired_in_scope"] is False
    assert ("40_rule", "u", "weight", "2026-09-06@v1") in {key for batch in s.requests for key in batch}
