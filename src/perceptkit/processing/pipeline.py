"""处理管线 —— 谁在什么时候被调用。

**这就是上一版真正缺的东西。** 上一版交了一堆算式（分类、去重键、排序、日聚合），
但"按什么顺序调它们"留在了宿主的业务代码里。结果是别人拿到一盒零件和一本
没有装配图的说明书 —— 装上了跑不起来，跑起来了每个宿主的行为还不一样。

顺序在这里定死，宿主只实现被调用的方法。宿主想"先投递再落地"？做不到，
他手里根本没有那个顺序。

这个模块实现前七步（落地为止）：

    ① 批级幂等：这批处理过没有
    ② 按 manifest 校验、标准化
    ③ 观测级幂等：这条处理过没有
    ④ 写观测
    ⑤ 记住去重身份（明细将来被清理掉之后，靠它挡住重放）
    ⑥ 只在 (occurred_at, source_revision) 更新时才动当前值
    ⑦ 折进当天的聚合

规则求值、写发件箱、投递是后三步，在 ``dispatch`` 模块（批 3B）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..contracts import receipt as _receipt
from ..contracts.context import IngestContext
from ..contracts.records import (
    CONFLICT,
    IGNORE,
    REPLACE,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
)
from ..contracts.report import ReportEnvelope
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort
from ..rules.types import EventDefinition
from . import aggregate as _aggregate
from .dispatch import evaluate_and_enqueue
from .normalize import NormalizedObservation, normalize_observations

#: 聚合算法的版本。改了口径就加这个数并重算，**不原地改写旧统计的语义** ——
#: 否则同一张表里一半是老口径一半是新口径，而且看不出来。
AGGREGATION_VERSION = 1


@dataclass
class IngestOutcome:
    """一批上报处理完的结果。"""

    receipt: _receipt.IngestReceipt
    applied: list[NormalizedObservation] = field(default_factory=list)
    #: 去重挡掉的（这条之前处理过）。不是错误，是重传的正常结果。
    duplicates: list[NormalizedObservation] = field(default_factory=list)
    #: 校验没过的：``(下标, 问题清单)``。**不影响同一批里其他观测**。
    rejected: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)
    #: 同一时刻同一版本但内容不同 —— 不静默挑一个，交给宿主决定。
    conflicts: list[NormalizedObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 这批上报产生的事件（已经落进发件箱，还没投）。
    events: list[Any] = field(default_factory=list)
    #: 求值了但没触发的规则，带原因。排查"为什么没提醒我"时要用。
    rule_misses: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.conflicts


def _epoch(dt: Any) -> float:
    return dt.timestamp()


def ingest_report(
    report: ReportEnvelope,
    *,
    context: IngestContext,
    storage: StoragePort,
    signals: Mapping[str, SignalDefinition],
    definitions: Sequence[EventDefinition] = (),
    extra_evaluators: Mapping[str, Callable[..., Any]] | None = None,
    timezone_fallback: str | None = None,
    max_observations: int = 200,
    max_payload_bytes: int = 256 * 1024,
) -> IngestOutcome:
    """把一批上报走完前七步。

    ``max_observations`` / ``max_payload_bytes`` 是资源上限。前台每 30 秒一次
    上报，不设上限的话，一个构造过的 report 就能让单次 ingest 跑很久。
    超限**拒收整批**而不是截断 —— 截断会静默丢数据，比拒收难查得多。
    """
    payload_digest = _batch_digest(report)

    if len(report.observations) > max_observations:
        return IngestOutcome(
            receipt=_receipt.IngestReceipt(
                subject_id=context.subject_id, producer=report.producer,
                report_id=report.report_id, payload_digest=payload_digest,
                received_at=context.received_at, status=_receipt.INGEST_REJECTED,
                error_code="too_many_observations",
            ),
            rejected=[(-1, (
                f"一批最多 {max_observations} 条观测，收到 {len(report.observations)} 条",
            ))],
        )

    # ① 批级幂等。同 identity 同摘要 -> 返回原结果不重复处理；
    #    同 identity 异摘要 -> conflict，不静默覆盖。
    claim = storage.claim_report(
        subject_id=context.subject_id,
        producer=report.producer,
        report_id=report.report_id,
        payload_digest=payload_digest,
        received_at=context.received_at,
    )
    if claim.status != _receipt.INGEST_ACCEPTED:
        return IngestOutcome(receipt=claim)

    # ② 校验 + 标准化。
    normalized = normalize_observations(
        report.observations,
        context=context,
        signals=signals,
        source=report.producer,
        timezone_fallback=timezone_fallback,
    )
    outcome = IngestOutcome(
        receipt=claim,
        rejected=list(normalized.rejected),
        warnings=list(normalized.warnings),
    )

    for item in normalized.normalized:
        sig = signals[item.stored.signal]
        _apply_one(item, sig, context=context, storage=storage, outcome=outcome,
                   definitions=definitions, extra_evaluators=extra_evaluators)

    outcome.receipt = _receipt.IngestReceipt(
        subject_id=claim.subject_id, producer=claim.producer,
        report_id=claim.report_id, payload_digest=claim.payload_digest,
        received_at=claim.received_at, status=claim.status,
        observations_applied=len(outcome.applied),
    )
    return outcome


def _apply_one(
    item: NormalizedObservation,
    sig: SignalDefinition,
    *,
    context: IngestContext,
    storage: StoragePort,
    outcome: IngestOutcome,
    definitions: Sequence[EventDefinition] = (),
    extra_evaluators: Mapping[str, Callable[..., Any]] | None = None,
) -> None:
    """③~⑨：一条观测的落地，以及命中规则时写发件箱。

    整条包在一个事务里：观测、去重身份、当前值、聚合要么一起成功，要么一起
    不生效。分开写的话会出现"观测写了但去重身份没写"——下次重传就会重复累计。
    """
    stored = item.stored
    with storage.transaction():
        # ③ 观测级幂等。明细可能已经按保留期删掉了，所以问的是去重身份，
        #    不是"这条观测还在不在"。
        if storage.has_seen_identity(
            subject_id=context.subject_id, signal=stored.signal,
            source=stored.source, digest=item.identity_digest,
        ):
            outcome.duplicates.append(item)
            return

        # ④ 写观测。
        appended = storage.append_observation(stored)
        if not appended:
            outcome.duplicates.append(item)
            return

        # ⑤ 记住身份。**必须和 ④ 同事务** —— 只写了观测没写身份，
        #    下次重传会当成新数据再加一遍聚合。
        storage.remember_identity(DurableDedupeIdentity(
            subject_id=context.subject_id,
            signal=stored.signal,
            source=stored.source,
            source_event_identity_digest=item.identity_digest,
            first_applied_at=context.received_at,
            aggregate_scope=sig.key if sig.keeps_history_forever else None,
            # 永久聚合依赖的身份必须永久保留：明细删了之后，
            # 它是唯一还能挡住重放的东西。
            retain_until=None,
        ))

        # ⑥ 当前值。只有 observed 会更新数值；no_data / unavailable 不覆盖
        #    最后一次可靠值。
        if stored.availability == "observed":
            _update_current(item, sig, context=context, storage=storage, outcome=outcome)

        # ⑦ 聚合。
        if sig.stores_history and stored.availability == "observed":
            _update_aggregate(item, sig, context=context, storage=storage)

        # ⑧⑨ 求值 + 写发件箱。**和上面在同一个事务里** —— 事件落地了但观测
        # 没落地(或反过来)，都会让"为什么会有这个事件"永远解释不清。
        if definitions:
            rules = evaluate_and_enqueue(
                item, context=context, storage=storage,
                definitions=definitions, extra_evaluators=extra_evaluators,
            )
            outcome.events.extend(rules.events)
            outcome.rule_misses.extend(rules.misses)

    # ⑩ 事务在这里提交。走到这一行,事件就丢不了了 —— 投递由宿主的 worker 驱动。
    outcome.applied.append(item)


def _update_current(
    item: NormalizedObservation,
    sig: SignalDefinition,
    *,
    context: IngestContext,
    storage: StoragePort,
    outcome: IngestOutcome,
) -> None:
    from datetime import timedelta

    stored = item.stored
    dimension = sig.key
    existing_map = storage.get_current(
        subject_id=context.subject_id, signals=[stored.signal]
    )
    existing = None
    for candidate in existing_map.get(stored.signal, ()):
        if candidate.dimension_key == dimension:
            existing = candidate
            break

    from ..contracts.records import decide_current_update
    decision = decide_current_update(
        new_occurred_at=stored.occurred_at,
        new_revision=stored.source_revision,
        new_digest=item.content_digest,
        existing=existing,
    )
    if decision == IGNORE:
        return
    if decision == CONFLICT:
        # 不静默挑一个。"到底哪份数据生效了"说不清，比多一条冲突记录糟得多。
        outcome.conflicts.append(item)
        return

    assert decision == REPLACE
    expires = (
        stored.occurred_at + timedelta(seconds=sig.current_ttl_sec)
        if sig.current_ttl_sec > 0 else None
    )
    storage.compare_and_put_current(
        CurrentProjection(
            subject_id=context.subject_id,
            signal=stored.signal,
            dimension_key=dimension,
            typed_value=stored.typed_value,
            availability=stored.availability,
            observed_at=stored.occurred_at,
            received_at=stored.received_at,
            expires_at=expires,
            source_observation_id=stored.observation_id,
            source_revision=stored.source_revision,
            version=(existing.version + 1) if existing else 0,
            content_digest=item.content_digest,
        ),
        expected_version=existing.version if existing else -1,
    )


def _update_aggregate(
    item: NormalizedObservation,
    sig: SignalDefinition,
    *,
    context: IngestContext,
    storage: StoragePort,
) -> None:
    stored = item.stored
    day = stored.effective_local_date
    kind = "daily"
    existing = storage.get_aggregate(
        subject_id=context.subject_id, signal=stored.signal,
        start_date=day, end_date=day, aggregation_kind=kind,
    )
    prev = existing[0].typed_aggregate if existing else None
    doc = _aggregate.fold_into_day(
        prev, sig, stored.typed_value or {}, ts=_epoch(stored.occurred_at),
    )
    coverage = dict((existing[0].source_coverage if existing else {}) or {})
    coverage["observations"] = int(coverage.get("observations", 0)) + 1
    storage.put_aggregate(DailyAggregate(
        subject_id=context.subject_id,
        signal=stored.signal,
        local_date=day,
        aggregation_kind=kind,
        aggregation_version=AGGREGATION_VERSION,
        typed_aggregate=doc,
        timezone_attribution=stored.timezone,
        source_coverage=coverage,
        updated_at=stored.received_at,
    ))


def _batch_digest(report: ReportEnvelope) -> str:
    from .normalize import _canonical, _digest
    return _digest(
        report.report_id,
        report.producer,
        _canonical([
            {
                "signal": o.signal, "occurred_at": o.occurred_at.isoformat(),
                "availability": o.availability, "value": o.value,
                "source_event_id": o.source_event_id,
            }
            for o in report.observations
        ]),
    )


__all__ = ["IngestOutcome", "ingest_report", "AGGREGATION_VERSION"]
