# Fact repair and external-delivery boundary

Deleting 72 from 70 -> 72 restores the remaining 70 baseline, removes the old
trigger's once-slot, and allows a later real 70 -> 73 crossing. Correcting 72 to
69 likewise rebuilds the state from 70, 69. Repair itself never sends or creates
replacement notifications. Retrying the same operation cannot create duplicate
event IDs or rewrite the independent delivery receipts.

## Provenance and repair

Outbox `fact_dependencies` is a tuple of value-free references containing
`subject_id`, `signal`, `source`, `source_event_id`, canonical `fact_key`,
`source_revision`, `observation_id`, and role (`current`, `previous`, `reference`).
Previous/reference dependencies are explicit; human reason text is never parsed.
Occurrence does not inherit an unrelated previous occurrence.

`fact_dependencies_complete=False` marks legacy or scheduled lineage which
cannot prove every historical dependency. Any mutation of the same subject and
signal conservatively invalidates these events. This can discard an otherwise
valid pending scheduled event; it is the declared cost of incomplete lineage,
not proof of complete historical replay. Hosts may backfill proven lineage only.

Source-ID retraction matches all dependencies by subject/signal/source/source ID.
Correction additionally matches canonical Fact identity and invalidates existing
revision references before creating any new-revision event. The current
Retraction contract still rejects singleton/deterministic identities atomically.

RuleState repair loads effective valid revisions, sorts by business `occurred_at`,
canonical Fact identity, revision and observation ID, and evaluates the original
definition version in its scope. Recorded surviving triggers occupy their scope,
even when a late Fact changes the replay path so that the original crossing no
longer occurs. Cooldown uses the latest surviving Event's actual `detected_at`,
never the Fact's business timestamp. Hypothetical replay crossings without a
surviving event do not consume a slot.
State stores `previous_fact`, `signal`, `completeness` and `incomplete_reason` in
addition to evaluator state. Replay never calls WakePort or enqueues a correction
notification. A corrected Fact moved to a new day establishes that day's baseline.

Missing detail proven by durable identities, unavailable archived definitions,
current-only storage, or missing scheduled tick/aggregate history produces
`completeness=incomplete`. Available facts establish only a safe last baseline;
no crossing is fabricated. Independent surviving recorded triggers still prove
fired/last-fired state. Existing incomplete coverage is not silently promoted to
complete. Production archived-definition persistence belongs to Task 6/D12.
Full scheduled replay requires retained tick/aggregate-generation evidence;
Task 5 conservatively invalidates and marks incomplete instead. Aggregate
retention/date completeness itself remains Task 6C's separate obligation.

Legacy states without signal metadata must be attributed using their original
definition version or exact-version/scope Outbox evidence. If attribution is
missing or contradictory, mutation fails before writes with
`rule_state_attribution_incomplete` and recovery action
`restore_rule_state_attribution`. Hosts must restore trustworthy metadata/archive
and then retry; Kit never guesses from the newest definition. This can block a
subject mutation when Kit cannot prove that an old state belongs to another
signal. Persisted archive recovery remains the host/Task 6 responsibility.

## Delivery states

```text
pending -> claimed -> durable start -> WakePort -> genuine receipt
   |          |             |                          |
   +---- invalidation ------+                          |
       before start: invalidated                       |
       after start: unknown <---- timeout/crash -------+
                                  accepted/duplicate -> delivered
```

`invalidated` is terminal and distinct from Runtime suppression. `unknown` is
not claimable, is excluded from worker backlog, and is visible via `list_events`.
The claim token and dispatch-start marker remain available for reconciliation.
Started lease expiry never creates another attempt. WakePort exceptions and
mismatched event/attempt receipts enter unknown without a fabricated failure
receipt. Runtime idempotency remains required.

Only a real `enqueue_failed` receipt permits normal backoff/dead-letter handling.
If invalidation already occurred, that failed receipt yields terminal invalidated.
Real accepted/duplicate receipts resolve an unknown invalidated attempt to
delivered while keeping invalidated_at/reason and the scrubbed snapshot. Missing
or stale claim tokens cannot overwrite unknown or invalidated.

Delivered records retain identity, definition version, occurrence/detection times
and separate immutable WakeReceipt audit. Snapshot scrub uses a metadata allowlist:
previous/current become null; condition, derived reason and arbitrary nested
extensions are removed. No numeric string replacement, recall or automatic
correction message occurs.

## Required adapter migration

Persist the Outbox provenance, completeness, start and invalidation columns;
retain RuleState signal/version/scope metadata and implement list_rule_states.
Extend list_identities to allow signal-scoped completeness queries. Implement
required scrub_event_snapshots, begin_event_dispatch, mark_dispatch_unknown and
the committed-state return from record_wake_receipt. Backfill or mark legacy
lineage incomplete, and reconcile pre-upgrade in-flight events before resuming
workers. No optional best-effort scrub path remains.

Every writer must use Fact -> Current -> Aggregate -> RuleState -> Event resource
order before mutation. Claim, start, invalidation and receipt serialize on
`(50_events, subject, signal)`; post-lock reads must be fresh. Conformance checks
prove sequential protocol behavior only. IO/Rokku must prove two-connection
claim/invalidate races, crash recovery and Runtime receipt reconciliation before
release. This Kit change alone is not an end-to-end delivery claim.

Preflight includes every old Event scope that same-signal conservative
invalidation can affect, even when it is outside the mutated Fact's day. Those
RuleState keys are acquired before Event ownership and rebuilt in the same
transaction. If fresh post-lock evidence expands the scope plan, retry the whole
transaction; never acquire a lower-ranked RuleState key after an Event key.
