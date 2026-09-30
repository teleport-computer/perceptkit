# Aggregate generations, incomplete history, and definition durability

This document is the executable D11–D13 adapter contract. It describes Kit
behavior; it is not evidence that IO/Rokku PostgreSQL migrations are deployed.

## User-visible invariant

Ordinary daily, trend, and scheduled streak reads select exactly one explicit
active generation. A rebuild is an audit candidate until every requested date
is accounted for and the storage adapter atomically changes the active pointer.
An interrupted, partial, or incomplete candidate never changes ordinary reads.

`aggregation_version` identifies algorithm semantics. `generation_id`
identifies one rebuild attempt. Retrying the same algorithm creates a different
generation and retains the prior attempt for audit.

## State and storage contract

`AggregateGeneration` is scoped by subject, signal, and aggregation kind. It
persists requested date coverage, accounted/incomplete dates, status,
completeness, timestamps, and failure reason. `DailyAggregate` belongs to one
generation and persists completeness/reasons.

Adapters implement generation insert/update/read/list, an explicit active
pointer, atomic compare-and-activate, durable incomplete marking, and explicit
live-range accounting. PerceptionKit validates this v0.10 capability at
construction; direct query/processing entry points also fail closed. There is
no compatibility fallback that filters rows by algorithm version, because two
attempts may legitimately share that version. Activation must verify the
complete requested date set and complete candidate rows in the same
transaction. It must never select `max(aggregation_version)`.

Every aggregate row is bound to the generation's exact subject, signal,
aggregation kind, generation id, and algorithm version. Adapters reject a
version/scope mismatch on write and revalidate all candidate rows during
activation, so legacy or corrupt rows cannot publish a v2 document under a v3
active-generation record.

Activation is a whole-generation replacement, so candidate coverage must be a
superset of the currently active generation's requested coverage. A 90-day
active generation cannot be replaced by a one-day candidate: activation is
rejected with `candidate_coverage_does_not_cover_active_scope`, the old 90 days
remain visible, and ordinary queries never fall back to/mix the old version for
the other 89 days. Range-only recomputes remain complete audit candidates; to
publish a new algorithm the Host must rebuild the entire active scope (or first
run an explicit future retention/scope-change protocol; none exists today).

The reference adapter's bootstrap rule is deliberately narrow: the first
directly inserted complete legacy row can establish a one-day active
generation. A raw later insert can extend an adjacent day, but sparse endpoints
do not prove the intervening dates were inspected: those gaps become durable
`incomplete` dates. Kit's live ingest explicitly calls
`account_active_aggregate_range` before widening active coverage; Host
migrations must do their own verified backfill/resync rather than treating row
existence as evidence of contiguous complete history.

Incremental ingest and correction/retraction take the generation-scope mutation
resource before date rows. This serializes active-pointer reads with cutover.
After cutover, new observations update the selected active generation.

## Incomplete and retention

`allow_incomplete=True` is retained only as a compatibility spelling. It builds
an explicitly incomplete audit candidate; `RecomputeOutcome.ok` is false and
the candidate cannot activate. It never publishes a partial result.

When durable Fact identity identifies an affected date but retained detail can
no longer recompute it, correction/retraction persists an incomplete reason in
the active generation/row in the same mutation. Daily reads expose
`completeness` and `incomplete_reasons`. Trend and scheduled streak evaluation
exclude incomplete days; trend reports `days_incomplete` separately from
ordinary missing days.

Generation completeness is authoritative over rows. An incomplete date is
returned by daily queries even when it has no aggregate row (`has_data=false`),
and trend counts it in `days_incomplete`. If later live ingest writes a row for
that date, the row remains incomplete until a verified generation rebuild and
cutover; an ordinary fold cannot erase the durable warning.

Aggregate retention reconciles the active generation's readable coverage (or
clears the pointer when no active coverage remains). A post-retention candidate
only needs to cover retained history, so the safe narrow-candidate rule does
not permanently compare against dates policy has deleted.

Audit export includes a separate, paged `aggregate_generations` collection with
requested coverage, status, completeness, accounted/incomplete dates, reasons,
failure, and timestamps. Failed and zero-row attempts are exported too. Date
windows use requested-coverage overlap, and caps/truncation are reported per
signal. Ordinary query semantics must not be inferred from export ordering.

## DefinitionProvider durability

`PersistentDefinitionProviderPort` adds one narrow write capability:
`archive_definition(definition)`. Its immutable key is `(definition_id,
version)`. Equivalent retry is idempotent; changed semantics under the same key
raise `DefinitionArchiveConflictError` before RuleState/Event commit.

The provider declares `persistent=True`; `PerceptionKit.definition_provider_status()`
reports production readiness without guessing from read methods. Plain lists,
`StaticDefinitions`, and legacy read-only providers remain development/test
compatible but are explicitly not production-ready. Kit's local archive is not
restart durability.

`run_definition_provider_conformance(factory)` verifies fresh-provider restart,
upgrade, deletion, immutable conflict, active/history separation,
archive-before-Event ordering, and archive-failure transaction rollback. A
missing historical version returns `None`; replay/repair keeps its existing
explicit incomplete behavior and never substitutes the newest version.

## Host migration matrix

| Area | Required Host work |
| --- | --- |
| aggregate generation | New generation table keyed by scope + generation id; immutable algorithm/coverage identity; status/completeness/reasons/timestamps |
| aggregate rows | Add non-null generation id plus completeness/reasons; unique key includes generation id |
| active pointer | One row per subject + signal + kind; CAS old generation to fully verified candidate in the same transaction |
| live range accounting | Implement explicit active-range accounting; never infer complete gaps from sparse aggregate rows |
| mutation/fencing | Serialize generation pointer, date rows, correction/retraction, and activation; test with two real connections |
| legacy bootstrap | Classify/backfill verified active coverage; incomplete/unverifiable history stays explicitly incomplete or is resynced |
| coverage cutover | Candidate coverage must contain the full old active scope; a narrower range is audit-only and cannot collapse visible history |
| queries | Ordinary daily/trend/streak read only active generation; audit/export keeps all generations with stable paging |
| definitions | Durable immutable definition archive; archive before Event commit; restart/upgrade/deletion and failure rollback tests |
| retention | Preserve durable Fact date attribution; mark stale active coverage incomplete when detail is gone; never partial-recompute over it |
| aggregate retention | Reconcile active retained scope/pointer atomically with row deletion so later cutover compares against retained history |
| export | Page generation audit records independently, including failed and zero-row attempts, with requested-coverage overlap windowing |

These are Kit/reference and conformance results only. SQL migrations, deployed
transactions, installed-wheel Hosts, Runtime receipts, and device behavior are
separate acceptance evidence.
