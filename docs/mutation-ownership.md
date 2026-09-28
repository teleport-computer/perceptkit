# Fact mutation ownership contract

This contract supplements `StoragePort.transaction`. It applies to ingest,
correction, retraction, aggregate recompute and RuleState advancement. The Kit
chooses resource identities and order; the adapter provides database ownership,
isolation, rollback and fencing. `InMemoryStorage` is a deterministic reference,
not proof of concurrent PostgreSQL behavior.

## API and ordering

`with storage.mutation_transaction() as owner:` begins one atomic operation.
`owner.acquire(keys)` acquires a sorted, deduplicated tuple of resource keys.
The owner retains every resource until the database COMMIT or ROLLBACK finishes.
An adapter cannot release locks when `acquire` returns or before commit.

| Rank | Resource tuple after rank | Used by |
| --- | --- | --- |
| `10_fact` | subject, signal, source, `source_id`/`fallback`, identity | ingest, correction, retraction |
| `20_current` | subject, signal, dimension_key | Current advancement and reselect |
| `30_aggregate` | subject, signal, ISO local date, kind, algorithm version | incremental fold, correction, retract, recompute |
| `40_rule` | subject, definition ID, scope including definition version | ingest and scheduled rules |

Tuples and rank strings are public canonical keys; use unambiguous serialization.
Never concatenate fields with an unescaped delimiter. Kit orders the canonical
tuples; an adapter must not reorder their derived database lock IDs. Resource
keys contain subject identity, including when two subjects share all other IDs.

Ingest acquires **all Facts in the report** first. Under Fact ownership it reads
durable revision metadata to find old dates, using retained evidence for legacy
unknown dates. It then acquires all Current, all Aggregate and all RuleState
keys in that order. Only then may it decide retraction/revision status and write
Observations, identities or projections. Report claims are atomic and roll back
with this same operation. Batch input order never controls lock order.

Fact resource selection follows the manifest identity strategy, not merely the
presence of an optional wire `source_event_id`. Only `source_event_id` strategy
with an actual ID uses a `source_id` resource. `deterministic_digest`, `singleton`
and source-ID signals falling back because an ID is absent use the normalized
canonical `fact_key` in the `fallback` resource. Optional IDs on fallback signals
cannot split ownership or change persisted-evidence matching.

Retraction acquires all Facts before discovering affected dates, then all Current
dimensions and Aggregates before writing tombstones/reselect/recompute. Recompute
starts at Aggregate ownership before reading Facts; it never requests Fact locks
afterwards. A writer cannot append a Fact and then wait for its Aggregate lock.
RuleState advancement owns the scope from before reading state until Outbox and
state commit together. Scheduled evaluation pre-acquires the entire batch of
scope keys so reversed definition order cannot deadlock.

Current ownership uses exactly subject + signal + dimension_key. Under Fact
ownership, Kit discovers every old partition from retained Observations, matching
Current rows and durable identity metadata, and combines them with incoming
partitions. It acquires this full sorted set before writes. A correction moving
an anchor from A to B therefore owns both; independent A/B Current resources do
not contend (they may still share an Aggregate or RuleState resource).

`DurableDedupeIdentity.dimension_key` preserves each accepted revision's partition
after detail expiry. Legacy None means unknown. Backfill may only fill an unknown
partition using persisted evidence, without changing identity/content/known date.
Incoming correction fields are never treated as the old partition. A manifest
with no dimension fields proves its structural signal-only dimension directly.
For a partitioned signal, an identified prior revision whose partition cannot be
proved fails with `ContractError: current_dimension_evidence_incomplete`; restore
or backfill evidence before retry. The operation leaves no partial writes.

Durable partition evidence is queried by canonical Fact digest for **all** identity
strategies, including observations with no source event ID. The default
`proximity_anchor` deterministic fallback identifies its Fact by subject, source,
signal and observation timestamp; revision/content distinguish deliveries. A
higher revision at that same timestamp can therefore retain A's Fact key while
reporting partition B, and must pre-own A+B. Missing event ID does not skip this
lookup. Fallback legacy evidence is matched by reconstructing each persisted
row's canonical Fact identity from its own timestamp/strategy; sharing a null
event ID is not proof. Unrelated unmapped legacy identities cannot supply a
partition or globally block a new Fact.

This dimension repair does not repair `_affected_days` after detail retention:
that existing helper still discovers aggregate days from retained observations.
Task 6C must surface incomplete date evidence and ensure old aggregates cannot
be silently omitted by a final retraction result.

## Retraction identity capability

The current `Retraction` envelope names a source reference and the time deletion
was observed. That timestamp is not the original Fact's observation timestamp.
The complete batch is therefore preflighted before any transaction or write:

