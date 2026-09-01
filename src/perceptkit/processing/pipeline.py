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
from .normalize import NormalizedObservation, _canonical, normalize_observations

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

    # 真的量一下，而不是摆一个从不生效的参数。
    # ⚠️ 这里量的是【已经解析完】的结构 —— 真正的防线必须在宿主的 HTTP 层
    # （Content-Length / 流式读取上限）。到这一步内存和解析 CPU 已经花掉了。
    approx_bytes = len(payload_digest) + sum(
        len(_canonical(o.value or {})) for o in report.observations
    )
    if approx_bytes > max_payload_bytes:
        return IngestOutcome(
            receipt=_receipt.IngestReceipt(
                subject_id=context.subject_id, producer=report.producer,
                report_id=report.report_id, payload_digest=payload_digest,
                received_at=context.received_at, status=_receipt.INGEST_REJECTED,
                error_code="payload_too_large",
            ),
            rejected=[(-1, (
                f"payload 约 {approx_bytes} 字节，超过上限 {max_payload_bytes}",
            ))],
        )

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

    # 🔴 【整批一个事务】。认领、全部观测、最终回执必须一起成功或一起不生效。
    #
    # 之前是"先认领、再逐条各自提交"：第 1 条提交后崩溃，重试会直接拿到
    # duplicate，剩下的观测【永久丢失】——批级幂等把一次中断伪装成了"已处理完"。
    # 代价是单个事务变长，所以 max_observations 是必须的,不是可选的。
    with storage.transaction():
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


def _repeats_declared_state(
    item: NormalizedObservation,
    sig: SignalDefinition,
    *,
    context: IngestContext,
    storage: StoragePort,
) -> bool:
    """这条观测是不是「和当前值同一个状态」的重复上报。

    只看声明了 ``comparison_strategy="state_change"`` 的字段。**这个声明以前
    在 manifest 里写着、却没有任何代码读它** —— 于是 focus / motion /
    time_context 上那句「只在变化时追加」在文档里成立、在数据里不成立。

    保守判定：只有当前值确实存在、且**每一个**声明了 state_change 的字段都和
    当前值一样时，才算重复。任何一个字段变了、或者当前值还不存在（第一条）、
    或者这条不是 observed，都照常写明细 —— 宁可多写一条，不可漏掉一次真正的
    状态变化。
    """
    if item.stored.availability != "observed":
        return False
    watched = [f.key for f in sig.fields if f.comparison_strategy == "state_change"]
    if not watched:
        return False
    value = item.stored.typed_value or {}
    for projection in storage.get_current(
        subject_id=context.subject_id, signals=[sig.key],
    ).get(sig.key, ()):
        if (projection.dimension_key != sig.dimension_key_for(value)
                or projection.availability != "observed"):
            continue
        current = projection.typed_value or {}
        return all(current.get(k) == value.get(k) for k in watched)
    return False


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

    # ③ 观测级幂等。问的是【投递身份】不是【事实身份】——用事实身份去重会把
    #    "同一条事实的新版本"(电量的新读数、样本的修订)误判成重传丢掉。
    #    也不是问"这条观测还在不在"：明细可能已经按保留期删掉了。
    if storage.has_seen_identity(
        subject_id=context.subject_id, signal=stored.signal,
        source=stored.source, digest=item.identity_digest,
    ):
        outcome.duplicates.append(item)
        return

    # ④ 写观测。只留当前值的信号不写明细 —— 否则 current_only 名不副实。
    #
    #    声明了 `state_change` 的字段还有一条：状态没变就**不追加明细**，只刷新
    #    当前值。iOS 每 5 分钟保活上报一次，「还在专注」「还在静止」会一天写出
    #    几百条一模一样的记录 —— 时间线本该记的是「什么时候变了」，被同一个
    #    状态刷满之后，「每日切换次数」「最长一段」这类聚合直接失去意义。
    #
    #    ⚠️ 只跳过明细，**当前值和聚合照常走**：`duration_by_state` 靠相邻两条
    #    观测的时间差累计时长，跳过聚合会把时长永远停在第一次。
    if sig.stores_history and not _repeats_declared_state(
        item, sig, context=context, storage=storage,
    ):
        if not storage.append_observation(stored):
            outcome.duplicates.append(item)
            return

    # ⑤ 记住身份。并发下可能有另一个事务刚记过同一条 —— 那说明它赢了，
    #    我们退出，避免两边都往聚合里加一遍。
    if not storage.remember_identity(DurableDedupeIdentity(
        subject_id=context.subject_id,
        signal=stored.signal,
        source=stored.source,
        source_event_identity_digest=item.identity_digest,
        first_applied_at=context.received_at,
        aggregate_scope=sig.key if sig.keeps_history_forever else None,
        # 永久聚合依赖的身份必须永久保留：明细删了之后，
        # 它是唯一还能挡住重放的东西。
        retain_until=None,
    )):
        outcome.duplicates.append(item)
        return

    # ⑥ 当前值。
    #
    #    `observed`               更新数值
    #    `no_data` / `unavailable` **不覆盖最后一次可靠值**，但要把状态记下来
    #
    #    后半句以前没做，后果很具体：用户 09:10 撤销了步数权限，09:20 去查
    #    还在说「fresh，8000 步」—— 把一个已经读不到的值当成当前事实报出去。
    #    规范 §12-12 要的是两件事：不覆盖最后可靠数值，**并且**查询时能表达
    #    当前不可用。
    decision = _update_current(item, sig, context=context, storage=storage,
                               outcome=outcome)

    # 🔴 冲突时暂停一切派生。"到底哪份数据生效了"都说不清的时候，
    #    再去更新聚合、推进规则状态、产生事件，只会把错误扩散。
    if decision == CONFLICT:
        outcome.applied.append(item)
        return

    # ⑦ 聚合。迟到的旧数据(IGNORE)【要】进历史 —— 它只是不该改当前值。
    if sig.stores_history and stored.availability == "observed":
        _update_aggregate(item, sig, context=context, storage=storage)

    # ⑧⑨ 求值 + 写发件箱。和上面同事务 —— 事件落地了但观测没落地(或反过来)，
    #    都会让"为什么会有这个事件"永远解释不清。
    #
    #    只在 observed 且当前值真的推进了(REPLACE)时才跑值变化型规则：
    #    拿 no_data 去喂 `changed`，会把"100 → 没数据"当成一次变化，
    #    还会把 previous 推成 None，之后的 threshold_crossing 全废。
    #    迟到数据(IGNORE)同理 —— 它的 previous/current 讲的不是当前故事。
    if definitions and decision == REPLACE and stored.availability == "observed":
        rules = evaluate_and_enqueue(
            item, context=context, storage=storage,
            definitions=definitions, extra_evaluators=extra_evaluators,
            signal_definition=sig,
        )
        outcome.events.extend(rules.events)
        outcome.rule_misses.extend(rules.misses)
    elif definitions:
        outcome.rule_misses.append(
            ("*", f"未求值：availability={stored.availability}，当前值决策={decision}")
        )

    outcome.applied.append(item)


