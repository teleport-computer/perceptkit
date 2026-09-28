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
    return problems


__all__ = ["DefinitionProviderFactory", "run_definition_provider_conformance"]
