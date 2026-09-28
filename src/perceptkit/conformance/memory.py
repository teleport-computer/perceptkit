"""内存版存储 —— **只是测试工具，不是生产实现。**

它存在的意义是让宿主在写自己的 adapter 之前，先有一个能跑通的参照，
以及让 kit 自己的管线测试不依赖任何数据库。

🔴 **它验不出数据库事务边界和隔离级别。** 这里以快照实现异常回滚，
提供同步 reference CAS，不提供多线程或独立连接隔离保证。
宿主必须另外用真实数据库、两条独立连接、
在关键写操作之间打断点，才能证明那条保证成立。

在这里绿 = 端口语义、调用顺序、确定性没问题。
仅此而已。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

#: 排序时给「没有时间」的条目垫底用的哨兵，不参与任何业务判断。
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
from typing import Any, Iterator, Sequence

from ..contracts import delivery as _delivery
from ..contracts.records import (
    CalendarEventMirror,
    AggregateGeneration,
    ConflictRecord,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
    _compare_revisions,
)
from ..contracts.receipt import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    IngestReceipt,
    WakeReceipt,
)
from ..contracts.mutation import canonical_keys, RetryableMutationError


class _MemoryMutationOwner:
    def __init__(self, storage):
        self.storage = storage
        self.keys = set()
        self.active = True
        self.failed = False

    def validate(self):
        if (not self.active or self.failed or any(
                self.storage._mutation_locks.get(key) is not self for key in self.keys)):
            self.failed = True
            raise RetryableMutationError("mutation owner expired, aborted or lost its fence")

    def acquire(self, keys):
        def fail(reason):
            self.failed = True
            raise RetryableMutationError(reason)

        self.validate()
        if tuple(keys) != canonical_keys(keys):
            fail("mutation keys must be sorted and unique")
        new = set(keys) - self.keys
        if new and self.keys and min(new) < max(self.keys):
            fail("mutation lock order violation")
        if any(key in self.storage._mutation_locks for key in new):
            fail("mutation resource is owned by another operation")
        for key in new:
            self.storage._mutation_locks[key] = self
        self.keys.update(new)


class InMemoryStorage:
    """把 :class:`~perceptkit.ports.storage.StoragePort` 实现在几个字典上。"""

    def __init__(self) -> None:
        self.reports: dict[tuple[str, str, str], IngestReceipt] = {}
        self.observations: dict[str, StoredObservation] = {}
        self.identities: set[tuple[str, str, str, str]] = set()
        self.identity_records: dict[tuple[str, str, str, str], DurableDedupeIdentity] = {}
        self.conflicts: dict[tuple[str, str], ConflictRecord] = {}
        self.current: dict[tuple[str, str, str], CurrentProjection] = {}
        self.aggregates: dict[tuple[str, str, date, str, int, str], DailyAggregate] = {}
        self.aggregate_generations: dict[tuple[str, str, str, str], AggregateGeneration] = {}
        self.active_aggregate_generations: dict[tuple[str, str, str], str] = {}
        self.calendar: dict[tuple, CalendarEventMirror] = {}
        self.reminders: dict[tuple, ReminderItemMirror] = {}
        self.sync_state: dict[tuple[str, str, str], SourceSyncState] = {}
        self.rule_state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.outbox: dict[str, EventOutboxEntry] = {}
        self.receipts: list[WakeReceipt] = []
        #: (subject, signal, source, source_event_id) -> Retraction
        self.retractions: dict[tuple[str, str, str, str], Any] = {}
        #: 测试用：数一数事务嵌套层数，验证调用方确实把该原子的操作包起来了。
        self.transaction_depth = 0
        self.transactions_opened = 0
        self._mutation_locks = {}

    # -- 事务 ------------------------------------------------------------

    @contextmanager
    def mutation_transaction(self):
        """Deterministic try-lock/fence reference, NOT a multithreaded adapter."""
        owner = _MemoryMutationOwner(self)
        try:
            with self.transaction():
                yield owner
                owner.validate()
        finally:
            owner.active = False
            for key in owner.keys:
                if self._mutation_locks.get(key) is owner:
                    del self._mutation_locks[key]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """嵌套边界使用快照回滚；仅作同步端口参照，不证明数据库隔离。"""
        collections = ("reports", "observations", "identities", "identity_records", "current", "aggregates",
                       "aggregate_generations", "active_aggregate_generations",
                       "calendar", "reminders", "sync_state", "rule_state", "outbox",
                       "receipts", "retractions", "conflicts")
        before = {key: deepcopy(getattr(self, key)) for key in collections}
        self.transaction_depth += 1
        self.transactions_opened += 1
        try:
            yield
        except BaseException:
            for key, value in before.items():
                setattr(self, key, value)
            raise
        finally:
            self.transaction_depth -= 1

    # -- 批级幂等 --------------------------------------------------------

    def put_conflict(self, record):
        key = (record.subject_id, record.conflict_id)
        if key not in self.conflicts:
            self.conflicts[key] = deepcopy(record)
        return deepcopy(self.conflicts[key])

    def list_conflicts(self, *, subject_id, signal=None, source=None,
                       fact_key=None, status=None, start=None, end=None,
                       limit=None, offset=0):
        if status not in (None, "pending", "resolved"):
            raise ValueError("conflict status must be pending or resolved")
        return deepcopy(sorted((r for r in self.conflicts.values()
                                if r.subject_id == subject_id
                                and (signal is None or r.signal == signal)
                                and (source is None or r.source == source)
                                and (fact_key is None or r.fact_key == fact_key)
                                and (status is None or r.status == status)
                                and (start is None or r.created_at >= start)
                                and (end is None or r.created_at <= end)),
                               key=lambda r: (r.created_at, r.conflict_id))[
                                   offset:None if limit is None else offset + limit])

    def resolve_conflict(self, *, subject_id, conflict_id, revision,
                         semantic_digest, observation_id, resolved_at):
        key = (subject_id, conflict_id)
        old = self.conflicts.get(key)
        if old is None:
            return False
        if old.status == "resolved":
            return (old.resolution_revision == revision
                    and old.resolution_semantic_digest == semantic_digest
                    and old.resolution_observation_id == observation_id)
        if _compare_revisions(revision, old.candidate_revision) != 1:
            return False
        self.conflicts[key] = replace(old, status="resolved", updated_at=resolved_at,
                                      resolved_at=resolved_at, resolution_revision=revision,
                                      resolution_semantic_digest=semantic_digest,
                                      resolution_observation_id=observation_id)
        return True

    def claim_report(self, *, subject_id, producer, report_id, payload_digest,
                     received_at) -> IngestReceipt:
        key = (subject_id, producer, report_id)
        prior = self.reports.get(key)
        if prior is not None:
            if prior.payload_digest == payload_digest and prior.status != INGEST_ACCEPTED:
                return replace(prior, observations_applied=0)
            status = (INGEST_DUPLICATE if prior.payload_digest == payload_digest
                      else INGEST_CONFLICT)
            return IngestReceipt(
                subject_id=subject_id, producer=producer, report_id=report_id,
                payload_digest=prior.payload_digest, received_at=prior.received_at,
                status=status,
                error_code=None if status == INGEST_DUPLICATE else "digest_mismatch",
                observations_applied=0,
            )
        fresh = IngestReceipt(
            subject_id=subject_id, producer=producer, report_id=report_id,
            payload_digest=payload_digest, received_at=received_at,
            status=INGEST_ACCEPTED,
        )
        self.reports[key] = fresh
        return fresh

    def finalize_report(self, receipt):
        key = (receipt.subject_id, receipt.producer, receipt.report_id)
        prior = self.reports.get(key)
        if prior is None or prior.payload_digest != receipt.payload_digest:
            raise ValueError("report finalization requires matching claim")
        if prior.status != INGEST_ACCEPTED and prior != receipt:
            raise ValueError("cannot overwrite a terminal report failure")
        self.reports[key] = receipt

    def backfill_report_digest(self, *, subject_id, producer, report_id,
                               expected_digest, payload_digest):
        key = (subject_id, producer, report_id)
        prior = self.reports.get(key)
        if prior is None:
            return False
        if prior.payload_digest == payload_digest:
            return True
        if (prior.payload_digest != expected_digest or prior.payload_digest.startswith("v2:")
                or not payload_digest.startswith("v2:")):
            return False
        self.reports[key] = replace(prior, payload_digest=payload_digest)
        return True

    # -- 观测 ------------------------------------------------------------

    def append_observation(self, observation: StoredObservation) -> bool:
        if observation.observation_id in self.observations:
            return False
        self.observations[observation.observation_id] = observation
        return True

    def list_observations(self, *, subject_id, signal, start=None, end=None,
                          cursor=None, limit=100):
        rows = sorted(
            (o for o in self.observations.values()
             if o.subject_id == subject_id and o.signal == signal
             and (start is None or o.occurred_at >= start)
             and (end is None or o.occurred_at <= end)),
            key=lambda o: (o.occurred_at, o.observation_id),
        )
        offset = int(cursor) if cursor else 0
        page = rows[offset:offset + limit]
        nxt = str(offset + limit) if offset + limit < len(rows) else None
        return page, nxt

    def delete_observations(self, *, subject_id, signal=None, before=None) -> int:
        doomed = [
            k for k, o in self.observations.items()
            if o.subject_id == subject_id
            and (signal is None or o.signal == signal)
            and (before is None or o.occurred_at < before)
        ]
        for k in doomed:
            del self.observations[k]
        return len(doomed)

    # -- 去重身份 --------------------------------------------------------

    def remember_identity(self, identity: DurableDedupeIdentity) -> bool:
        key = (identity.subject_id, identity.signal, identity.source,
               identity.source_event_identity_digest)
        if key in self.identities:
            return False
        self.identities.add(key)
        self.identity_records[key] = identity
        return True

    def has_seen_identity(self, *, subject_id, signal, source, digest) -> bool:
        return (subject_id, signal, source, digest) in self.identities

    def list_identities(self, *, subject_id, signal, source=None, fact_key=None):
        scoped = [self.identity_records.get(key) or DurableDedupeIdentity(
            subject_id=subject_id, signal=signal, source=key[2],
            source_event_identity_digest=key[3], first_applied_at=_EPOCH,
        ) for key in sorted(self.identities) if key[:2] == (subject_id, signal)
            and (source is None or key[2] == source)]
        return [row for row in scoped if fact_key is None or row.fact_key is None or row.fact_key == fact_key]

    def backfill_identity(self, identity):
        key = (identity.subject_id, identity.signal, identity.source,
               identity.source_event_identity_digest)
        if key not in self.identities:
            raise ValueError("cannot backfill an unseen identity")
        existing = self.identity_records.get(key)
        if existing is not None and existing.fact_key is not None:
            # An exact opaque match may have established identity but not date.
            # Restored persisted detail may fill date/Current partition only
            # when unknown; no identity/content/known attribution is replaced.
            candidate = existing
            if existing.dimension_key is None and identity.dimension_key is not None:
                candidate = replace(candidate, dimension_key=identity.dimension_key)
            if (existing.semantic_digest is None and existing.effective_local_date is None
                    and identity.effective_local_date is not None):
                candidate = replace(candidate, effective_local_date=identity.effective_local_date)
            if candidate == identity:
                self.identity_records[key] = identity
                return
            if existing != identity:
                raise ValueError("conflicting durable identity metadata")
            return
        self.identity_records[key] = identity

    # -- 当前值 ----------------------------------------------------------

    def get_current(self, *, subject_id, signals):
        out: dict[str, list[CurrentProjection]] = {s: [] for s in signals}
        for (subj, sig, _dim), proj in self.current.items():
            if subj == subject_id and sig in out:
                out[sig].append(proj)
        return out

    def compare_and_put_current(self, projection, *, expected_version) -> bool:
        key = (projection.subject_id, projection.signal, projection.dimension_key)
        existing = self.current.get(key)
        actual = existing.version if existing else -1
        if actual != expected_version:
            return False
        self.current[key] = projection
        return True

    # -- 聚合 ------------------------------------------------------------

    def delete_aggregates(self, *, subject_id, signal, before) -> int:
        doomed = [k for k, v in self.aggregates.items()
                  if v.subject_id == subject_id and v.signal == signal
                  and v.local_date < before]
        for k in doomed:
            del self.aggregates[k]
        return len(doomed)

    def get_aggregate(self, *, subject_id, signal, start_date, end_date,
                      aggregation_kind=None, limit=None, offset=0):
        rows = [
            a for (subj, sig, day, kind, _v, _gid), a in self.aggregates.items()
            if subj == subject_id and sig == signal
            and start_date <= day <= end_date
            and (aggregation_kind is None or kind == aggregation_kind)
        ]
        active = self.active_aggregate_generations.get((subject_id, signal, aggregation_kind or "daily"))
        rows.sort(key=lambda a: (a.local_date, a.aggregation_kind,
                                 0 if a.generation_id == active else 1,
                                 a.aggregation_version, a.generation_id or ""))
        return rows[offset:None if limit is None else offset + limit]

    def put_aggregate(self, aggregate: DailyAggregate) -> None:
        generation_id = aggregate.generation_id or f"legacy-v{aggregate.aggregation_version}"
        if aggregate.generation_id != generation_id:
            aggregate = replace(aggregate, generation_id=generation_id)
        scope = (aggregate.subject_id, aggregate.signal, aggregate.aggregation_kind)
        generation_key = (*scope, generation_id)
        generation = self.aggregate_generations.get(generation_key)
        if generation is None:
            # Explicit legacy/bootstrap rule: the first directly inserted
            # complete generation becomes active. Later versions are retained
            # for audit until an explicit activation CAS.
            active = self.active_aggregate_generations.get(scope)
            status = "active" if active is None and aggregate.completeness == "complete" else (
                "complete" if aggregate.completeness == "complete" else "incomplete")
            generation = AggregateGeneration(
                generation_id=generation_id, subject_id=aggregate.subject_id,
                signal=aggregate.signal, aggregation_kind=aggregate.aggregation_kind,
                aggregation_version=aggregate.aggregation_version,
                requested_start_date=aggregate.local_date,
                requested_end_date=aggregate.local_date,
                status=status, completeness=aggregate.completeness,
                accounted_dates=(aggregate.local_date,),
                incomplete_dates=((aggregate.local_date,)
                                  if aggregate.completeness == "incomplete" else ()),
                incomplete_reasons=aggregate.incomplete_reasons,
                created_at=aggregate.updated_at, updated_at=aggregate.updated_at,
                activated_at=aggregate.updated_at if status == "active" else None,
            )
            self.aggregate_generations[generation_key] = generation
            if status == "active":
                self.active_aggregate_generations[scope] = generation_id
        elif generation.status == "active":
            days = tuple(sorted(set(generation.accounted_dates) | {aggregate.local_date}))
            self.aggregate_generations[generation_key] = replace(
                generation,
                requested_start_date=min(generation.requested_start_date, aggregate.local_date),
                requested_end_date=max(generation.requested_end_date, aggregate.local_date),
                accounted_dates=days, updated_at=aggregate.updated_at or generation.updated_at,
            )
        key = (
            aggregate.subject_id, aggregate.signal, aggregate.local_date,
            aggregate.aggregation_kind, aggregate.aggregation_version, generation_id,
        )
        existing = self.aggregates.get(key)
        self.aggregates[key] = replace(aggregate, version=existing.version + 1 if existing else 0)

    def compare_and_put_aggregate(self, aggregate, *, expected_version) -> bool:
        key = (aggregate.subject_id, aggregate.signal, aggregate.local_date,
               aggregate.aggregation_kind, aggregate.aggregation_version,
               aggregate.generation_id or f"legacy-v{aggregate.aggregation_version}")
        existing = self.aggregates.get(key)
        if (existing.version if existing else -1) != expected_version:
            return False
        self.put_aggregate(aggregate)
        return True

    @staticmethod
    def _generation_identity(generation):
        return (generation.generation_id, generation.subject_id, generation.signal,
                generation.aggregation_kind, generation.aggregation_version,
                generation.requested_start_date, generation.requested_end_date)

    def put_aggregate_generation(self, generation):
        key = (generation.subject_id, generation.signal,
               generation.aggregation_kind, generation.generation_id)
        existing = self.aggregate_generations.get(key)
        if existing is not None:
            if self._generation_identity(existing) != self._generation_identity(generation):
                raise ValueError("conflicting aggregate generation identity")
            return False
        self.aggregate_generations[key] = deepcopy(generation)
        return True

    def update_aggregate_generation(self, generation):
        key = (generation.subject_id, generation.signal,
               generation.aggregation_kind, generation.generation_id)
        existing = self.aggregate_generations.get(key)
        if existing is None:
            raise ValueError("aggregate generation does not exist")
        if self._generation_identity(existing) != self._generation_identity(generation):
            raise ValueError("aggregate generation immutable identity changed")
        allowed = {
            "building": {"building", "complete", "failed", "incomplete"},
            "complete": {"complete", "active", "failed"},
            "active": {"active", "complete"},
            "incomplete": {"incomplete", "failed"},
            "failed": {"failed"},
        }
        if generation.status not in allowed[existing.status]:
            raise ValueError(f"invalid aggregate generation transition {existing.status}->{generation.status}")
        self.aggregate_generations[key] = deepcopy(generation)

    def get_aggregate_generation(self, *, subject_id, signal, aggregation_kind, generation_id):
        return deepcopy(self.aggregate_generations.get(
            (subject_id, signal, aggregation_kind, generation_id)))

    def list_aggregate_generations(self, *, subject_id, signal, aggregation_kind):
        return deepcopy(sorted((g for (sub, sig, kind, _), g in self.aggregate_generations.items()
                                if (sub, sig, kind) == (subject_id, signal, aggregation_kind)),
                               key=lambda g: ((g.created_at or _EPOCH), g.generation_id)))

    def get_active_aggregate_generation(self, *, subject_id, signal, aggregation_kind):
        gid = self.active_aggregate_generations.get((subject_id, signal, aggregation_kind))
        if gid is None:
            return None
        return deepcopy(self.aggregate_generations.get((subject_id, signal, aggregation_kind, gid)))

    def activate_aggregate_generation(self, *, subject_id, signal, aggregation_kind,
                                      generation_id, expected_active_generation_id,
                                      activated_at):
        scope = (subject_id, signal, aggregation_kind)
        if self.active_aggregate_generations.get(scope) != expected_active_generation_id:
            return False
        key = (*scope, generation_id)
        generation = self.aggregate_generations.get(key)
        if generation is None or generation.status != "complete" or generation.completeness != "complete":
            return False
        required = {
            generation.requested_start_date + timedelta(days=i)
            for i in range((generation.requested_end_date - generation.requested_start_date).days + 1)
        }
        if set(generation.accounted_dates) != required or generation.incomplete_dates:
            return False
        if expected_active_generation_id is not None:
            old = self.aggregate_generations.get((*scope, expected_active_generation_id))
            if old is None:
                return False
            # A cutover replaces the whole ordinary-read generation. A narrower
            # candidate would make previously visible history disappear; reject
            # it instead of mixing the old version outside candidate coverage.
            if (generation.requested_start_date > old.requested_start_date
                    or generation.requested_end_date < old.requested_end_date):
                return False
        rows = [a for a in self.aggregates.values() if a.generation_id == generation_id
                and (a.subject_id, a.signal, a.aggregation_kind) == scope]
        if {a.local_date for a in rows} != required or any(a.completeness != "complete" for a in rows):
            return False
        if expected_active_generation_id is not None:
            old_key = (*scope, expected_active_generation_id)
            old = self.aggregate_generations.get(old_key)
            if old is not None:
                self.aggregate_generations[old_key] = replace(old, status="complete")
        self.aggregate_generations[key] = replace(
            generation, status="active", activated_at=activated_at, updated_at=activated_at)
        self.active_aggregate_generations[scope] = generation_id
        return True

    def mark_active_aggregate_incomplete(self, *, subject_id, signal, aggregation_kind,
                                         local_date, reason, updated_at):
        scope = (subject_id, signal, aggregation_kind)
        gid = self.active_aggregate_generations.get(scope)
        if gid is None:
            return False
        key = (*scope, gid)
        generation = self.aggregate_generations[key]
        self.aggregate_generations[key] = replace(
            generation, completeness="incomplete",
            incomplete_dates=tuple(sorted(set(generation.incomplete_dates) | {local_date})),
            incomplete_reasons=tuple(sorted(set(generation.incomplete_reasons) | {reason})),
            updated_at=updated_at,
        )
        for aggregate_key_, aggregate in list(self.aggregates.items()):
            if (aggregate.subject_id, aggregate.signal, aggregate.aggregation_kind,
                    aggregate.generation_id, aggregate.local_date) == (
                    subject_id, signal, aggregation_kind, gid, local_date):
                self.aggregates[aggregate_key_] = replace(
                    aggregate, completeness="incomplete",
                    incomplete_reasons=tuple(sorted(set(aggregate.incomplete_reasons) | {reason})),
                    updated_at=updated_at,
                )
        return True

    # -- 来源镜像 --------------------------------------------------------

    def get_sync_state(self, *, subject_id, source, collection_kind):
        return self.sync_state.get((subject_id, source, collection_kind))

    def put_sync_state(self, state: SourceSyncState) -> None:
        self.sync_state[(state.subject_id, state.source, state.collection_kind)] = state

    def upsert_calendar_events(self, *, subject_id, events) -> None:
        # 键里带 source —— 少了它，两个来源系统里碰巧同 id 的日程会互相覆盖，
        # 而全量同步还会把另一个来源的条目当成"这轮没见到"删掉。
        for e in events:
            self.calendar[(subject_id, e.source, e.source_account_id,
                           e.source_calendar_id, e.source_event_id)] = e

    def upsert_reminders(self, *, subject_id, items) -> None:
        for r in items:
            self.reminders[(subject_id, r.source, r.source_account_id,
                            r.source_list_id, r.source_reminder_id)] = r

    def list_calendar_events(self, *, subject_id, start=None, end=None,
                             limit=50, offset=0):
        rows = [v for k, v in self.calendar.items() if k[0] == subject_id]
        keep = []
        for item in rows:
            at = item.event_fields.get("start_at")
            # 时间不明的条目**保留** —— 和删除那边同一条纪律：证明不了它在
            # 范围外，就不能替用户把它藏起来。
            if at is not None and start is not None and at < start:
                continue
            if at is not None and end is not None and at > end:
                continue
            keep.append(item)
        keep.sort(key=lambda i: (i.event_fields.get("start_at") is None,
                                 i.event_fields.get("start_at") or _EPOCH,
                                 i.source_event_id))
        return keep[offset:offset + limit]

    def list_reminders(self, *, subject_id, include_completed=False,
                       limit=50, offset=0):
        keep = [
            v for k, v in self.reminders.items()
            if k[0] == subject_id
            and (include_completed or not v.reminder_fields.get("is_completed"))
        ]
        keep.sort(key=lambda i: (i.reminder_fields.get("due_at") is None,
                                 i.reminder_fields.get("due_at") or _EPOCH,
                                 i.source_reminder_id))
        return keep[offset:offset + limit]

    def record_retraction(self, retraction) -> bool:
        key = (retraction.subject_id, retraction.signal,
               retraction.source, retraction.source_event_id)
        if key in self.retractions:
            return False
        self.retractions[key] = retraction
        return True

    def scrub_event_snapshots(self, *, subject_id, signal, source, source_event_id,
                              now, reason="fact_retracted", observation_ids=None,
                              canonical_fact_key=None) -> int:
        hit = 0
        for event_id, entry in list(self.outbox.items()):
            if entry.subject_id != subject_id or entry.fact_snapshot.get("signal") != signal:
                continue
            # Legacy missing provenance is not evidence of independence. Fail
            # closed for that signal until adapters backfill canonical refs.
            matches = not entry.fact_dependencies_complete or not entry.fact_dependencies or any(
                (ref.get("subject_id"), ref.get("signal"), ref.get("source"), ref.get("source_event_id"))
                == (subject_id, signal, source, source_event_id)
                and (observation_ids is None or ref.get("observation_id") in observation_ids)
                and (canonical_fact_key is None or ref.get("fact_key") == canonical_fact_key)
                for ref in entry.fact_dependencies)
            if not matches or entry.invalidated_at is not None:
                continue
            audit_keys = ("event_id", "definition_id", "definition_version", "subject_id",
                          "type", "signal", "field", "occurred_at", "received_at", "schema_version")
            snap = {key: entry.fact_snapshot[key] for key in audit_keys if key in entry.fact_snapshot}
            snap.update(previous=None, current=None, retracted=reason == "fact_retracted",
                        invalidated=True, context={"scope": entry.fact_snapshot.get("context", {}).get("scope")})
            state = entry.delivery_state
            if state in (_delivery.PENDING, _delivery.CLAIMED):
                state = _delivery.UNKNOWN if entry.dispatch_started_at else _delivery.INVALIDATED
            self.outbox[event_id] = replace(
                entry, fact_snapshot=snap, delivery_state=state,
                invalidated_at=now, invalidation_reason=reason,
                claim_token=entry.claim_token if state == _delivery.UNKNOWN else None,
                lease_owner=entry.lease_owner if state == _delivery.UNKNOWN else None,
                lease_expires_at=entry.lease_expires_at if state == _delivery.UNKNOWN else None,
                budget_reservation_id=(entry.budget_reservation_id
                                       if state in (_delivery.UNKNOWN, _delivery.DELIVERED) else None))
            hit += 1
        return hit

    def list_retractions(self, *, subject_id, signal, source_event_ids=None):
        wanted = set(source_event_ids) if source_event_ids is not None else None
        return [r for (sub, sig, _src, eid), r in self.retractions.items()
                if sub == subject_id and sig == signal
                and (wanted is None or eid in wanted)]

    def delete_source_items(self, *, subject_id, source, collection_kind,
                            deleted_items) -> int:
        store = self.calendar if collection_kind == "calendar" else self.reminders
        # key = (subject, source, account, collection, item_id)。
        # **五段全比**：少一层就会命中同名的兄弟条目。
        wanted = {(i.source_account_id, i.source_collection_id, i.source_item_id)
                  for i in deleted_items}
        doomed = [k for k in store
                  if k[0] == subject_id and k[1] == source
                  and (k[2], k[3], k[4]) in wanted]
        for k in doomed:
            del store[k]
        return len(doomed)

    def apply_source_snapshot(self, *, subject_id, source, collection_kind, sync_id,
                              coverage_start, coverage_end, snapshot_kind) -> int:
        # 增量同步没有资格删任何东西 —— 它只知道"变了什么"，不知道"还剩什么"。
        if snapshot_kind != "full":
            return 0
        store = self.calendar if collection_kind == "calendar" else self.reminders
        doomed = []
        for key, item in store.items():
            # 🔴 必须同时限定 subject **和 source**。只按 subject 删的话，
            # 一次 source="ios" 的全量同步会把 Google 日历的条目一起删掉 ——
            # 它们当然没出现在这一轮 ios 的批次里。
            if (key[0] != subject_id or item.source != source
                    or item.last_seen_sync_id == sync_id):
                continue
            # 🔴 只删【能证明落在覆盖范围内】的。拿局部窗口去删窗口外的数据，
            # 会让用户发现自己去年的日程凭空消失，而且不可逆。
            # **时间不明的条目一律不删** —— 证明不了它在范围内，就没有资格删它。
            start = (item.event_fields.get("start_at")
                     if isinstance(item, CalendarEventMirror)
                     else item.reminder_fields.get("due_at"))
            if start is None or not (coverage_start <= start <= coverage_end):
                continue
            doomed.append(key)
        for key in doomed:
            del store[key]
        return len(doomed)

    # -- 规则状态 --------------------------------------------------------

    def get_rule_state(self, *, subject_id, definition_id, scope_key):
        return self.rule_state.get((subject_id, definition_id, scope_key))

    def list_rule_states(self, *, subject_id):
        return [(did, scope, deepcopy(raw)) for (sub, did, scope), raw in self.rule_state.items()
                if sub == subject_id]

    def put_rule_state(self, *, subject_id, definition_id, scope_key, state) -> None:
        self.rule_state[(subject_id, definition_id, scope_key)] = dict(state)

    # -- 事件与投递 ------------------------------------------------------

    def enqueue_event(self, entry: EventOutboxEntry) -> bool:
        if entry.event_id in self.outbox:
            return False
        self.outbox[entry.event_id] = entry
        return True

    def claim_pending_event(self, *, worker_id, now, lease_seconds):
        from dataclasses import replace
        from datetime import timedelta
        for event_id, entry in sorted(self.outbox.items()):
            if (entry.delivery_state == _delivery.CLAIMED and entry.dispatch_started_at
                    and entry.lease_expires_at is not None and entry.lease_expires_at <= now):
                self.mark_dispatch_unknown(event_id=event_id, claim_token=entry.claim_token)
                continue
            claimable = (
                entry.delivery_state == _delivery.PENDING
                or (entry.delivery_state == _delivery.CLAIMED
                    and entry.lease_expires_at is not None
                    and entry.lease_expires_at <= now)
            )
            if not claimable:
                continue
            if entry.next_attempt_at is not None and entry.next_attempt_at > now:
                continue
            from ..contracts.mutation import event_key
            with self.mutation_transaction() as owner:
                owner.acquire((event_key(entry.subject_id, entry.fact_snapshot.get("signal", "")),))
                entry = self.outbox[event_id]
                if entry.invalidated_at or entry.dispatch_started_at or not (
                    entry.delivery_state == _delivery.PENDING or
                    entry.delivery_state == _delivery.CLAIMED and entry.lease_expires_at is not None
                    and entry.lease_expires_at <= now
                ) or entry.next_attempt_at is not None and entry.next_attempt_at > now:
                    continue
                claimed = replace(
                    entry, delivery_state=_delivery.CLAIMED,
                    attempt_count=entry.attempt_count + 1, lease_owner=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    budget_reservation_id=f"resv_{event_id}_{entry.attempt_count + 1}",
                    claim_token=f"{worker_id}:{entry.attempt_count + 1}")
                self.outbox[event_id] = claimed
            return claimed
        return None

    def begin_event_dispatch(self, *, event_id, claim_token, now):
        from ..contracts.mutation import event_key
        entry = self.outbox[event_id]
        with self.mutation_transaction() as owner:
            owner.acquire((event_key(entry.subject_id, entry.fact_snapshot.get("signal", "")),))
            entry = self.outbox[event_id]
            if (entry.delivery_state != _delivery.CLAIMED or not claim_token
                    or entry.claim_token != claim_token or entry.invalidated_at
                    or entry.dispatch_started_at or entry.lease_expires_at is None
                    or entry.lease_expires_at <= now):
                return None
            started = replace(entry, dispatch_started_at=now)
            self.outbox[event_id] = started
            return started

    def mark_dispatch_unknown(self, *, event_id, claim_token):
        from ..contracts.mutation import event_key
        entry = self.outbox[event_id]
        with self.mutation_transaction() as owner:
            owner.acquire((event_key(entry.subject_id, entry.fact_snapshot.get("signal", "")),))
            entry = self.outbox[event_id]
            if (entry.claim_token != claim_token or not claim_token or not entry.dispatch_started_at
                    or entry.delivery_state not in (_delivery.CLAIMED, _delivery.UNKNOWN)):
                return False
            self.outbox[event_id] = replace(entry, delivery_state=_delivery.UNKNOWN)
            return True

    def record_wake_receipt(self, *, receipt, next_state, claim_token=None,
                            next_attempt_at=None) -> str | bool:
        from ..contracts.mutation import event_key
        entry = self.outbox[receipt.event_id]
        with self.mutation_transaction() as owner:
            owner.acquire((event_key(entry.subject_id, entry.fact_snapshot.get("signal", "")),))
            return self._record_wake_receipt(receipt=receipt, next_state=next_state,
                                             claim_token=claim_token, next_attempt_at=next_attempt_at)

    def _record_wake_receipt(self, *, receipt, next_state, claim_token=None,
                             next_attempt_at=None) -> str | bool:
        from dataclasses import replace
        entry = self.outbox.get(receipt.event_id)
        if entry is None:
            raise KeyError(f"unknown event_id {receipt.event_id!r}")
        if (not claim_token or entry.claim_token != claim_token
                or entry.delivery_state not in (_delivery.CLAIMED, _delivery.UNKNOWN)):
            # 令牌过期:这个事件已经被别人接管了。只记审计,不改状态。
            if receipt not in self.receipts:
                self.receipts.append(receipt)
            return False
        if entry.invalidated_at is not None and next_state in (_delivery.PENDING, _delivery.DEAD_LETTER):
            next_state = _delivery.INVALIDATED
        _delivery.assert_transition(entry.delivery_state, next_state)
        if receipt not in self.receipts:
            self.receipts.append(receipt)
        self.outbox[receipt.event_id] = replace(
            entry,
            delivery_state=next_state,
            next_attempt_at=next_attempt_at,
            lease_owner=None,
            lease_expires_at=None,
            dispatch_started_at=None if next_state == _delivery.PENDING else entry.dispatch_started_at,
            # 兑现或释放：只有 delivered 会把占位变成真正的消耗。
            claim_token=None,
            budget_reservation_id=(
                entry.budget_reservation_id
                if _delivery.consumes_budget(next_state) else None
            ),
        )
        return next_state

    def list_pending_events(self, *, subject_id=None, limit=100):
        return [
            e for e in self.outbox.values()
            if e.delivery_state in (_delivery.PENDING, _delivery.CLAIMED)
            and (subject_id is None or e.subject_id == subject_id)
        ][:limit]

    def list_events(self, *, subject_id, delivery_states=None, event_type=None,
                    start=None, end=None, limit=50, offset=0):
        # 注意这里**没有** is_terminal 过滤 —— 排查要的正是终态。
        rows = [e for e in self.outbox.values() if e.subject_id == subject_id]
        if delivery_states is not None:
            wanted = set(delivery_states)
            rows = [e for e in rows if e.delivery_state in wanted]
        if event_type is not None:
            rows = [e for e in rows if e.event_type == event_type]
        if start is not None:
            rows = [e for e in rows if e.occurred_at >= start]
        if end is not None:
            rows = [e for e in rows if e.occurred_at <= end]
        rows.sort(key=lambda e: (e.occurred_at, e.event_id), reverse=True)
        return rows[offset:offset + limit]

    # -- 用户数据 --------------------------------------------------------

    def purge_subject(self, *, subject_id) -> dict[str, int]:
        doomed_event_ids = {e.event_id for e in self.outbox.values()
                            if e.subject_id == subject_id}

        def drop(store: dict, pick) -> int:
            doomed = [k for k, v in store.items() if pick(k, v) == subject_id]
            for k in doomed:
                del store[k]
            return len(doomed)

        counts = {
            "reports": drop(self.reports, lambda k, v: k[0]),
            "observations": drop(self.observations, lambda k, v: v.subject_id),
            "current": drop(self.current, lambda k, v: k[0]),
            "aggregates": drop(self.aggregates, lambda k, v: k[0]),
            "aggregate_generations": drop(self.aggregate_generations, lambda k, v: k[0]),
            "active_aggregate_generations": drop(
                self.active_aggregate_generations, lambda k, v: k[0]),
            "calendar": drop(self.calendar, lambda k, v: k[0]),
            "reminders": drop(self.reminders, lambda k, v: k[0]),
            "sync_state": drop(self.sync_state, lambda k, v: k[0]),
            "rule_state": drop(self.rule_state, lambda k, v: k[0]),
            "outbox": drop(self.outbox, lambda k, v: v.subject_id),
            "retractions": drop(self.retractions, lambda k, v: k[0]),
            "conflicts": drop(self.conflicts, lambda k, v: k[0]),
        }
        before = len(self.identities)
        self.identities = {i for i in self.identities if i[0] != subject_id}
        counts["identities"] = before - len(self.identities)
        drop(self.identity_records, lambda k, v: k[0])
        # 回执按它对应事件的 subject 清理。以前这里只是原样复制了一遍列表 ——
        # "删除我的数据"这件事没有部分成功。
        doomed_events = {k for k in doomed_event_ids}
        kept = [r for r in self.receipts if r.event_id not in doomed_events]
        counts["receipts"] = len(self.receipts) - len(kept)
        self.receipts = kept
        return counts


__all__ = ["InMemoryStorage"]
