"""Kit's canonical resource planning; adapters implement ownership, not policy."""
from ..contracts.mutation import aggregate_key, canonical_keys, current_key, fact_key, rule_key
from ..rules.engine import scope_key


def rule_keys(items, definitions):
    return canonical_keys([
        rule_key(item.stored.subject_id, d.definition_id,
                 f"{scope_key(d, local_date=item.stored.effective_local_date)}@v{d.version}")
        for item in items for d in definitions
        if d.enabled and d.signal == item.stored.signal
        and d.subject_id in (None, item.stored.subject_id)
    ])


def acquire_ingest(owner, storage, items, signals, definitions, version):
    from .retract import _all_observations

    owner.acquire(canonical_keys([
        fact_key(o.stored.subject_id, o.stored.signal, o.stored.source,
                 o.stored.source_event_id, fallback=o.fact_key) for o in items
    ]))
    # Old dates are discovered ONLY after every Fact in the batch is owned.
    # No writes or aggregate/rule/current decisions happen in this phase.
    days = {(o.stored.subject_id, o.stored.signal, o.stored.effective_local_date)
            for o in items if signals[o.stored.signal].stores_history}
    for o in items:
        row = o.stored
        sig = signals[row.signal]
        if sig.stores_history and sig.identity_strategy == "source_event_id" and row.source_event_id:
            revisions = storage.list_identities(subject_id=row.subject_id, signal=row.signal,
                                                 source=row.source, fact_key=o.fact_key)
            days.update((row.subject_id, row.signal, old.effective_local_date)
                        for old in revisions if old.fact_key == o.fact_key
                        and old.effective_local_date is not None)
            # Legacy evidence may not have a date yet. Recover from retained
            # rows, never infer an old date from the incoming correction.
            if any(old.fact_key is None or old.effective_local_date is None for old in revisions):
                days.update((row.subject_id, row.signal, old.effective_local_date)
                            for old in _all_observations(storage, row.subject_id, row.signal)
                            if (old.source, old.source_event_id) == (row.source, row.source_event_id))
    owner.acquire(canonical_keys([
        current_key(o.stored.subject_id, o.stored.signal) for o in items
        if signals[o.stored.signal].current_policy == "latest"
    ]))
    owner.acquire(canonical_keys([aggregate_key(subject, signal, day, "daily", version)
                                  for subject, signal, day in days]))
    owner.acquire(rule_keys(items, definitions))
