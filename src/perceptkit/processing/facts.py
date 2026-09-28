"""Fact revision decisions and evidence-based legacy identity backfill.

This is the integration seam for durable ConflictRecord (D07) and the common
Fact mutation lock (Task 4). All methods run inside the caller's transaction.
"""
from dataclasses import replace

from ..contracts.records import DurableDedupeIdentity, _compare_revisions
from .normalize import _canonical, _digest
from .retract import _all_observations


def identity_evidence(storage, item):
    """Backfill only identities demonstrably tied to persisted old facts.

    In particular, NEVER calculate an old identity using this upload's time.
    Current may retain evidence after details expire; an orphan digest cannot
    be reversed and is returned explicitly as unresolved.
    """
    stored = item.stored
    scope = dict(subject_id=stored.subject_id, signal=stored.signal, source=stored.source,
                 fact_key=item.fact_key)
    identities = list(storage.list_identities(**scope))
    missing = {r.source_event_identity_digest: r for r in identities if r.fact_key is None}
    if not missing:
        return identities
    evidence = []
    for row in _all_observations(storage, stored.subject_id, stored.signal):
        if row.source == stored.source and row.source_event_id:
            evidence.append((row.source_event_id, row.source_revision, row.occurred_at,
                             _digest(_canonical(row.typed_value), row.availability),
                             row.observation_id, row.effective_local_date))
    for row in storage.get_current(subject_id=stored.subject_id, signals=[stored.signal]).get(stored.signal, ()):
        if row.source == stored.source and row.source_event_id and row.content_digest:
            evidence.append((row.source_event_id, row.source_revision, row.observed_at,
                             row.content_digest, row.source_observation_id, None))
    for event_id, revision, occurred_at, content, observation_id, day in evidence:
        fact = _digest(stored.subject_id, stored.source, stored.signal, event_id)
        rev = "" if revision is None else str(revision)
        # Recognize both released layouts, using the old row's timestamp.
        digests = (_digest(fact, occurred_at.isoformat(), rev, content),
                   _digest(fact, rev, content))
        if observation_id not in digests or observation_id not in missing:
            continue
        original = missing.pop(observation_id)
        storage.backfill_identity(replace(
            original, fact_key=fact, source_revision=revision,
            legacy_content_digest=content, effective_local_date=day,
        ))
    return list(storage.list_identities(**scope))


def decide_fact(storage, item):
    """Return (decision, prior revisions, reason) before any projection writes."""
    identities = identity_evidence(storage, item)
    prior = [row for row in identities if row.fact_key == item.fact_key]
    for row in prior:
        order = _compare_revisions(item.stored.source_revision, row.source_revision)
        if order == 0:
            if row.semantic_digest is not None:
                equal = row.semantic_digest == item.semantic_digest
            else:
                # Explicit legacy compatibility: released producers stamped upload
                # time on samples. A proven persisted fact may be replayed under a
                # different upload time, without re-projecting any metadata.
                equal = row.legacy_content_digest == item.content_digest
            return ("duplicate" if equal else "conflict"), prior, None
        if order is None:
            return "conflict", prior, "incomparable_fact_revision"
    if any(row.fact_key is None for row in identities):
        return "incomplete", prior, "legacy_identity_incomplete"
    if any(_compare_revisions(item.stored.source_revision, row.source_revision) == -1 for row in prior):
        return "stale", prior, "stale_fact_revision"
    return "accept", prior, None


def durable_identity(item, *, received_at, aggregate_scope):
    return DurableDedupeIdentity(
        subject_id=item.stored.subject_id, signal=item.stored.signal,
        source=item.stored.source, source_event_identity_digest=item.identity_digest,
        first_applied_at=received_at, aggregate_scope=aggregate_scope,
        fact_key=item.fact_key, source_revision=item.stored.source_revision,
        semantic_digest=item.semantic_digest,
        effective_local_date=item.stored.effective_local_date,
    )
