"""Quarantine before applied identity/projections, resolve only on acceptance.

All reads/writes execute under the ingest transaction's existing Fact owner.
No pending candidate becomes an applied Observation or a remembered identity.
"""
from ..contracts.records import ConflictRecord, _compare_revisions
from ..contracts.mutation import RetryableMutationError
from ..manifest.units import relative_jump
from .normalize import _digest, identity_for


def _current_is_same_fact(current, item, sig):
    """Reconstruct the persisted Current's canonical Fact, never compare None IDs.

    Source event IDs count only for the manifest's source-ID strategy; optional
    IDs on fallback strategies cannot override the persisted timestamp/strategy.
    This comparison does not grant fallback Facts new revision capabilities.
    """
    from ..contracts.context import IngestContext
    from ..contracts.observation import Observation

    row = item.stored
    if current.source is None or current.source != row.source:
        return False
    evidence = Observation(sig.key, sig.schema_version, current.observed_at, current.availability,
                           source_event_id=current.source_event_id,
                           source_revision=current.source_revision)
    _, existing_fact, _, _ = identity_for(
        evidence, sig, IngestContext(row.subject_id, row.received_at),
        source=current.source, content_digest="")
    return existing_fact == item.fact_key


def pending_conflicts(storage, item):
    row = item.stored
    return storage.list_conflicts(subject_id=row.subject_id, signal=row.signal,
                                  source=row.source, fact_key=item.fact_key, status="pending")


def record_conflict(storage, item, *, kind, reason, now):
    row = item.stored
    # A pending candidate's initial reason is stable even if surrounding Current
    # changes before retry. Do not create a second record with a new explanation.
    for existing in pending_conflicts(storage, item):
        if existing.semantic_digest == item.semantic_digest:
            return existing
    return storage.put_conflict(ConflictRecord(
        conflict_id=_digest(item.fact_key, item.semantic_digest, kind),
        subject_id=row.subject_id, signal=row.signal, source=row.source,
        fact_key=item.fact_key, candidate_revision=row.source_revision,
        semantic_digest=item.semantic_digest, content_digest=item.content_digest,
        kind=kind, reason=reason, candidate=row, created_at=now, updated_at=now,
    ))


def blocked_by_pending(storage, item):
    pending = pending_conflicts(storage, item)
    return any(_compare_revisions(item.stored.source_revision, r.candidate_revision) != 1
               for r in pending)


def relative_jump_reason(storage, item, sig, *, correction=False):
    row = item.stored
    if row.availability != "observed" or not any(f.max_relative_jump for f in sig.fields):
        return None
    dimension = sig.dimension_key_for(row.typed_value)
    # Corrections compare to their own latest applied Fact when it is still
    # available, not an unrelated newer measurement at the end of the timeline.
    prior = []
    if (correction and sig.stores_history and sig.identity_strategy == "source_event_id"
            and row.source_event_id):
        from .retract import _all_observations, canonical_revisions, drop_retracted
        prior = [o for o in drop_retracted(storage, canonical_revisions(
                    _all_observations(storage, row.subject_id, row.signal), sig),
                    subject_id=row.subject_id, signal=row.signal)
                 if (o.source, o.source_event_id) == (row.source, row.source_event_id)
                 and o.availability == "observed"]
    previous = prior[0].typed_value if prior else None
    if previous is None:
        for current in storage.get_current(subject_id=row.subject_id, signals=[row.signal]).get(row.signal, ()):
            if current.dimension_key == dimension and current.typed_value is not None:
                same_fact = _current_is_same_fact(current, item, sig)
                if same_fact or current.observed_at <= row.occurred_at:
                    previous = current.typed_value
                    break
    # Late independent measurements compare to their real chronological
    # predecessor, never to a reading that occurred after the candidate.
    if previous is None and sig.stores_history:
        from .retract import _all_observations, canonical_revisions, drop_retracted
        earlier = [o for o in drop_retracted(storage, canonical_revisions(
                    _all_observations(storage, row.subject_id, row.signal), sig),
                    subject_id=row.subject_id, signal=row.signal)
                   if o.availability == "observed" and o.occurred_at <= row.occurred_at
                   and sig.dimension_key_for(o.typed_value) == dimension]
        if earlier:
            previous = max(earlier, key=lambda o: (o.occurred_at, o.observation_id)).typed_value
    if previous is None:
        return None
    exceeded = []
    for fd in sig.fields:
        if fd.max_relative_jump is None:
            continue
        jump = relative_jump((row.typed_value or {}).get(fd.key), previous.get(fd.key))
        if jump is not None and jump > fd.max_relative_jump:
            exceeded.append(fd.key)
    if exceeded:
        return "max_relative_jump: " + ", ".join(exceeded)
    return None


def resolve_pending(storage, item, *, now):
    row = item.stored
    for pending in pending_conflicts(storage, item):
        if not storage.resolve_conflict(subject_id=row.subject_id, conflict_id=pending.conflict_id,
                                        revision=row.source_revision, semantic_digest=item.semantic_digest,
                                        observation_id=row.observation_id, resolved_at=now):
            raise RetryableMutationError("conflict resolution changed under Fact owner")
