"""Executable conformance for durable DefinitionProvider implementations."""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Sequence

from ..ports.definitions import DefinitionArchiveConflictError
from ..rules.types import EventDefinition


DefinitionProviderFactory = Callable[[Sequence[EventDefinition]], object]


def _rule(version: int, value: int) -> EventDefinition:
    return EventDefinition.parse({
        "id": "conformance-rule", "version": version,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "threshold_crossing", "operator": "gte", "value": value},
        "event": {"type": "conformance.steps"},
    })


def run_definition_provider_conformance(factory: DefinitionProviderFactory) -> list[str]:
    """Return provider problems; fresh factory calls must share durable history.

    The factory receives the definitions that are currently live. It must return
    a *fresh provider instance* over the same durable backing store each time.
    """
    problems: list[str] = []
    v1, v2 = _rule(1, 100), _rule(2, 200)
    first = factory((v1,))
    if not bool(getattr(first, "persistent", False)):
        problems.append("provider does not explicitly assert persistent=True")
    try:
        first.archive_definition(v1)
        first.archive_definition(v1)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"equivalent archive retry failed: {type(exc).__name__}: {exc}")
        return problems
    restarted = factory((v2,))
    if restarted.definition_at(v1.definition_id, v1.version) != v1:
        problems.append("v1 missing after provider restart/upgrade")
    if tuple(restarted.definitions_for("u")) != (v2,):
        problems.append("current definitions did not follow host upgrade")
    restarted.archive_definition(v2)
    deleted = factory(())
    if deleted.definition_at(v1.definition_id, 1) != v1 or deleted.definition_at(v2.definition_id, 2) != v2:
        problems.append("history missing after current rule deletion")
    if tuple(deleted.definitions_for("u")):
        problems.append("deleted rule remained active")
    try:
        deleted.archive_definition(replace(v1, value=999))
    except DefinitionArchiveConflictError:
        pass
    except Exception as exc:  # noqa: BLE001
        problems.append(f"immutable conflict raised wrong error: {type(exc).__name__}")
    else:
        problems.append("conflicting content replaced immutable definition version")

    # The provider contract matters at the transaction boundary too: immutable
    # definition history must exist before an Event references it, and archival
    # failure must roll the whole ingest back.
    event_rule = EventDefinition.parse({
        "id": "conformance-event-rule", "version": 1,
        "source": {"signal": "steps"},
        "condition": {"type": "occurrence"},
        "event": {"type": "conformance.event"},
    })
    event_provider = factory((event_rule,))
    try:
        from datetime import datetime, timezone
        from ..contracts.context import IngestContext
        from ..kit import PerceptionKit
        from .memory import InMemoryStorage

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = {
            "schema_version": 1, "report_id": "definition-conformance",
            "producer": "conformance",
            "observations": [{
                "signal": "steps", "signal_schema_version": 1,
                "occurred_at": now.isoformat(), "local_date": now.date().isoformat(),
                "availability": "observed", "source_event_id": "sample-1",
                "value": {"step_count": 1},
            }],
        }

        class TrackingProvider:
            persistent = True
            archived = False
            def definitions_for(self, subject_id):
                return event_provider.definitions_for(subject_id)
            def definition_at(self, definition_id, version):
                return event_provider.definition_at(definition_id, version)
            def archive_definition(self, definition):
                event_provider.archive_definition(definition)
                self.archived = True

        tracking = TrackingProvider()

        class ArchiveOrderStorage(InMemoryStorage):
            def enqueue_event(self, entry):
                if not tracking.archived:
                    raise AssertionError("event referenced definition before durable archive")
                return super().enqueue_event(entry)

        ordered = ArchiveOrderStorage()
        result = PerceptionKit(ordered, definitions=tracking).ingest(
            report, context=IngestContext("u", now))
        if not result.events:
            problems.append("archive-before-Event check did not produce an Event")

        class ArchiveFailureProvider:
            persistent = True
            def definitions_for(self, subject_id):
                return (event_rule,)
            def definition_at(self, definition_id, version):
                return None
            def archive_definition(self, definition):
                raise RuntimeError("archive unavailable")

        failed_store = InMemoryStorage()
        try:
            PerceptionKit(failed_store, definitions=ArchiveFailureProvider()).ingest(
                {**report, "report_id": "archive-failure"},
                context=IngestContext("u", now))
        except RuntimeError as exc:
            if str(exc) != "archive unavailable":
                problems.append("archive failure surfaced a different error")
        else:
            problems.append("archive failure did not fail ingest")
        if failed_store.reports or failed_store.observations or failed_store.outbox:
            problems.append("archive failure did not roll back Report/Observation/Event")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"archive/Event transactional conformance failed: {type(exc).__name__}: {exc}")
    return problems


__all__ = ["DefinitionProviderFactory", "run_definition_provider_conformance"]
