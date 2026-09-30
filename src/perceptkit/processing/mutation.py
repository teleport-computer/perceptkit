"""Kit's canonical resource planning; adapters implement ownership, not policy."""
from dataclasses import replace

from ..contracts.errors import ContractError, UnsupportedRetractionIdentityError
from ..contracts.mutation import (aggregate_key, aggregate_generation_key,
                                  canonical_keys, current_key, fact_key,
                                  rule_key, event_key)
from ..rules.engine import scope_key


def retraction_fact_identity(retraction, sig):
    """Resolve only identities supported end-to-end by the deletion protocol.

    Retraction/tombstone/reselect/drop_retracted all use raw source event IDs.
    Deriving a singleton fallback lock alone cannot change that identity model.
    Deterministic fallback also lacks original Fact time; observed_at is audit.
    """
    from .normalize import _digest

    r = retraction
    strategy = sig.identity_strategy if sig is not None else "unknown"
    if strategy == "source_event_id" and r.source_event_id and r.source_event_id.strip():
        digest = _digest(r.subject_id, r.source, r.signal, r.source_event_id)
        return fact_key(r.subject_id, r.signal, r.source, r.source_event_id), digest
    raise UnsupportedRetractionIdentityError(r.signal, strategy)


def persisted_current_dimensions(storage, sig, *, subject, source, event_id,
                                 fact_digest, revisions=None):
    """Plan old partitions under Fact ownership, without writing/backfilling yet.

    Current rows and retained revisions are authority. Incoming corrected values
    cannot supply an old partition. An identified legacy revision with no provable
    dimension rejects the operation until evidence is restored/backfilled.
    """
    from ..contracts.context import IngestContext
    from ..contracts.observation import Observation
    from .normalize import _canonical, _digest, identity_for
    from .retract import _all_observations

    if revisions is None:
        revisions = storage.list_identities(subject_id=subject, signal=sig.key,
                                            source=source, fact_key=fact_digest)
    prior = [r for r in revisions if r.fact_key == fact_digest]
    dimensions = {r.dimension_key for r in prior if r.dimension_key is not None}
    updates = []
    if not sig.dimension_fields and sig.current_policy == "latest":
        # A dimension-free manifest makes the partition structural, independent
        # of the historical value. This is proof, not a guess from the correction.
        dimension = sig.dimension_key_for(None)
        dimensions.add(dimension)
        return dimensions, [replace(r, dimension_key=dimension) for r in prior
                            if r.dimension_key is None]

    def matches_fact(row):
        if row.source != source:
            return False
        if sig.identity_strategy == "source_event_id" and event_id:
            return row.source_event_id == event_id
        # None is not an identity shared by every fallback Fact. Reconstruct
        # the canonical Fact identity from THIS persisted row's own timestamp
        # and manifest strategy, never the incoming correction's values/time.
        when = row.observed_at if hasattr(row, "dimension_key") else row.occurred_at
        evidence = Observation(sig.key, sig.schema_version, when, row.availability,
                               source_event_id=row.source_event_id,
                               source_revision=row.source_revision)
        _, persisted_fact, _, _ = identity_for(
            evidence, sig, IngestContext(subject, when), source=source, content_digest="")
        return persisted_fact == fact_digest

    currents = [row for row in storage.get_current(subject_id=subject, signals=[sig.key]).get(sig.key, ())
                if matches_fact(row)]
    dimensions.update(row.dimension_key for row in currents)
    missing = [r for r in prior if r.dimension_key is None]
    # Current metadata alone remains authoritative when details have expired.
    details = []
    if sig.stores_history and (not prior or missing or any(r.fact_key is None for r in revisions)):
        details = [row for row in _all_observations(storage, subject, sig.key)
                   if matches_fact(row)]
        dimensions.update(sig.dimension_key_for(row.typed_value) for row in details)

    evidence = {}
    for row in [*details, *currents]:
        if hasattr(row, "dimension_key"):
            dimension, when, content = row.dimension_key, row.observed_at, row.content_digest
        else:
            dimension, when = sig.dimension_key_for(row.typed_value), row.occurred_at
            content = _digest(_canonical(row.typed_value), row.availability)
        if not content:
            continue
        revision = "" if row.source_revision is None else str(row.source_revision)
        for digest in (_digest(fact_digest, revision, content),
                       _digest(fact_digest, when.isoformat(), revision, content)):
            evidence.setdefault(digest, set()).add(dimension)
    for record in missing:
        known = evidence.get(record.source_event_identity_digest, set())
        if len(known) == 1:
            dimension = next(iter(known))
            dimensions.add(dimension)
            updates.append(replace(record, dimension_key=dimension))
        elif sig.current_policy == "latest":
            raise ContractError([f"{sig.key}: current_dimension_evidence_incomplete; "
                                 "restore persisted revision evidence before mutation"])
    return dimensions, updates


