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

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from ..contracts import receipt as _receipt
from ..contracts.context import IngestContext
from ..contracts.errors import RetryableProjectionError, LEGACY_REPORT_SEMANTICS_UNVERIFIABLE
from ..contracts.records import (
    CONFLICT,
    IGNORE,
    REPLACE,
    CurrentProjection,
    DailyAggregate,
    _compare_revisions,
)
from ..contracts.report import ReportEnvelope, canonical_semantics
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort
from .retract import is_retracted
from ..rules.types import EventDefinition
from . import aggregate as _aggregate
from .dispatch import evaluate_and_enqueue
from .normalize import NormalizedObservation, _canonical, normalize_observations
from .facts import decide_fact, durable_identity, detail_proves_revision

#: 聚合算法的版本。改了口径就加这个数并重算，**不原地改写旧统计的语义** ——
#: 否则同一张表里一半是老口径一半是新口径，而且看不出来。
#: 聚合文档的语义版本。**语义变了就必须升**，否则新旧口径的行会混在一起
#: 被读出来，而两边看起来都是合法的 JSON。
#:
#: 2 = 2026-09-07：睡眠改用 duration_sum_by_state。此前 health_sleep 的
#: 聚合文档里 minutes 恒为空、duration_minutes.total 是各阶段的 max 而非和。
AGGREGATION_VERSION = 2


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
    #: 来源早就撤回过这条事实，这次投递不作数（重传 / 乱序 / 补传）。
    #: 不是错误，也不是重复 —— 单独一栏，宿主排查"我删了怎么又回来了"时要用。
    retracted: list[NormalizedObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 这批上报产生的事件（已经落进发件箱，还没投）。
    events: list[Any] = field(default_factory=list)
    #: 求值了但没触发的规则，带原因。排查"为什么没提醒我"时要用。
    rule_misses: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.receipt.status in (_receipt.INGEST_ACCEPTED, _receipt.INGEST_DUPLICATE)
                and not self.rejected and not self.conflicts)


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
            if claim.status == _receipt.INGEST_CONFLICT and not claim.payload_digest.startswith("v2:"):
                claim = replace(claim, error_code=LEGACY_REPORT_SEMANTICS_UNVERIFIABLE)
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

        # A batch is immutable: array order must never choose the winner among
        # two different contents claiming the same Fact revision. Preflight all
        # candidates before any Observation/projection writes; unrelated siblings
        # retain their original processing order and independent outcome.
        blocked = set()
        fact_candidates: dict[str, list[NormalizedObservation]] = {}
        for item in normalized.normalized:
            if signals[item.stored.signal].identity_strategy != "source_event_id" or not item.stored.source_event_id:
                continue
            siblings = fact_candidates.setdefault(item.fact_key, [])
            if any(_compare_revisions(item.stored.source_revision, other.stored.source_revision) == 0
                   and item.semantic_digest != other.semantic_digest for other in siblings):
                blocked.add(item.fact_key)
            siblings.append(item)

        for item in normalized.normalized:
            if item.fact_key in blocked:
                outcome.conflicts.append(item)
                continue
            sig = signals[item.stored.signal]
            _apply_one(item, sig, context=context, storage=storage, outcome=outcome,
                       definitions=definitions, extra_evaluators=extra_evaluators)

        status, code = _receipt.INGEST_ACCEPTED, None
        if outcome.conflicts:
            status, code = _receipt.INGEST_CONFLICT, "fact_conflict"
        elif outcome.rejected:
            status, code = _receipt.INGEST_REJECTED, "observations_rejected"
            if any("fact_revision_details_incomplete" in str(reasons) for _, reasons in outcome.rejected):
                code = "fact_revision_details_incomplete"
        outcome.receipt = replace(claim, status=status, error_code=code,
                                  observations_applied=len(outcome.applied))
        storage.finalize_report(outcome.receipt)
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
    if item.stored.availability != "observed" or sig.current_policy == "none":
        return False
    # 🔴 来源给了这条事实自己的身份，它就是**一件独立的事**，不是保活重复。
    #
    #     这条规则防的是 iOS 每 5 分钟一次的保活上报（"还在专注""还在静止"），
    #     那些信号没有上游身份，只能靠"内容没变"认重复。而睡眠、经期这类
    #     HealthKit 样本每条都带着自己的 uuid 和起止时间 —— 同一晚两段 core
    #     是两条真实事实，判成重复就把那一晚的一半直接丢了，不报错。
    #     （外部审查 F1：30+45 的一晚只留下 30）
    if sig.identity_strategy == "source_event_id":
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

    # ②·5 来源撤回过的事实，不许被迟到的样本复活。
    #
    #     撤回先到、原样本后到是常态（重传、乱序、补传）。不查的话这条后到
    #     的样本会被当成一条新事实：当前值和日聚合又变回那个已经被用户在
    #     健康 app 里删掉的数 —— 删了，过一会儿自己回来了（外部审查 F2）。
    #
    #     放在幂等检查**之前**：这条根本不该进来，不是"进来过所以跳过"。
    if is_retracted(
        storage, subject_id=context.subject_id, signal=stored.signal,
        source=stored.source, source_event_id=stored.source_event_id,
    ):
        outcome.retracted.append(item)
        return

    # ③ 观测级幂等。问的是【投递身份】不是【事实身份】——用事实身份去重会把
    #    "同一条事实的新版本"(电量的新读数、样本的修订)误判成重传丢掉。
    #    也不是问"这条观测还在不在"：明细可能已经按保留期删掉了。
    # Source-identified facts are decided against durable revision evidence first.
    # Legacy identities are migrated from persisted rows, never this upload time.
    prior_revisions = []
    if sig.identity_strategy == "source_event_id" and stored.source_event_id:
        fact_decision, prior_revisions, reason = decide_fact(storage, item)
        if fact_decision == "conflict":
            outcome.conflicts.append(item)
            return
        if fact_decision == "duplicate":
            outcome.duplicates.append(item)
            return
        if reason:
            outcome.warnings.append(f"{stored.signal}: {reason}")
        if fact_decision in ("incomplete", "stale"):
            outcome.rejected.append((-1, (f"{stored.signal}: {reason}",)))
            return
        if prior_revisions and sig.stores_history:
            # Corrections require complete persisted evidence to subtract the
            # previous active revision. Expired details cannot be manufactured.
            from .retract import _all_observations, canonical_revisions, drop_retracted
            details = _all_observations(storage, stored.subject_id, stored.signal)
            if any(r.effective_local_date is None or not any(
                detail_proves_revision(detail, r, item) for detail in details
            ) for r in prior_revisions):
                outcome.rejected.append((-1, (f"{stored.signal}: fact_revision_details_incomplete",)))
                return
            active = drop_retracted(storage, canonical_revisions(details, sig),
                                    subject_id=stored.subject_id, signal=stored.signal)
            for day in {stored.effective_local_date, *(r.effective_local_date for r in prior_revisions)}:
                count = sum(r.availability == "observed" and r.effective_local_date == day for r in active)
                aggregates = storage.get_aggregate(subject_id=stored.subject_id, signal=stored.signal,
                                                   start_date=day, end_date=day)
                if any(a.aggregation_version == AGGREGATION_VERSION
                       and int(a.source_coverage.get("observations", 0)) > count for a in aggregates):
                    outcome.rejected.append((-1, (f"{stored.signal}: fact_revision_details_incomplete",)))
                    return
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
    if not storage.remember_identity(durable_identity(
        item, received_at=context.received_at,
        # ★ 问的是**聚合**永不永久，不是明细。
        #
        #   这条记录存在的全部理由就是「明细会过期、聚合可能永久」——
        #   所以拿明细的保留期来判断，恰好在它唯一有用的那些信号上判成 None：
        #   照片（明细 7 天 / 每日数量永久）、focus / motion / music
        #   （明细 1 年 / 聚合永久）。四个信号的去重身份可以先于聚合被清掉，
        #   之后一次重传就把永久聚合多加一遍，**加完没法回滚**。
        #   产品规范 §14-2 点名的正是这个场景。
        aggregate_scope=sig.key if sig.keeps_aggregates_forever else None,
        # 永久聚合依赖的身份必须永久保留：明细删了之后，
        # 它是唯一还能挡住重放的东西。
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
    decision = (_update_current(item, sig, context=context, storage=storage,
                                outcome=outcome, correction=bool(prior_revisions))
                if sig.current_policy == "latest" else None)

    # ⑦ 已接受事实独立参与聚合；Current 同时刻冲突不否定另一个 Fact。
    if sig.stores_history:
        if prior_revisions:
            for day in {stored.effective_local_date, *(r.effective_local_date for r in prior_revisions)}:
                _rebuild_corrected_day(storage, sig, context=context, day=day)
        elif stored.availability == "observed":
            _update_aggregate(item, sig, context=context, storage=storage)

    # ⑧⑨ 求值 + 写发件箱。和上面同事务 —— 事件落地了但观测没落地(或反过来)，
    #    都会让"为什么会有这个事件"永远解释不清。
    #
    #    值变化规则仍要求 Current 推进；occurrence 只依赖独立事实身份，
    #    不能因为迟到或 Current 冲突漏掉。无 Current 的信号只求值 occurrence，
    #    原始片段到达顺序不能充当 changed/threshold/delta 等规则的事实顺序。
    #    拿 no_data 去喂 `changed`，会把"100 → 没数据"当成一次变化，
    #    还会把 previous 推成 None，之后的 threshold_crossing 全废。
    #    迟到数据(IGNORE)同理 —— 它的 previous/current 讲的不是当前故事。
    eligible_definitions = [
        definition for definition in definitions
        if stored.availability == "observed" and (
            definition.condition_type == "occurrence"
            or decision == REPLACE
        )
    ]
    if eligible_definitions:
        rules = evaluate_and_enqueue(
            item, context=context, storage=storage,
            definitions=eligible_definitions, extra_evaluators=extra_evaluators,
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
    correction: bool = False,
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
        candidate_item = item
        stored = item.stored
        existing = None
        for candidate in storage.get_current(
            subject_id=context.subject_id, signals=[stored.signal],
        ).get(stored.signal, ()):
            if candidate.dimension_key == dimension:
                existing = candidate
                break

        owns_current = (correction and existing is not None and
                        (existing.source, existing.source_event_id) ==
                        (stored.source, stored.source_event_id))
        if owns_current and sig.stores_history and stored.availability == "observed":
            from .retract import _all_observations, canonical_revisions, drop_retracted
            pool = drop_retracted(storage, canonical_revisions(
                _all_observations(storage, stored.subject_id, stored.signal), sig),
                subject_id=stored.subject_id, signal=stored.signal)
            pool = [row for row in pool if row.availability == "observed"
                    and sig.dimension_key_for(row.typed_value) == dimension]
            if pool:
                stored = max(pool, key=lambda row: (row.occurred_at, row.observation_id))
                from .normalize import _digest
                candidate_item = replace(item, stored=stored,
                                         content_digest=_digest(_canonical(stored.typed_value), stored.availability))
        decision = REPLACE if owns_current else decide_current_update(
            new_occurred_at=stored.occurred_at,
            new_revision=stored.source_revision,
            new_digest=candidate_item.content_digest,
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
                # 源事实身份跟着当前值走 —— 撤回靠它定位，而 current_only
                # 的信号没有观测可以反查。
                source=stored.source,
                source_event_id=stored.source_event_id,
                source_revision=stored.source_revision,
                version=(existing.version + 1) if existing else 0,
                content_digest=candidate_item.content_digest,
            ),
            expected_version=existing.version if existing else -1,
        ):
            return REPLACE if stored.observation_id == item.stored.observation_id else IGNORE
        # 有人在我们读之后写了。重读、重新判断 —— 说不定这次该 IGNORE 了。

    raise RetryableProjectionError("current", stored.signal, MAX_CAS_RETRIES)


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
    # 计数器纪元由生产方在载荷里给（来源重置了计数就换一个）。它是 manifest
    # 声明的普通字段，这里只是把它当上下文取出来 —— 累计型靠它区分
    # 「重置」和「修订」，混了的话「重置到 0」会被当成错值吃掉。
    typed = stored.typed_value or {}
    epoch = typed.get("counter_epoch_id")
    for _ in range(MAX_CAS_RETRIES):
        existing = next((
            a for a in storage.get_aggregate(
                subject_id=context.subject_id, signal=stored.signal,
                start_date=day, end_date=day, aggregation_kind=kind,
            ) if a.aggregation_version == AGGREGATION_VERSION
        ), None)
        doc = _aggregate.fold_into_day(
            existing.typed_aggregate if existing else None, sig, typed,
            ts=_epoch(stored.occurred_at), revision=stored.source_revision,
            counter_epoch=str(epoch) if epoch is not None else None,
        )
        coverage = dict((existing.source_coverage if existing else {}) or {})
        coverage["observations"] = int(coverage.get("observations", 0)) + 1
        version = existing.version if existing else -1
        if storage.compare_and_put_aggregate(DailyAggregate(
            subject_id=context.subject_id,
            signal=stored.signal,
            local_date=day,
            aggregation_kind=kind,
            aggregation_version=AGGREGATION_VERSION,
            typed_aggregate=doc,
            timezone_attribution=stored.timezone,
            source_coverage=coverage,
            updated_at=stored.received_at,
            version=version + 1,
        ), expected_version=version):
            return
    raise RetryableProjectionError("aggregate", stored.signal, MAX_CAS_RETRIES)


def _batch_digest(report: ReportEnvelope) -> str:
    from .normalize import _digest
    return "v2:" + _digest(canonical_semantics(report.semantic_payload()))


def _rebuild_corrected_day(storage, sig, *, context, day):
    from .recompute import recompute_day
    for _ in range(MAX_CAS_RETRIES):
        old = next((row for row in storage.get_aggregate(
            subject_id=context.subject_id, signal=sig.key,
            start_date=day, end_date=day, aggregation_kind="daily",
        ) if row.aggregation_version == AGGREGATION_VERSION), None)
        expected = old.version if old else -1
        rebuilt = recompute_day(storage, sig, subject_id=context.subject_id,
                                day=day, version=AGGREGATION_VERSION,
                                updated_at=context.received_at)
        if storage.compare_and_put_aggregate(replace(rebuilt, version=expected + 1),
                                             expected_version=expected):
            return
    raise RetryableProjectionError("aggregate", sig.key, MAX_CAS_RETRIES)


__all__ = ["IngestOutcome", "ingest_report", "AGGREGATION_VERSION"]
