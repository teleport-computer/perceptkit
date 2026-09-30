"""Deterministic, non-delivering replay after a Fact correction/deletion.

State is a projection. Definition versions and canonical references survive
independently of values; expired evidence produces an explicit safe baseline.
"""
from dataclasses import replace

from ..contracts.context import IngestContext
from ..contracts.observation import Observation
from ..contracts.mutation import canonical_keys, rule_key
from ..contracts.errors import RuleStateAttributionIncompleteError
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


def subject_events(storage, subject):
    rows = []
    offset = 0
    while True:
        page = storage.list_events(subject_id=subject, offset=offset, limit=500)
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def repair_scopes(storage, *, subject, signal, definitions, definition_at=None, days=None):
    """Read-only planning before any writes. Includes archived states/versions."""
    available = {(d.definition_id, d.version): d for d in definitions}
    events = subject_events(storage, subject)
    states = storage.list_rule_states(subject_id=subject)
    conservative_scopes = set()
    for event in events:
        if (event.fact_snapshot.get("signal") != signal
                or event.fact_dependencies_complete and event.fact_dependencies):
            continue
        scope = event.fact_snapshot.get("context", {}).get("scope")
        if not scope:
            candidates = [key for did, key, _ in states if did == event.definition_id
                          and key.endswith(f"@v{event.definition_version}")]
            if len(candidates) != 1:
                raise RuleStateAttributionIncompleteError(subject, event.definition_id, "unknown-event-scope")
            scope = candidates[0]
        if not scope.endswith(f"@v{event.definition_version}"):
            raise RuleStateAttributionIncompleteError(subject, event.definition_id, scope)
        conservative_scopes.add((event.definition_id, scope))
    result = []
    for did, scope, raw in states:
        version = int(scope.rsplit("@v", 1)[-1])
        d = available.get((did, version))
        if d is None and definition_at is not None:
            d = definition_at(did, version)
        known_signals = {value for value in (d.signal if d else None, raw.get("signal")) if value}
        known_signals.update(event.fact_snapshot.get("signal") for event in events
                             if event.definition_id == did and event.definition_version == version
                             and event.fact_snapshot.get("context", {}).get("scope") == scope
                             and event.fact_snapshot.get("signal"))
        if len(known_signals) != 1:
            raise RuleStateAttributionIncompleteError(subject, did, scope)
        if signal not in known_signals:
            continue
        day = scope.rsplit("@v", 1)[0]
        if (days is not None and day != "forever" and day not in {str(x) for x in days}
                and d is not None and d.condition_type not in ("streak", "absence")
                and (did, scope) not in conservative_scopes):
            continue
        result.append((did, scope, d))
    for did, scope in conservative_scopes:
        if any(key == did and existing == scope for key, existing, _ in result):
            continue
        version = int(scope.rsplit("@v", 1)[-1])
        definition = available.get((did, version))
        if definition is None and definition_at is not None:
            definition = definition_at(did, version)
        result.append((did, scope, definition))
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
    events = [e for e in subject_events(storage, subject) if e.invalidated_at is None] if scopes else []
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
        else:
            last_audit_at = None
            for row in rows:
                values = row.typed_value or {}
                if not precondition_met(definition, values)[0]:
                    continue
                result = evaluate(definition, state, values.get(definition.field_name),
                                  now=row.received_at,
                                  context={"source_event_id": row.source_event_id or row.observation_id,
                                           "signal": row.signal, "occurred_at": row.occurred_at},
                                  extra_evaluators=extra_evaluators)
                # Replaying a hypothetical trigger must not consume a real
                # delivery opportunity. Only surviving recorded triggers do.
                triggers = [e for e in valid_events if any(
                    ref.get("observation_id") == row.observation_id and ref.get("role") == "current"
                    for ref in e.fact_dependencies)]
                last_audit_at = max([e.detected_at for e in triggers] +
                                    ([last_audit_at] if last_audit_at else []), default=None)
                state = replace(result.state, fired_in_scope=state.fired_in_scope or bool(triggers),
                                last_fired_at=last_audit_at.isoformat() if last_audit_at else None)
                previous = references[row.observation_id]
        # Event existence is independent evidence of a real trigger, even if
        # late Facts mean replay no longer crosses at that row. Its audit clock,
        # not occurred_at, controls cooldown. This also covers missing history.
        latest_trigger = max((e.detected_at for e in valid_events), default=None)
        state = replace(state, fired_in_scope=bool(valid_events),
                        last_fired_at=latest_trigger.isoformat() if latest_trigger else None)
        output = state.to_dict()
        output.update(signal=signal.key, previous_fact=previous,
                      completeness="incomplete" if incomplete else "complete",
                      incomplete_reason="history_or_definition_unavailable" if incomplete else None)
        storage.put_rule_state(subject_id=subject, definition_id=did, scope_key=scope, state=output)