#: compare-and-put 失败后重读重判几次。并发下"读到旧版本 -> 写失败"是正常的，
#: 但不能无限重试 —— 那会在热点上把一个事务拖到超时。
MAX_CAS_RETRIES = 3


def _update_current(
    item: NormalizedObservation,
    sig: SignalDefinition,
    *,
    context: IngestContext,
    storage: StoragePort,
    outcome: IngestOutcome,
) -> str:
    """更新当前值，返回决策（``REPLACE`` / ``IGNORE`` / ``CONFLICT``）。

    **compare-and-put 的返回值必须认。** 忽略它的话，两个并发事务都读到旧版本、
    较新的那个 CAS 失败被静默丢掉 —— 当前值停在旧数据上，没有任何地方报错。
    """
    from datetime import timedelta

    from ..contracts.records import decide_current_update

    stored = item.stored
    dimension = sig.dimension_key_for(stored.typed_value)

    for attempt in range(MAX_CAS_RETRIES):
        existing = None
        for candidate in storage.get_current(
            subject_id=context.subject_id, signals=[stored.signal],
        ).get(stored.signal, ()):
            if candidate.dimension_key == dimension:
                existing = candidate
                break

        decision = decide_current_update(
            new_occurred_at=stored.occurred_at,
            new_revision=stored.source_revision,
            new_digest=item.content_digest,
            existing=existing,
        )
        if decision == IGNORE:
            return IGNORE
        if decision == CONFLICT:
            # 不静默挑一个。"到底哪份数据生效了"说不清，比多一条冲突记录糟得多。
            outcome.conflicts.append(item)
            return CONFLICT

        expires = (
            stored.occurred_at + timedelta(seconds=sig.current_ttl_sec)
            if sig.current_ttl_sec > 0 else None
        )
        if storage.compare_and_put_current(
            CurrentProjection(
                subject_id=context.subject_id,
                signal=stored.signal,
                dimension_key=dimension,
                # 非 observed 时保留上一次的可靠值 —— 它变成 last_known。
                # 写 None 进去等于把"我们曾经知道什么"一起抹掉，
                # agent 就只能说"你没有步数数据"，而不是
                # "你上次是 8000 步，现在读不到了"。
                typed_value=(stored.typed_value if stored.availability == "observed"
                             else (existing.typed_value if existing else None)),
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
        ):
            return REPLACE
        # 有人在我们读之后写了。重读、重新判断 —— 说不定这次该 IGNORE 了。

    outcome.warnings.append(
        f"{stored.signal}: 当前值连续 {MAX_CAS_RETRIES} 次写入竞争失败，本次放弃"
    )
    return IGNORE


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
    # 按当前算法版本挑。算法升级后旧版本的文档要留着(供对照/回滚),
    # 但绝不能拿旧口径的文档继续 fold 新数据 —— 那会得到一份两种口径混合的统计,
    # 而且看不出来。
    existing = [
        a for a in storage.get_aggregate(
            subject_id=context.subject_id, signal=stored.signal,
            start_date=day, end_date=day, aggregation_kind=kind,
        )
        if a.aggregation_version == AGGREGATION_VERSION
    ]
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
