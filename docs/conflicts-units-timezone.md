# Durable conflicts, canonical units and timezone attribution

Implements D07–D09 of the consistency contract. Example: an accepted 70 kg
measurement followed by an implausible 150 kg candidate leaves Current at 70 kg.
The candidate is queryable after restarting Kit, and a valid higher source
revision can resolve it without losing the original conflict audit.

## Pipeline and ownership

1. Parse immutable Report semantics, preserving declared units and timezone.
2. Validate the per-field units map, convert to manifest canonical units, then
   validate canonical types/ranges. Validate explicit IANA timezone independently
   for each observation and calculate local date using that validated zone.
3. Acquire the existing Fact/Current/Aggregate/RuleState/Event mutation resources.
4. Apply Task 3 Fact revision decisions. Previously accepted retries stay
   duplicates even when a different candidate for that Fact is pending.
5. Block unresolved candidates unless their revision is strictly higher than
   all pending revisions. Check `max_relative_jump` using canonical values:
   corrections compare against their own applied Fact; new readings compare
   against comparable Current, or a chronological predecessor for late data.
6. Insert a ConflictRecord, or apply normally and resolve eligible records.
   Finalize Report in the same transaction. Failure rolls back both records and
   every Fact/projection/rule/event mutation.

Unresolved candidates do not write Observation, applied durable identity,
Current, Aggregate, RuleState or EventOutbox. Their `candidate` is a value in the
ConflictRecord, not an applied Observation table row.

Task 2's Current tie between **different** independently accepted Facts remains
a projection-choice result; it does not turn either Fact into a quarantined
candidate or prevent occurrence/aggregate participation. Durable identity
conflicts here consume Task 3's stable source-ID Fact revision decision. Existing
fallback identity/revision capability boundaries are unchanged.

## Conflict storage contract

`ConflictRecord` stores `conflict_id`, subject/signal/source, canonical `fact_key`,
candidate revision, semantic/content digests, kind/reason, sanitized canonical
candidate with source evidence, `pending|resolved`, created/updated timestamps,
and resolution revision/digest/observation ID/time. The stable ID hashes canonical
Fact + candidate semantic digest + original conflict kind. An exact pending
candidate retry returns its existing record even if surrounding state changes.

Required StoragePort additions:

- `put_conflict(record)`: insert-if-absent by `(subject_id, conflict_id)`; never
  overwrite candidate evidence, timestamps or resolution on retry.
- `list_conflicts(subject_id, signal?, source?, fact_key?, status?)`: tenant
  isolation, stable `(created_at, conflict_id)` ordering, no hidden truncation.
- `resolve_conflict(...)`: pending to resolved only for strictly higher
  comparable revision. Identical resolution retry succeeds; a different
  resolution cannot overwrite the audit. Caller holds the canonical Fact owner.

`kit.list_conflicts(subject_id=..., signal=..., status=...)` is the query entry.
`IngestOutcome.conflicts` remains an immediate hint; persistent records are the
authority. `export_subject` includes JSON-compatible conflict records; subject
purge removes them. Detail retention does not erase conflicts or applied identity.
Conflict export is complete and currently unbounded; Task 6B owns common export
window/cap semantics and must include this new collection in that contract.

## Units and audit

Wire example: `value: {weight_kg: 154}, units: {weight_kg: "lb"}` becomes
`typed_value: {weight_kg: 69.85322498}`. Omitted map/field means canonical units;
explicit canonical unit is accepted even when absent from `accepted_units`.
Unsupported units, malformed maps, undeclared/non-numeric fields, absent values
and incompatible conversions produce an `invalid_units` observation problem.

`StoredObservation` and `CurrentProjection` persist `source_units` and
`source_values` separately from canonical `typed_value`. Restricted/undeclared
values cannot enter this audit metadata. Current-only signals preserve this
evidence too; Current reselection copies the winning Fact's metadata. Aggregate,
rule and event numerical values are canonical. No raw unit values enter public
Current values or rule evaluation.

Wire units remain part of Report and Fact semantic digests. Changing units or
timezone while retaining the same Fact revision is a semantic conflict, including
canonical-equivalent numbers expressed with a different explicit wire unit.
Use a higher revision for such a correction and a new Report ID. Canonical
omission and an empty units map have the same semantics. Explicit null units are
invalid and distinct from omission; valid existing v2 digests are unchanged.

## Timezone

Explicit timezone must be a valid `zoneinfo.ZoneInfo` key; UTC and DST regions
are supported. Empty, whitespace, numeric offsets, nonexistent names, wrong
types and explicit null reject only that observation. Batch parsing defers this
validation so other observations still apply. Standalone `Observation.parse`
retains its structural validation. Parser presence metadata distinguishes null
from omission; direct object construction with the default None means omission.

Only omission uses configured `timezone_fallback`. Invalid fallback is a
configuration exception and rolls back the Report. With neither zone nor
fallback, existing offset-based attribution continues with a warning and
`timezone_source="missing"`. Persisted source values are `observation`,
`host_fallback`, `missing`; old rows default to `legacy_unknown`, never fabricated
as producer-supplied. Observation stores the resolved zone; Current stores zone
and source for current-only evidence too.

## Host migration and evidence

Hosts must migrate ConflictRecord storage, the three new methods, transaction
rollback/purge/export, and Observation/Current audit fields before enabling this
Kit version. Use conformance guarantee 17 for deterministic insert, retry,
resolution, filtering, rollback, metadata, retention and purge behavior. Add
real-database restart and two-connection conflict/resolution contention tests;
InMemory conformance alone does not establish production durability/isolation.

No manual resolution UI, worker, new rule-history provider, aggregation generation
activation or public Current array cutover is introduced here.
