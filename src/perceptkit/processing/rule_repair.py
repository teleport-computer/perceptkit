"""Deterministic, non-delivering replay after a Fact correction/deletion.

State is a projection. Definition versions and canonical references survive
independently of values; expired evidence produces an explicit safe baseline.
"""
from dataclasses import replace

from ..contracts.context import IngestContext
from ..contracts.observation import Observation
from ..contracts.mutation import canonical_keys, rule_key
from ..rules.engine import evaluate, precondition_met, scope_key
from ..rules.types import RuleState
from .normalize import identity_for, _digest, _canonical


def fact_reference(row, signal, *, role="current"):
    obs = Observation(row.signal, row.signal_schema_version, row.occurred_at,
                      row.availability, source_event_id=row.source_event_id,
                      source_revision=row.source_revision)
    _, fact, _, _ = identity_for(obs, signal, IngestContext(row.subject_id, row.received_at),
                                 source=row.source, content_digest="")
    return dict(subject_id=row.subject_id, signal=row.signal, source=row.source,
                source_event_id=row.source_event_id, fact_key=fact,
                source_revision=row.source_revision, observation_id=row.observation_id,
                role=role)


def repair_scopes(storage, *, subject, signal, definitions, definition_at=None, days=None):
    """Read-only planning before any writes. Includes archived states/versions."""
    available = {(d.definition_id, d.version): d for d in definitions}
    result = []
    for did, scope, raw in storage.list_rule_states(subject_id=subject):
        version = int(scope.rsplit("@v", 1)[-1])
        d = available.get((did, version))
        if d is None and definition_at is not None:
            d = definition_at(did, version)
        if (d.signal if d else raw.get("signal")) != signal:
            continue
        day = scope.rsplit("@v", 1)[0]
        if (days is not None and day != "forever" and day not in {str(x) for x in days}
                and d is not None and d.condition_type not in ("streak", "absence")):
            continue
        result.append((did, scope, d))
    # A moved correction can establish a scope which has never been observed.
    for d in definitions:
        if not d.enabled or d.signal != signal or d.subject_id not in (None, subject):
            continue
        for day in days or ():
            scope = f"{scope_key(d, local_date=day)}@v{d.version}"
            if not any(did == d.definition_id and existing == scope for did, existing, _ in result):
                result.append((d.definition_id, scope, d))
    return result


def repair_keys(subject, scopes):
    return canonical_keys([rule_key(subject, did, scope) for did, scope, _ in scopes])


def rebuild_rules(storage, *, subject, signal, scopes, extra_evaluators=None):
    from .retract import _all_observations, canonical_revisions, drop_retracted

    details = _all_observations(storage, subject, signal.key) if signal.stores_history else []
    active = drop_retracted(storage, canonical_revisions(details, signal),
                            subject_id=subject, signal=signal.key)
    active = [r for r in active if r.availability == "observed"]
    references = {r.observation_id: fact_reference(r, signal) for r in active}
    active.sort(key=lambda r: (r.occurred_at, references[r.observation_id]["fact_key"],
                              str(r.source_revision), r.observation_id))
    identities = storage.list_identities(subject_id=subject, signal=signal.key)
    retained = set()
    for row in details:
        ref = fact_reference(row, signal)
        revision = "" if row.source_revision is None else str(row.source_revision)
        content = _digest(_canonical(row.typed_value), row.availability)
        retained.update((row.observation_id, _digest(ref["fact_key"], revision, content),
                         _digest(ref["fact_key"], row.occurred_at.isoformat(), revision, content)))
    # Value-free identity records make missing detail detectable after retention.
    # observation_id is the durable delivery digest in normalized Kit records.
    events = []
    offset = 0
    while scopes:
        page = storage.list_events(subject_id=subject, offset=offset, limit=500)
        events.extend(e for e in page if e.invalidated_at is None)
        if len(page) < 500:
            break
        offset += len(page)
    for did, scope, definition in scopes:
        day = scope.rsplit("@v", 1)[0]
        rows = [r for r in active if day == "forever" or str(r.effective_local_date) == day]
        missing = any(
            (day == "forever" or i.effective_local_date is None or str(i.effective_local_date) == day)
            and i.source_event_identity_digest not in retained
            for i in identities)
        raw = storage.get_rule_state(subject_id=subject, definition_id=did, scope_key=scope) or {}
        incomplete = (missing or definition is None or not signal.stores_history
                      or raw.get("completeness") == "incomplete"
                      or (definition and definition.condition_type in ("streak", "absence")))
        state = RuleState()
        previous = None
        valid_events = [e for e in events if e.definition_id == did
                        and e.definition_version == int(scope.rsplit("@v", 1)[-1])
                        and e.fact_snapshot.get("context", {}).get("scope") == scope]
        if incomplete:
            # Available last sample is a baseline, never a made-up crossing.
            if rows and definition is not None:
                valid = [r for r in rows if precondition_met(definition, r.typed_value)[0]]
                if valid:
                    last = valid[-1]
                    state = RuleState(previous_value=(last.typed_value or {}).get(definition.field_name))
                    previous = references[last.observation_id]
            if valid_events:
                # Incomplete history does not erase independent proof of a
                # surviving trigger and accidentally grant another once-slot.
                state = replace(state, fired_in_scope=True,
                                last_fired_at=max(e.detected_at for e in valid_events).isoformat())
        else:
            for row in rows:
                values = row.typed_value or {}
                if not precondition_met(definition, values)[0]:
                    continue
                result = evaluate(definition, state, values.get(definition.field_name),
                                  now=row.occurred_at,
                                  context={"source_event_id": row.source_event_id or row.observation_id,
                                           "signal": row.signal, "occurred_at": row.occurred_at},
                                  extra_evaluators=extra_evaluators)
                # Replaying a hypothetical trigger must not consume a real
                # delivery opportunity. Only surviving recorded triggers do.
                occupied = any(any(ref.get("observation_id") == row.observation_id
                                   and ref.get("role") == "current" for ref in e.fact_dependencies)
                               for e in valid_events)
                advanced = result.state
                if result.fired and not occupied:
                    advanced = replace(advanced, fired_in_scope=state.fired_in_scope,
                                       last_fired_at=state.last_fired_at)
                state = advanced
                previous = references[row.observation_id]
        output = state.to_dict()
        output.update(signal=signal.key, previous_fact=previous,
                      completeness="incomplete" if incomplete else "complete",
                      incomplete_reason="history_or_definition_unavailable" if incomplete else None)
        storage.put_rule_state(subject_id=subject, definition_id=did, scope_key=scope, state=output)
