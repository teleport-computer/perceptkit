"""后六步 —— 求值、落地、投递、回执。

    ⑧ 读定义和规则状态，求值
    ⑨ 命中时**原子地**写规则状态 + 写待发件箱
    ⑩ 提交事务          ← ``ingest()`` 到这里就返回了
    ⑪ 调 WakePort       ← 以下由宿主的 worker 驱动
    ⑫ 存回执
    ⑬ accepted 之后才把冷却额度占位兑现成真正的消耗

**第 ⑩ 步是分界线。** 上报接口同步做到"事件已落地并提交"就返回：走到这里
事件就丢不了了，后台慢慢投、崩了能重投，而手机不用等 runtime 的响应
—— runtime 一慢，上报接口就超时，客户端重传，雪上加霜。

**事件 id 是算出来的，不是随机的。** 由 ``(subject, 定义, 范围, 触发依据)``
确定性地导出 —— 同一次触发无论重算多少遍都是同一个 id，runtime 靠它幂等。
用随机 id 的话，一次重放就是一个新事件，用户被提醒两次。
（另外：这个包不读时钟也不生成随机数，那会让重放和测试都做不了。）

**"触发依据"是【哪一条观测】触发的，不是【从什么变成什么】。**
这两件事要分清：

    同一次投递重放   同一条观测 → 同一个 id → 发件箱挡下         ← 要的
    同一个跳变再发生 另一条观测 → 另一个 id → 又是一件事         ← 也要的

0.6.0 及以前值变化型规则用的是 ``"旧值->新值"`` 这段文字，把第二种也当成了
第一种：第二天"家 -> 公司"和第一天算出同一个 id，被 ``ON CONFLICT DO NOTHING``
静默丢掉，而引擎明明说了 fired。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..contracts import delivery as _delivery
from ..contracts.context import IngestContext
from ..contracts.event import EventCondition, PerceptionEvent, safe_context
from ..contracts.records import EventOutboxEntry
from ..contracts.receipt import WakeReceipt
from ..ports.storage import StoragePort
from ..ports.wake import WakePort
from ..rules.engine import evaluate, precondition_met, scope_key
from ..rules.types import EventDefinition, RuleResult, RuleState
from .normalize import NormalizedObservation, _digest

#: 一次投递最多重试几次。用尽进死信 —— 无限重试会让一个投不出去的事件
#: 永远占着 worker。
MAX_ATTEMPTS = 5

#: 重试的退避基数（秒）。第 n 次重试等 ``BACKOFF_BASE * 2**(n-1)`` 秒。
BACKOFF_BASE = 30.0


def event_id_for(
    *, subject_id: str, definition: EventDefinition, scope: str, trigger: str,
) -> str:
    """确定性的事件 id。见模块开头 —— 随机 id 会让重放变成新事件。"""
    return "evt_" + _digest(
        subject_id, definition.definition_id, str(definition.version), scope, trigger,
    )[:32]


def _trigger_for(
    definition: EventDefinition, item: NormalizedObservation, ctx: Mapping[str, Any],
) -> str:
    """事件 id 里的"触发依据"：**是哪一条观测让这条规则触发的**。

        occurrence   上游事件 id（没有时退回投递身份）—— 和以前逐字节相同，
                     它从来就是按观测算的，没有这个缺陷
        其余全部     这条观测的投递身份 ``identity_digest``

    投递身份正是第 ③ 步拿来挡重传的那个值（事实 + 时刻 + 版本 + 内容），
    所以两条性质是同一件事的两面：

        客户端重传同一份上报   投递身份相同 → 在 ③ 就被挡下；就算走到这里
                               （非原子 adapter、崩溃重放）也是同一个 id
        真的又发生了一次       另一条观测 → 另一个投递身份 → 另一个 id

    **不再把** ``"旧值->新值"`` **拼进去**：一条观测对一条规则在一个范围里
    最多就是一件事。把前值也算进身份的话，同一条观测在并发重试里读到不同
    的前值，就会变成两个事件。

    时钟驱动的 streak / absence 拿到的是调度器造的载体，它的投递身份是
    ``scheduled:<signal>:<日期>`` —— 同一天重跑是同一个 id，换一天是新的。
    """
    if definition.condition_type == "occurrence":
        return str(ctx["source_event_id"])
    return item.identity_digest


@dataclass
class RuleOutcome:
    """一条观测跑完所有相关规则的结果。"""

    events: list[PerceptionEvent] = field(default_factory=list)
    #: 求值了但没触发的，带原因。排查"为什么没提醒我"时要用。
    misses: list[tuple[str, str | None]] = field(default_factory=list)


def definitions_for_signal(
    definitions: Sequence[EventDefinition], *, signal: str, subject_id: str,
) -> list[EventDefinition]:
    """挑出和这个信号、这个用户相关的规则。

    **按信号索引，不是每次全量遍历。** 前台每 30 秒一次上报，用户配了几十条
    规则的话，全量求值会稳定占住 CPU。
    """
    return [
        d for d in definitions
        if d.enabled and d.signal == signal
        and (d.subject_id is None or d.subject_id == subject_id)
    ]



def _unit_of(sig: Any, field_name: str | None) -> str | None:
    """触发字段声明的单位。无量纲的（布尔、枚举）返回 ``None``。"""
    if sig is None or not field_name:
        return None
    fd = sig.field_map().get(field_name)
    return fd.unit if fd is not None else None


def evaluate_and_enqueue(
    item: NormalizedObservation,
    *,
    context: IngestContext,
    storage: StoragePort,
    definitions: Sequence[EventDefinition],
    extra_evaluators: Mapping[str, Callable[..., RuleResult]] | None = None,
    extra_context: Mapping[str, Any] | None = None,
    signal_definition: Any = None,
) -> RuleOutcome:
    """⑧⑨：对一条已经落地的观测求值，命中就写发件箱。

    **规则状态和发件箱必须同事务。** 分开写的话会出现"状态说已经触发过了、
    但事件没进发件箱" —— 这次触发就永远丢了，而且规则要等到下一个范围
    才会重新武装。
    """
    outcome = RuleOutcome()
    stored = item.stored
    relevant = definitions_for_signal(
        definitions, signal=stored.signal, subject_id=context.subject_id
    )
    if not relevant:
        return outcome

    values = stored.typed_value or {}
    for definition in relevant:
        # 前置条件不满足：这条观测对这条规则【不存在】。
        # 🔴 必须在读写规则状态【之前】跳过 —— 下面那句"状态每次都要写回"
        #    会把前值推进成这条观测的值，一条迟到的"家里断开"就把之后真正
        #    的"到家"吞成了"没变"。
        met, why_not = precondition_met(definition, values)
        if not met:
            outcome.misses.append((definition.definition_id, why_not))
            continue

        # 状态键带上定义版本：当天把阈值规则从 v1 改成 v2,v1 的
        # "今天已经触发过"不该继续压制 v2 —— 用户改了规则却不生效,
        # 而且没有任何地方报错。
        scope = "%s@v%d" % (
            scope_key(definition, local_date=stored.effective_local_date),
            definition.version,
        )
        raw_state = storage.get_rule_state(
            subject_id=context.subject_id,
            definition_id=definition.definition_id,
            scope_key=scope,
        )
        state = RuleState.from_dict(raw_state)

        current = values.get(definition.field_name) if definition.field_name else None
        ctx: dict[str, Any] = {
            "source_event_id": stored.source_event_id or item.identity_digest,
            "signal": stored.signal,
            "occurred_at": stored.occurred_at,
        }
        ctx.update(extra_context or {})

        result = evaluate(
            definition, state, current,
            now=stored.received_at,
            context=ctx,
            extra_evaluators=extra_evaluators,
        )

        # 状态**每次都要写回**，哪怕没触发 —— 不推进 previous_value 的话，
        # threshold_crossing 永远拿不到正确的前值，规则就成了死的。
        storage.put_rule_state(
            subject_id=context.subject_id,
            definition_id=definition.definition_id,
            scope_key=scope,
            state=result.state.to_dict(),
        )

        if not result.fired:
            outcome.misses.append((definition.definition_id, result.reason))
            continue

        trigger = _trigger_for(definition, item, ctx)
        event = PerceptionEvent(
            event_id=event_id_for(
                subject_id=context.subject_id, definition=definition,
                scope=scope, trigger=str(trigger),
            ),
            definition_id=definition.definition_id,
            definition_version=definition.version,
            subject_id=context.subject_id,
            type=definition.event_type,
            signal=stored.signal,
            occurred_at=stored.occurred_at,
            received_at=stored.received_at,
            condition=EventCondition(
                type=definition.condition_type,
                operator=definition.operator,
                value=definition.value,
            ),
            field_name=definition.field_name,
            previous=result.previous,
            current=result.current,
            # 受控的附加事实。**不透传整个存储 doc** —— 那既撑爆上下文也漏隐私。
            # reason 来自 evaluator，而宿主可以注册自己的 evaluator，所以这里
            # 不能直接信它 —— 一律过 safe_context。
            context=safe_context({
                "scope": scope,
                "reason": result.reason,
                # 带上单位。数字离开 manifest 之后就没别的地方能说清
                # 它是步数、毫升还是分钟了。
                "unit": _unit_of(signal_definition, definition.field_name),
            }),
        )

        # 🔴 事件【一律落地】。wake_enabled 只决定进不进可投递状态,
        #    不决定这个事实存不存 —— 之前不唤醒的事件只放进内存返回值,
        #    进程一崩就永久丢失,而规则状态已经推进、不会再产生它。
        entry = EventOutboxEntry(
            event_id=event.event_id,
            subject_id=context.subject_id,
            definition_id=definition.definition_id,
            definition_version=definition.version,
            event_type=definition.event_type,
            occurred_at=stored.occurred_at,
            detected_at=stored.received_at,
            fact_snapshot=event.to_dict(),
            dedupe_key=event.event_id,
            created_at=stored.received_at,
            delivery_state=(_delivery.PENDING if definition.wake_enabled
                            else _delivery.NOT_DISPATCHED),
        )
        if storage.enqueue_event(entry):
            outcome.events.append(event)
        else:
            # 同一个 event_id 已经在发件箱里 —— 这不是错误，是幂等生效了。
            outcome.misses.append((definition.definition_id, "事件已在发件箱中"))

    return outcome


# ---------------------------------------------------------------------------
# 投递（由宿主的 worker 驱动）
# ---------------------------------------------------------------------------

@dataclass
class DispatchOutcome:
    delivered: list[str] = field(default_factory=list)
    retrying: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _backoff(attempt: int) -> timedelta:
    return timedelta(seconds=BACKOFF_BASE * (2 ** max(0, attempt - 1)))


#: 快照里没有 condition 时用的占位。**不是空字符串** —— 空的 type 会一路传到
#: 宿主那里，看起来像「这条规则没有条件」，而实情是「我们弄丢了它」。
UNKNOWN_CONDITION_TYPE = "unknown"


def _condition_from(raw: Any) -> EventCondition:
    """从发件箱快照重建判据。**残缺就用占位，不抛异常。**"""
    fields = {k: v for k, v in (raw or {}).items()
              if k in ("type", "operator", "value")}
    if not fields.get("type"):
        fields["type"] = UNKNOWN_CONDITION_TYPE
    return EventCondition(**fields)


def dispatch_once(
    *,
    storage: StoragePort,
    wake: WakePort,
    worker_id: str,
    now: datetime,
    lease_seconds: float = 60.0,
    max_attempts: int = MAX_ATTEMPTS,
) -> DispatchOutcome | None:
    """领一个待投递事件、投出去、存回执。没有可领的返回 ``None``。

    ``WakePort.wake`` 抛异常时按 ``enqueue_failed`` 处理 —— **不能当成功**。
    "结果未知"和"失败"要走同一条路（重试 + 靠 ``event_id`` 幂等兜底），
    因为盲目当成功会让事件永远送不到。
    """
    entry = storage.claim_pending_event(
        worker_id=worker_id, now=now, lease_seconds=lease_seconds
    )
    if entry is None:
        return None

    outcome = DispatchOutcome()
    event = PerceptionEvent(
        event_id=entry.event_id,
        definition_id=entry.definition_id,
        definition_version=entry.definition_version,
        subject_id=entry.subject_id,
        type=entry.event_type,
        signal=entry.fact_snapshot.get("signal", ""),
        occurred_at=entry.occurred_at,
        received_at=entry.detected_at,
        # 快照里没有 condition 的话，用一个明确的占位而不是让构造炸掉。
        # 发件箱里一条残缺记录（老版本写的、写了一半的）**不能停掉所有人的
        # 投递** —— 而它会：drain 是一个循环，一条抛异常整轮就结束，
        # 后面排队的事件谁也送不出去，还没有任何地方说为什么。
        condition=_condition_from(entry.fact_snapshot.get("condition")),
        field_name=entry.fact_snapshot.get("field"),
        previous=entry.fact_snapshot.get("previous"),
        current=entry.fact_snapshot.get("current"),
        context=safe_context(entry.fact_snapshot.get("context")),
    )
    attempt = _delivery.DeliveryAttempt(
        event_id=entry.event_id,
        attempt_id=f"{entry.event_id}:{entry.attempt_count}",
        attempt_number=max(1, entry.attempt_count),
    )

    try:
        receipt = wake.wake(event, attempt)
    except Exception as exc:                       # noqa: BLE001 —— 见 docstring
        receipt = WakeReceipt(
            event_id=entry.event_id, attempt_id=attempt.attempt_id,
            status="enqueue_failed", received_at=now,
            reason=f"{type(exc).__name__}: {exc}",
        )

    attempts_left = entry.attempt_count < max_attempts
    next_state = _delivery.next_state_for_receipt(
        receipt.status, attempts_left=attempts_left
    )
    next_at = (
        now + _backoff(entry.attempt_count)
        if next_state == _delivery.PENDING else None
    )
    accepted = storage.record_wake_receipt(
        receipt=receipt, next_state=next_state,
        claim_token=entry.claim_token, next_attempt_at=next_at,
    )
    if accepted is False:
        # 令牌过期:这个事件在我们投递期间被别人接管了。回执只进审计,
        # 状态归新 owner 管 —— 我们这一次的结果不算数。
        outcome.retrying.append(entry.event_id)
        return outcome

    bucket = {
        _delivery.DELIVERED: outcome.delivered,
        _delivery.PENDING: outcome.retrying,
        _delivery.DEAD_LETTER: outcome.dead,
        _delivery.SUPPRESSED: outcome.suppressed,
        _delivery.REJECTED: outcome.rejected,
    }[next_state]
    bucket.append(entry.event_id)
    return outcome


def drain(
    *, storage: StoragePort, wake: WakePort, worker_id: str, now: datetime,
    limit: int = 100, lease_seconds: float = 60.0,
) -> DispatchOutcome:
    """把当前能投的都投一遍。给宿主的 worker 循环用。

    ``limit`` 不是可选的：不设上限的话，积压很多时这一轮会跑很久，
    而租约是有到期时间的 —— 跑太久会让前面已经领的事件被别人接管。
    """
    total = DispatchOutcome()
    for _ in range(limit):
        one = dispatch_once(
            storage=storage, wake=wake, worker_id=worker_id,
            now=now, lease_seconds=lease_seconds,
        )
        if one is None:
            break
        for name in ("delivered", "retrying", "dead", "suppressed", "rejected"):
            getattr(total, name).extend(getattr(one, name))
    return total


__all__ = [
    "MAX_ATTEMPTS", "BACKOFF_BASE", "UNKNOWN_CONDITION_TYPE", "event_id_for", "definitions_for_signal",
    "evaluate_and_enqueue", "RuleOutcome", "DispatchOutcome",
    "dispatch_once", "drain",
]
