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

    Current may retain evidence after details expire. An exact match against
    an actually persisted released digest is also proof; a nonmatch cannot tell
    an unrelated new Fact from an unprovable changed-timestamp legacy replay.
    """
    stored = item.stored
    scope = dict(subject_id=stored.subject_id, signal=stored.signal, source=stored.source,
                 fact_key=item.fact_key)
    identities = list(storage.list_identities(**scope))
    missing = {r.source_event_identity_digest: r for r in identities if r.fact_key is None}
    if not missing:
        return identities
    for digest in (item.identity_digest, item.legacy_identity_digest):
        if digest in missing:
            # This is equality with an existing cryptographic commitment, not
            # creation of an imaginary old hash from a new timestamp. Changed
            # timestamps do not match and cannot be inferred from an orphan hash.
            original = missing.pop(digest)
            storage.backfill_identity(replace(
                original, fact_key=item.fact_key, source_revision=stored.source_revision,
                legacy_content_digest=item.content_digest,
                effective_local_date=stored.effective_local_date,
            ))
    evidence = []
    for row in _all_observations(storage, stored.subject_id, stored.signal):
        if row.source == stored.source and row.source_event_id:
            evidence.append((row.source_event_id, row.source_revision, row.occurred_at,
                             _digest(_canonical(row.typed_value), row.availability),
                             row.effective_local_date))
    for row in storage.get_current(subject_id=stored.subject_id, signals=[stored.signal]).get(stored.signal, ()):
        if row.source == stored.source and row.source_event_id and row.content_digest:
            evidence.append((row.source_event_id, row.source_revision, row.observed_at,
                             row.content_digest, None))
    for event_id, revision, occurred_at, content, day in evidence:
        fact = _digest(stored.subject_id, stored.source, stored.signal, event_id)
        rev = "" if revision is None else str(revision)
        # Recognize both released layouts, using the old row's timestamp.
        digests = (_digest(fact, occurred_at.isoformat(), rev, content),
                   _digest(fact, rev, content))
        for digest in digests:
            if digest not in missing:
                continue
            original = missing.pop(digest)
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
    if any(_compare_revisions(item.stored.source_revision, row.source_revision) == -1 for row in prior):
        return "stale", prior, "stale_fact_revision"
    warning = ("legacy_identity_unmapped: changed_timestamp_replay_unverifiable"
               if any(row.fact_key is None for row in identities) else None)
    return "accept", prior, warning


def durable_identity(item, *, received_at, aggregate_scope):
    return DurableDedupeIdentity(
        subject_id=item.stored.subject_id, signal=item.stored.signal,
        source=item.stored.source, source_event_identity_digest=item.identity_digest,
        first_applied_at=received_at, aggregate_scope=aggregate_scope,
        fact_key=item.fact_key, source_revision=item.stored.source_revision,
        semantic_digest=item.semantic_digest,
        effective_local_date=item.stored.effective_local_date,
    )


def detail_proves_revision(detail, identity, item):
    """Match persisted Fact semantics, independent of host observation ID format."""
    if (detail.source != item.stored.source or detail.source_event_id != item.stored.source_event_id
            or _compare_revisions(detail.source_revision, identity.source_revision) != 0
            or detail.effective_local_date != identity.effective_local_date):
        return False
    content = _digest(_canonical(detail.typed_value), detail.availability)
    rev = "" if detail.source_revision is None else str(detail.source_revision)
    return identity.source_event_identity_digest in (
        _digest(item.fact_key, rev, content),
        _digest(item.fact_key, detail.occurred_at.isoformat(), rev, content),
    )