| Manifest strategy | Canonical mutation resource for retraction |
| --- | --- |
| `source_event_id` with valid ID | Same subject/signal/source/source ID resource as ingest |
| `singleton` | Typed unsupported: raw-ID tombstone/reselection cannot express canonical singleton deletion |
| `deterministic_digest` | Typed unsupported: original Fact time/canonical reference is absent |
| Unknown signal/strategy | Typed unsupported: identity contract is unavailable |

Unsupported deletion raises public `UnsupportedRetractionIdentityError`, a
`ContractError` with code `retraction_identity_unsupported`, `retryable=False`
and `recovery_action=upgrade_retraction_identity_contract`. The entire batch,
including any supported siblings, leaves no mutation. Hosts must expose this
failure and retain the deletion for recovery, not acknowledge deletion or advance
its ingestion cursor as though it succeeded. Repeating the unchanged request
cannot create the missing identity.

Product cost: default `screen_change` (singleton), `proximity_anchor` and other fallback signals
cannot currently use this source-reference-only deletion API. They previously
could take a different lock from ingest and report unsafe success; this version
explicitly refuses that operation. Their ingest/current/query behavior remains
available. Only source-ID signals such as health measurements retain deletion
support with canonical ownership. Even a singleton deletion whose optional ID
matches the current row is unsupported: accidental matching cannot establish
the end-to-end identity contract.

Restoring singleton or deterministic deletion requires canonical Fact identity
end-to-end in Retraction, durable tombstones, Current reselection and
`drop_retracted`; changing only the lock key is insufficient. This belongs to
Task 5 or a future breaking contract. If optional source references remain the
wire API, a durable reference → canonical-Fact mapping also needs a lower-rank
routing key shared by ingest and retraction. The protocol must cover mapping
after detail expiry, unmapped legacy evidence, deletion preceding upload,
multiple aliases for one Fact and one alias spanning several Facts. This task
does not add that mapping or synthesize a key from `observed_at`.

## Reentrancy, failures and database obligations

Only the **same explicit owner capability** may reacquire its held keys. A nested
public operation is a new owner, even on the same Python thread; overlapping keys
must contend. Internal helpers reuse the outer operation and preplanned keys.
An owner cannot acquire a new lower-order key or survive transaction exit.

Contention, invalid ordering, serialization failure and lost fences raise the
public `RetryableMutationError` and make the operation rollback-only. Catching
that error inside a transaction cannot permit its writes to commit. A caller
retries the whole unchanged report/retraction/recompute operation. Current and
Aggregate CAS remain backstops; exhausted CAS raises `RetryableProjectionError`
and rolls back. Aggregate CAS always re-reads and re-folds after a failed compare.

A PostgreSQL adapter can use transaction-scoped advisory locks or lock rows.
Use a dedicated transaction per independent owner and fresh post-lock snapshots
(for example READ COMMITTED). REPEATABLE READ needs serialization validation and
whole-operation retry after stale snapshots. Lock mapping must be stable across
processes; Python's randomized `hash` is not suitable. Any hash collision can
only add contention, never authorize shared ownership. Transaction locks are
preferred to expiring leases; a lease-based adapter must fence every protected
write and commit and must not let a stale owner release its successor's lock.
Independent nested operations cannot borrow outer database locks merely because
the database treats advisory locks on one connection as reentrant.

Low-level storage methods remain usable for migrations and conformance fixtures.
Production business mutations must run through the Kit ownership protocol;
wrapping them only in an ordinary transaction is insufficient. Direct rule-helper
callers composing a larger mutation must pass its explicit `mutation` owner.

## Required host evidence

Sequential conformance checks include Aggregate missing-version `-1`, initial
write `0`, successful increments, failed-CAS no-op, ordinary-put invalidation of
old CAS versions, and independence from algorithm `aggregation_version`. They
also check rollback-only ownership failures and expired capabilities.

IO and Rokku must additionally use **two real database connections** and barriers:

1. Same Fact: ingest vs retract and correction vs retract, both winning orders;
   committed tombstones must not leave active Current/Aggregate contributions.
2. Different Facts sharing a day: both contributions survive, then full recompute
   agrees; recompute vs ingest/correction/retract must have a legal serial result.
3. Same RuleState scope: one scope transition/outbox insertion survives, with no
   overwritten state or duplicate occurrence. Reversed multi-Fact batches and
   reversed definition batches cannot deadlock indefinitely.
4. A failure after Fact/Current/Aggregate/RuleState writes, or at commit fencing,
   exposes no partial report, identity, projection, state or event on the second
   connection. A full retry succeeds once.
5. Owners for different subjects and disjoint keys remain independent; a stale
   owner cannot write, commit, reacquire, or release a successor's ownership.

Event invalidation and rebuilding RuleState after deletion/correction are a
separate protocol step (Task 5). This contract provides serialization, not those
semantic repairs. Dispatch delivery leases retain their own event fencing.
