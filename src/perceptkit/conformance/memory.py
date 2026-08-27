"""内存版存储 —— **只是测试工具，不是生产实现。**

它存在的意义是让宿主在写自己的 adapter 之前，先有一个能跑通的参照，
以及让 kit 自己的管线测试不依赖任何数据库。

🔴 **它验不出真正的事务边界和隔离级别。** 内存实现天然是原子的、天然没有
并发 —— "RuleState 和 Outbox 必须同事务"这类保证，在这里永远是绿的，
不代表真实数据库上也绿。宿主必须另外用真实数据库、两条独立连接、
在关键写操作之间打断点，才能证明那条保证成立。

在这里绿 = 端口语义、调用顺序、确定性没问题。
仅此而已。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Sequence

from ..contracts import delivery as _delivery
from ..contracts.records import (
    CalendarEventMirror,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
)
from ..contracts.receipt import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    IngestReceipt,
    WakeReceipt,
)


class InMemoryStorage:
    """把 :class:`~perceptkit.ports.storage.StoragePort` 实现在几个字典上。"""

    def __init__(self) -> None:
        self.reports: dict[tuple[str, str, str], IngestReceipt] = {}
        self.observations: dict[str, StoredObservation] = {}
        self.identities: set[tuple[str, str, str, str]] = set()
        self.current: dict[tuple[str, str, str], CurrentProjection] = {}
        self.aggregates: dict[tuple[str, str, date, str, int], DailyAggregate] = {}
        self.calendar: dict[tuple, CalendarEventMirror] = {}
        self.reminders: dict[tuple, ReminderItemMirror] = {}
        self.sync_state: dict[tuple[str, str, str], SourceSyncState] = {}
        self.rule_state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.outbox: dict[str, EventOutboxEntry] = {}
        self.receipts: list[WakeReceipt] = []
        #: 测试用：数一数事务嵌套层数，验证调用方确实把该原子的操作包起来了。
        self.transaction_depth = 0
        self.transactions_opened = 0

    # -- 事务 ------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """内存里没有真正的回滚 —— 只记录边界，供测试断言"该包起来的确实包了"。

        **不要把这里的绿当成"原子性验过了"。** 见模块开头。
        """
        self.transaction_depth += 1
        self.transactions_opened += 1
        try:
            yield
        finally:
            self.transaction_depth -= 1

    # -- 批级幂等 --------------------------------------------------------

    def claim_report(self, *, subject_id, producer, report_id, payload_digest,
                     received_at) -> IngestReceipt:
        key = (subject_id, producer, report_id)
        prior = self.reports.get(key)
        if prior is not None:
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
        return True

    def has_seen_identity(self, *, subject_id, signal, source, digest) -> bool:
        return (subject_id, signal, source, digest) in self.identities

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

    def get_aggregate(self, *, subject_id, signal, start_date, end_date,
                      aggregation_kind=None):
        return [
            a for (subj, sig, day, kind, _v), a in self.aggregates.items()
            if subj == subject_id and sig == signal
            and start_date <= day <= end_date
            and (aggregation_kind is None or kind == aggregation_kind)
        ]

    def put_aggregate(self, aggregate: DailyAggregate) -> None:
        self.aggregates[(
            aggregate.subject_id, aggregate.signal, aggregate.local_date,
            aggregate.aggregation_kind, aggregate.aggregation_version,
        )] = aggregate

    # -- 来源镜像 --------------------------------------------------------

    def get_sync_state(self, *, subject_id, source, collection_kind):
        return self.sync_state.get((subject_id, source, collection_kind))

    def put_sync_state(self, state: SourceSyncState) -> None:
        self.sync_state[(state.subject_id, state.source, state.collection_kind)] = state

    def upsert_calendar_events(self, *, subject_id, events) -> None:
        for e in events:
            self.calendar[(subject_id, e.source_account_id, e.source_calendar_id,
                           e.source_event_id)] = e

    def upsert_reminders(self, *, subject_id, items) -> None:
        for r in items:
            self.reminders[(subject_id, r.source_account_id, r.source_list_id,
                            r.source_reminder_id)] = r

    def apply_source_snapshot(self, *, subject_id, source, collection_kind, sync_id,
                              coverage_start, coverage_end, snapshot_kind) -> int:
        # 增量同步没有资格删任何东西 —— 它只知道"变了什么"，不知道"还剩什么"。
        if snapshot_kind != "full":
            return 0
        store = self.calendar if collection_kind == "calendar" else self.reminders
        doomed = []
        for key, item in store.items():
            if key[0] != subject_id or item.last_seen_sync_id == sync_id:
                continue
            # 🔴 只删覆盖范围内的。拿局部窗口去删窗口外的数据，
            # 会让用户发现自己去年的日程凭空消失，而且不可逆。
            start = (item.event_fields.get("start_at")
                     if isinstance(item, CalendarEventMirror) else None)
            if start is not None and not (coverage_start <= start <= coverage_end):
                continue
            doomed.append(key)
        for key in doomed:
            del store[key]
        return len(doomed)

    # -- 规则状态 --------------------------------------------------------

    def get_rule_state(self, *, subject_id, definition_id, scope_key):
        return self.rule_state.get((subject_id, definition_id, scope_key))

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
            claimed = replace(
                entry,
                delivery_state=_delivery.CLAIMED,
                attempt_count=entry.attempt_count + 1,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                # 额度只是占位，不是消耗 —— delivered 时才兑现。
                budget_reservation_id=f"resv_{event_id}_{entry.attempt_count + 1}",
            )
            self.outbox[event_id] = claimed
            return claimed
        return None

    def record_wake_receipt(self, *, receipt, next_state, next_attempt_at=None) -> None:
        from dataclasses import replace
        entry = self.outbox.get(receipt.event_id)
        if entry is None:
            raise KeyError(f"unknown event_id {receipt.event_id!r}")
        _delivery.assert_transition(entry.delivery_state, next_state)
        self.receipts.append(receipt)
        self.outbox[receipt.event_id] = replace(
            entry,
            delivery_state=next_state,
            next_attempt_at=next_attempt_at,
            lease_owner=None,
            lease_expires_at=None,
            # 兑现或释放：只有 delivered 会把占位变成真正的消耗。
            budget_reservation_id=(
                entry.budget_reservation_id
                if _delivery.consumes_budget(next_state) else None
            ),
        )

    def list_pending_events(self, *, subject_id=None, limit=100):
        return [
            e for e in self.outbox.values()
            if not e.is_terminal and (subject_id is None or e.subject_id == subject_id)
        ][:limit]

    # -- 用户数据 --------------------------------------------------------

    def purge_subject(self, *, subject_id) -> dict[str, int]:
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
            "calendar": drop(self.calendar, lambda k, v: k[0]),
            "reminders": drop(self.reminders, lambda k, v: k[0]),
            "sync_state": drop(self.sync_state, lambda k, v: k[0]),
            "rule_state": drop(self.rule_state, lambda k, v: k[0]),
            "outbox": drop(self.outbox, lambda k, v: v.subject_id),
        }
        before = len(self.identities)
        self.identities = {i for i in self.identities if i[0] != subject_id}
        counts["identities"] = before - len(self.identities)
        self.receipts = [r for r in self.receipts]
        return counts


__all__ = ["InMemoryStorage"]
