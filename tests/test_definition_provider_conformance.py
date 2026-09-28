from __future__ import annotations

from datetime import datetime, timezone

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage, run_definition_provider_conformance
from perceptkit.contracts import IngestContext
from perceptkit.ports.definitions import DefinitionArchiveConflictError
from perceptkit.rules import EventDefinition


T = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)


def _rule(version=1, event_type="steps.seen"):
    return EventDefinition.parse({
        "id": "steps_seen", "version": version,
        "source": {"signal": "steps"},
        "condition": {"type": "occurrence"},
        "event": {"type": event_type},
    })


class DurableProvider:
    persistent = True

    def __init__(self, store, live=()):
        self.store = store
        self.live = tuple(live)

    def definitions_for(self, subject_id):
        return self.live

    def definition_at(self, definition_id, version):
        return self.store.get((definition_id, version))

    def archive_definition(self, definition):
        key = (definition.definition_id, definition.version)
        existing = self.store.get(key)
        if existing is not None and existing != definition:
            raise DefinitionArchiveConflictError(*key)
        self.store[key] = definition


def _report(report_id="r"):
    return {"schema_version": 1, "report_id": report_id, "producer": "ios",
            "observations": [{"signal": "steps", "signal_schema_version": 1,
                              "occurred_at": T.isoformat(), "local_date": T.date().isoformat(),
                              "availability": "observed", "source_event_id": report_id,
                              "value": {"step_count": 10}}]}


def test_definition_is_archived_before_event_and_survives_new_provider_instance():
    store = {}
    provider = DurableProvider(store, [_rule()])
    class ArchiveOrderStorage(InMemoryStorage):
        def enqueue_event(self, entry):
            assert (entry.definition_id, entry.definition_version) in store
            return super().enqueue_event(entry)
    storage = ArchiveOrderStorage()
    kit = PerceptionKit(storage, definitions=provider)
    out = kit.ingest(_report(), context=IngestContext("u", T))
    assert out.events
    restarted = PerceptionKit(storage, definitions=DurableProvider(store, ()))
    assert restarted.definition_at("steps_seen", 1) == _rule()
    assert restarted.definitions_for("u") == ()


def test_conflicting_same_definition_version_fails_closed_and_rolls_back_event_transaction():
    store = {("steps_seen", 1): _rule(event_type="original")}
    storage = InMemoryStorage()
    kit = PerceptionKit(storage, definitions=DurableProvider(store, [_rule(event_type="changed")]))
    with pytest.raises(DefinitionArchiveConflictError):
        kit.ingest(_report(), context=IngestContext("u", T))
    assert not storage.reports and not storage.observations and not storage.outbox


def test_provider_readiness_distinguishes_durable_provider_from_static_convenience():
    static = PerceptionKit(InMemoryStorage(), definitions=[_rule()])
    durable = PerceptionKit(InMemoryStorage(), definitions=DurableProvider({}, [_rule()]))
    assert static.definition_provider_status() == {"persistent": False, "production_ready": False}
    assert durable.definition_provider_status() == {"persistent": True, "production_ready": True}


def test_durable_provider_conformance_covers_restart_upgrade_deletion_and_conflict():
    store = {}
    assert run_definition_provider_conformance(
        lambda live: DurableProvider(store, live)) == []
