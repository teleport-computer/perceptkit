"""按新算法版本重算日聚合。

产品规范 §12「聚合算法升级 | 使用新 aggregation_version 重算，不静默改写语义」。
先前只做了一半：换版本号、按版本过滤（所以不会两种口径混着 fold），
**但没有任何东西真的把历史重算出来** —— 升级之后旧日子永远停在旧版本，
新旧数据一起查就是一段一段口径不同的曲线。

## 这件事唯一真正危险的地方

重算是**从明细重新折出来**的。而明细有保留期，聚合可以永久 ——
典型形态就是"明细 1 年、聚合永久"。所以：

    2023 年的那一天    明细早就清理了 → 能读到的只有零星几条,甚至一条没有
                       照样折一遍 → 得到一个"当天走了 200 步"的永久统计
                       → 数字错了一个数量级,没有任何地方报错,而且旧值已被覆盖

**所以默认拒绝重算明细可能已经不完整的日子**，而不是尽力而为。
``allow_incomplete=True`` 只允许生成显式 incomplete 的审计候选，永不激活、
也不覆盖普通查询正在读取的完整 generation。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Mapping
from uuid import uuid4

from ..contracts.records import AggregateGeneration, DailyAggregate
from ..manifest.types import PERMANENT, SignalDefinition
from ..ports.storage import StoragePort
from ..contracts.mutation import aggregate_generation_key, aggregate_key, canonical_keys
from . import aggregate as _aggregate
from .retract import canonical_revisions as _canonical
from .retract import drop_retracted as _drop_retracted


@dataclass
class RecomputeOutcome:
    """重算了哪些天、拒绝了哪些天、为什么拒绝。"""

    rebuilt: list[date] = field(default_factory=list)
    #: ``(那一天, 为什么没算)``
    skipped: list[tuple[date, str]] = field(default_factory=list)
    incomplete: list[tuple[date, str]] = field(default_factory=list)
    generation_id: str | None = None
    activated: bool = False

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.incomplete


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def details_may_be_incomplete(
    sig: SignalDefinition, day: date, *, today: date,
) -> bool:
    """这一天的明细有没有可能已经被保留期清掉了。

    **宁可误判成"可能不全"**：误判的代价是拒绝重算一天（可恢复），
    反过来的代价是写下一个错了一个数量级的永久统计（不可恢复）。
    """
    if sig.history_retention_days == PERMANENT:
        return False
    if not sig.stores_history:
        return True                     # 根本不存明细，无从重算
    cutoff = today - timedelta(days=sig.history_retention_days)
    return day < cutoff


def recompute_day(
    storage: StoragePort,
    sig: SignalDefinition,
    *,
    subject_id: str,
    day: date,
    version: int,
    updated_at: datetime,
    generation_id: str | None = None,
    completeness: str = "complete",
    incomplete_reasons: tuple[str, ...] = (),
) -> DailyAggregate:
    """把某一天的明细重新折成一份聚合文档。**不写库**，由调用方决定写不写。"""
    # 刻意**不按 occurred_at 开时间窗**：一条观测归到哪一天由
    # effective_local_date 决定，跨午夜的睡眠整段归到醒来那天，
    # 按 occurred_at 截窗口会把它切掉一半。宁可多读再过滤。
    rows, cursor = storage.list_observations(
        subject_id=subject_id, signal=sig.key, cursor=None, limit=500,
    )
    # 明细可能不止一页。重算必须读全 —— 只读第一页就是在算一份残缺统计。
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=subject_id, signal=sig.key, cursor=cursor, limit=500,
        )
        page.extend(more)

    # A higher revision may move the Fact to a different day. Select its active
    # revision before filtering dates, otherwise the old day keeps the old one.
    same_day = [o for o in _canonical(page, sig) if o.effective_local_date == day]
    # 被来源撤回的那些不算数。**放在 canonical 之后**：撤回针对的是一条
    # 源事实，而 canonical 已经把同一源事实的多个修订收敛成一条了 ——
    # 先滤会让"撤回了 revision 1、但 revision 2 还在"这种情况删错东西。
    same_day = _drop_retracted(storage, same_day, subject_id=subject_id,
                               signal=sig.key)
    same_day = [o for o in same_day if o.availability == "observed"]
    same_day.sort(key=lambda o: (o.occurred_at, o.observation_id))

    doc: dict = {}
    for obs in same_day:
        doc = _aggregate.fold_into_day(
            doc, sig, obs.typed_value or {}, ts=_epoch(obs.occurred_at),
        )
    return DailyAggregate(
        subject_id=subject_id,
        signal=sig.key,
        local_date=day,
        aggregation_kind="daily",
        aggregation_version=version,
        typed_aggregate=doc,
        generation_id=generation_id,
        completeness=completeness,
        incomplete_reasons=incomplete_reasons,
        timezone_attribution=same_day[0].timezone if same_day else None,
        source_coverage={
            "observations": len(same_day),
            # 标出来这份统计是重算的，不是随观测一条条折出来的。
            # 排查"这个数字怎么变了"时，第一件事就是看它是不是被重算过。
            "recomputed": True,
        },
        updated_at=updated_at,
    )


def recompute_range(
    storage: StoragePort,
    signals: Mapping[str, SignalDefinition],
    *,
    subject_id: str,
    signal: str,
    start_date: date,
    end_date: date,
    version: int,
    now: datetime,
    allow_incomplete: bool = False,
    mutation=None,
    generation_id: str | None = None,
) -> RecomputeOutcome:
    """按 ``version`` 重算一段日期的聚合。

    ``now`` 由调用方传 —— 这个包不读时钟。它只用来判断"这一天的明细
    是不是已经过了保留期"。

    ``allow_incomplete=False``（默认）时，明细可能已经不全的日子**不算**，
    并在 ``skipped`` 里说明原因。True 只写 incomplete 审计候选，不激活。
    """
    generation_id = generation_id or f"agg-{uuid4().hex}"
    sig = signals.get(signal)
    if sig is None:
        return RecomputeOutcome(skipped=[(start_date, f"manifest 里没有 {signal}")],
                                generation_id=generation_id)
    if not sig.stores_history:
        return RecomputeOutcome(skipped=[(start_date, f"{signal} 不存明细，无从重算")],
                                generation_id=generation_id)
    if mutation is None:
        try:
            with storage.mutation_transaction() as owner:
                return recompute_range(
                    storage, signals, subject_id=subject_id, signal=signal,
                    start_date=start_date, end_date=end_date, version=version,
                    now=now, allow_incomplete=allow_incomplete, mutation=owner,
                    generation_id=generation_id,
                )
        except Exception as exc:
            # The candidate transaction rolled back, but the failed attempt is
            # still useful audit evidence. It is never active.
            failed = AggregateGeneration(
                generation_id=generation_id, subject_id=subject_id, signal=signal,
                aggregation_kind="daily", aggregation_version=version,
                requested_start_date=start_date, requested_end_date=end_date,
                status="failed", completeness="incomplete",
                failure_reason=f"{type(exc).__name__}: {exc}",
                created_at=now, updated_at=now,
            )
            try:
                with storage.mutation_transaction() as owner:
                    owner.acquire((aggregate_generation_key(subject_id, signal, "daily"),))
                    storage.put_aggregate_generation(failed)
            except Exception:
                # Preserve the original build/activation failure if the store
                # itself is unavailable and cannot record audit evidence.
                pass
            raise
    days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
    mutation.acquire((aggregate_generation_key(subject_id, signal, "daily"),))
    mutation.acquire(canonical_keys([
        aggregate_key(subject_id, signal, day, "daily", version) for day in days
    ]))
    active = storage.get_active_aggregate_generation(
        subject_id=subject_id, signal=signal, aggregation_kind="daily")
    generation = AggregateGeneration(
        generation_id=generation_id, subject_id=subject_id, signal=signal,
        aggregation_kind="daily", aggregation_version=version,
        requested_start_date=start_date, requested_end_date=end_date,
        status="building", completeness="unknown", created_at=now, updated_at=now,
    )
    storage.put_aggregate_generation(generation)
    outcome = RecomputeOutcome(generation_id=generation_id)
    accounted = []
    today = now.date()
    for day in days:
        expired = details_may_be_incomplete(sig, day, today=today)
        if expired and not allow_incomplete:
            outcome.skipped.append((
                day, "detail_retention_expired: 明细可能已被清理，候选不会激活"
            ))
            continue
        reasons = ("detail_retention_expired",) if expired else ()
        storage.put_aggregate(recompute_day(
            storage, sig, subject_id=subject_id, day=day, version=version,
            updated_at=now, generation_id=generation_id,
            completeness="incomplete" if expired else "complete",
            incomplete_reasons=reasons,
        ))
        accounted.append(day)
        if expired:
            outcome.incomplete.append((day, "detail_retention_expired"))
        else:
            outcome.rebuilt.append(day)
    if outcome.skipped or outcome.incomplete:
        storage.update_aggregate_generation(replace(
            generation, status="incomplete", completeness="incomplete",
            accounted_dates=tuple(accounted),
            incomplete_dates=(tuple(day for day, _ in outcome.incomplete)
                              + tuple(day for day, _ in outcome.skipped)),
            incomplete_reasons=tuple(sorted({
                reason.split(":", 1)[0]
                for _, reason in [*outcome.incomplete, *outcome.skipped]
            })),
            updated_at=now,
        ))
        return outcome
    complete = replace(generation, status="complete", completeness="complete",
                       accounted_dates=tuple(accounted), updated_at=now)
    storage.update_aggregate_generation(complete)
    if active is not None and (
            start_date > active.requested_start_date
            or end_date < active.requested_end_date):
        outcome.skipped.append((
            start_date, "candidate_coverage_does_not_cover_active_scope"
        ))
        return outcome
    outcome.activated = storage.activate_aggregate_generation(
        subject_id=subject_id, signal=signal, aggregation_kind="daily",
        generation_id=generation_id,
        expected_active_generation_id=active.generation_id if active else None,
        activated_at=now,
    )
    if not outcome.activated:
        raise RuntimeError("aggregate generation activation CAS/coverage validation failed")
    return outcome


def _recompute_owned_range(storage, signals, *, subject_id, signal, start_date,
                           end_date, version, now, allow_incomplete=False):
    """Internal only: caller owns every affected aggregate through commit."""
    outcome = RecomputeOutcome()
    sig = signals.get(signal)
    if sig is None:
        outcome.skipped.append((start_date, f"manifest 里没有 {signal}"))
        return outcome
    if not sig.stores_history:
        outcome.skipped.append((start_date, f"{signal} 不存明细，无从重算"))
        return outcome

    today = now.date()
    day = start_date
    while day <= end_date:
        if details_may_be_incomplete(sig, day, today=today):
            outcome.skipped.append((
                day,
                f"{day} 早于明细保留期（{sig.history_retention_days} 天），"
                "明细可能已被清理。拿残缺明细重算会写下一个数量级都不对的统计，"
                "而且旧值会被覆盖、不可恢复。active projection 已标记 incomplete",
            ))
            marker = getattr(storage, "mark_active_aggregate_incomplete", None)
            if callable(marker):
                marker(subject_id=subject_id, signal=signal, aggregation_kind="daily",
                       local_date=day, reason="detail_retention_expired", updated_at=now)
            day += timedelta(days=1)
            continue
        active_getter = getattr(storage, "get_active_aggregate_generation", None)
        active = (active_getter(subject_id=subject_id, signal=signal,
                                aggregation_kind="daily")
                  if callable(active_getter) else None)
        storage.put_aggregate(recompute_day(
            storage, sig, subject_id=subject_id, day=day,
            version=active.aggregation_version if active else version, updated_at=now,
            generation_id=active.generation_id if active else None,
        ))
        outcome.rebuilt.append(day)
        day += timedelta(days=1)
    return outcome


__all__ = [
    "RecomputeOutcome", "details_may_be_incomplete",
    "recompute_day", "recompute_range",
]