def rule_keys(items, definitions):
    return canonical_keys([
        rule_key(item.stored.subject_id, d.definition_id,
                 f"{scope_key(d, local_date=item.stored.effective_local_date)}@v{d.version}")
        for item in items for d in definitions
        if d.enabled and d.signal == item.stored.signal
        and d.subject_id in (None, item.stored.subject_id)
    ])


def acquire_ingest(owner, storage, items, signals, definitions, version, definition_at=None):
    from .retract import _all_observations

    owner.acquire(canonical_keys([
        fact_key(o.stored.subject_id, o.stored.signal, o.stored.source,
                 (o.stored.source_event_id
                  if signals[o.stored.signal].identity_strategy == "source_event_id" else None),
                 fallback=o.fact_key) for o in items
    ]))
    # Old dates are discovered ONLY after every Fact in the batch is owned.
    # No writes or aggregate/rule/current decisions happen in this phase.
    days = {(o.stored.subject_id, o.stored.signal, o.stored.effective_local_date)
            for o in items if signals[o.stored.signal].stores_history}
    replay_days = {(o.stored.subject_id, o.stored.signal, o.stored.effective_local_date) for o in items}
    currents = set()
    backfills = {}
    for o in items:
        row = o.stored
        sig = signals[row.signal]
        revisions = None
        if sig.identity_strategy == "source_event_id" and row.source_event_id:
            revisions = storage.list_identities(subject_id=row.subject_id, signal=row.signal,
                                                 source=row.source, fact_key=o.fact_key)
            prior_days = {(row.subject_id, row.signal, old.effective_local_date)
                          for old in revisions if old.fact_key == o.fact_key
                          and old.effective_local_date is not None}
            replay_days.update(prior_days)
            if sig.stores_history:
                days.update(prior_days)
            # Legacy evidence may not have a date yet. Recover from retained
            # rows, never infer an old date from the incoming correction.
            if sig.stores_history and any(old.fact_key is None or old.effective_local_date is None for old in revisions):
                days.update((row.subject_id, row.signal, old.effective_local_date)
                            for old in _all_observations(storage, row.subject_id, row.signal)
                            if (old.source, old.source_event_id) == (row.source, row.source_event_id))
        if sig.current_policy == "latest":
            dimensions, updates = persisted_current_dimensions(
                storage, sig, subject=row.subject_id, source=row.source,
                event_id=row.source_event_id, fact_digest=o.fact_key, revisions=revisions)
            dimensions.add(sig.dimension_key_for(row.typed_value))
            currents.update(current_key(row.subject_id, row.signal, dimension) for dimension in dimensions)
            for update in updates:
                backfills[(update.subject_id, update.signal, update.source,
                           update.source_event_identity_digest)] = update
    owner.acquire(canonical_keys(list(currents)))
    scopes = {(subject, signal) for subject, signal, _ in days}
    owner.acquire(canonical_keys([aggregate_generation_key(subject, signal, "daily")
                                  for subject, signal in scopes]))
    active_versions = {}
    for subject, signal in scopes:
        active = storage.get_active_aggregate_generation(
            subject_id=subject, signal=signal, aggregation_kind="daily")
        active_versions[(subject, signal)] = active.aggregation_version if active else version
    owner.acquire(canonical_keys([aggregate_key(
        subject, signal, day, "daily", active_versions[(subject, signal)])
        for subject, signal, day in days]))
    replay_days.update(days)  # Includes legacy dates recovered from persisted detail.
    from .rule_repair import repair_scopes, repair_keys
    scopes = []
    for subject, signal in {(o.stored.subject_id, o.stored.signal) for o in items}:
        scopes.extend(repair_keys(subject, repair_scopes(
            storage, subject=subject, signal=signal, definitions=definitions,
            definition_at=definition_at,
            days={day for sub, sig, day in replay_days if (sub, sig) == (subject, signal)})))
    owner.acquire(canonical_keys([*rule_keys(items, definitions), *scopes]))
    owner.acquire(canonical_keys([event_key(o.stored.subject_id, o.stored.signal) for o in items]))
    # Scheduled evaluation can create a previously absent RuleState between
    # planning and the Event lock. Revalidate the key set after serialization;
    # retry the transaction rather than acquiring a lower-ranked key now.
    from ..contracts.errors import RetryableMutationError
    planned = set(scopes) | set(rule_keys(items, definitions))
    for subject, signal in {(o.stored.subject_id, o.stored.signal) for o in items}:
        refreshed = repair_keys(subject, repair_scopes(
            storage, subject=subject, signal=signal, definitions=definitions,
            definition_at=definition_at,
            days={day for sub, sig, day in replay_days if (sub, sig) == (subject, signal)}))
        if not set(refreshed) <= planned:
            raise RetryableMutationError("RuleState scope set changed during mutation planning")
    for update in backfills.values():
        storage.backfill_identity(update)
