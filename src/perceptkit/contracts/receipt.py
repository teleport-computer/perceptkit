"""两种回执 —— 上报进来的回执,和事件投出去的回执。

``IngestReceipt`` 让同一批上报重传时能返回相同结果,不重复处理。
``WakeReceipt`` 让"到底投没投成"变成一个可持久化、可查询的事实,
而不是一个只存在于某次函数调用里的瞬时值。

**为什么 wake 的状态不能只有成功/失败两种**:runtime 拒绝一次唤醒有好几种
完全不同的原因,处理方式也不同 —— 会话里正忙(等下次就好)和入队失败
(得重试)混成一个"失败",要么该重试的没重试,要么不该重试的一直重试。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from ._time import parse_timestamp
from .errors import ContractError

# ---------------------------------------------------------------------------
# 上报回执
# ---------------------------------------------------------------------------

#: 这批上报第一次被处理。
INGEST_ACCEPTED = "accepted"
#: 同一个 (subject, producer, report_id) + 相同内容 —— 返回原结果,不重复处理。
INGEST_DUPLICATE = "duplicate"
#: 同一个 identity 但内容不同。**必须报冲突,不能静默覆盖** ——
#: 静默覆盖会让"到底哪份数据生效了"永远说不清。
INGEST_CONFLICT = "conflict"
#: 校验没过。``error_code`` 说明原因。
INGEST_REJECTED = "rejected"

INGEST_STATUSES: frozenset[str] = frozenset({
    INGEST_ACCEPTED, INGEST_DUPLICATE, INGEST_CONFLICT, INGEST_REJECTED,
})


_QUOTED_LITERAL = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
_NUMERIC_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![A-Za-z0-9_])"
)


def sanitize_observation_problem(problem: object) -> str:
    """Keep a bounded diagnostic, never the rejected payload value itself."""
    text = " ".join(str(problem).split())
    text = _QUOTED_LITERAL.sub("<redacted>", text)
    text = _NUMERIC_LITERAL.sub("<redacted>", text)
    return (text[:253] + "...") if len(text) > 256 else text


@dataclass(frozen=True)
class ObservationRejection:
    """Durable, replayable outcome for one Report array entry.

    Only the request index and bounded/redacted diagnostics are persisted. Raw
    Observation, normalized values, source values, and restricted fields never
    become part of the Report receipt.
    """

    index: int
    problems: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ContractError(["observation rejection index must be a non-negative integer"])
        sanitized = tuple(sanitize_observation_problem(problem) for problem in self.problems)
        if not sanitized or any(not problem for problem in sanitized):
            raise ContractError(["observation rejection problems must not be empty"])
        object.__setattr__(self, "problems", sanitized)


@dataclass(frozen=True)
class IngestReceipt:
    """一批上报的处理结果。唯一身份是 ``(subject_id, producer, report_id)``。"""

    subject_id: str
    producer: str
    report_id: str
    #: 信封内容的摘要。同 identity 同摘要 → duplicate;同 identity 不同摘要 → conflict。
    payload_digest: str
    received_at: datetime
    status: str
    error_code: str | None = None
    #: 这批里有几条观测被真正处理了(duplicate 时为 0)。
    observations_applied: int = 0
    #: Durable per-item failures. Whole-batch rejection uses status/error_code.
    observations_rejected: tuple[ObservationRejection, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in INGEST_STATUSES:
            raise ContractError(
                [f"status: {self.status!r} is not one of {sorted(INGEST_STATUSES)}"]
            )
        parse_timestamp(self.received_at, field="received_at")
        if type(self.observations_applied) is not int or self.observations_applied < 0:
            raise ContractError(["observations_applied must be a non-negative integer"])
        rejected = tuple(self.observations_rejected)
        if any(not isinstance(item, ObservationRejection) for item in rejected):
            raise ContractError(["observations_rejected must contain ObservationRejection"])
        if tuple(sorted(item.index for item in rejected)) != tuple(item.index for item in rejected):
            raise ContractError(["observations_rejected must be ordered by request index"])
        if len({item.index for item in rejected}) != len(rejected):
            raise ContractError(["observations_rejected must contain one entry per request index"])
        object.__setattr__(self, "observations_rejected", rejected)

    @property
    def retryable(self) -> bool:
        """Returned receipts are committed terminal decisions, never retry hints.

        Transient transaction/CAS failures raise and roll back instead of
        returning a receipt. Repeating a failed immutable Report cannot fix it.
        """
        return False

    @property
    def recovery_action(self) -> str | None:
        """Machine-readable recovery contract for producer/host adapters."""
        if self.error_code == "legacy_report_semantics_unverifiable":
            return "migrate_original_envelope_or_use_new_report_id"
        if self.error_code == "fact_revision_details_incomplete":
            return "restore_fact_evidence_and_use_new_report_id"
        if self.error_code == "fact_conflict":
            return "resolve_fact_conflict_and_use_new_report_id"
        problems = {problem for item in self.observations_rejected for problem in item.problems}
        joined = " ".join(problems)
        if "fact_revision_details_incomplete" in joined:
            return "restore_fact_evidence_and_use_new_report_id"
        if "fact_conflict" in problems:
            return "resolve_fact_conflict_and_use_new_report_id"
        if self.status in (INGEST_REJECTED, INGEST_CONFLICT):
            return "correct_payload_and_use_new_report_id"
        return None


# ---------------------------------------------------------------------------
# 唤醒回执
# ---------------------------------------------------------------------------

#: runtime 收下了这次唤醒。**只有这个状态才允许正式扣冷却/额度。**
WAKE_ACCEPTED = "accepted"
#: runtime 认得这个 ``event_id``,之前已经处理过。重试撞上它是正常的,不是错误。
WAKE_DUPLICATE = "duplicate"
#: runtime 现在不想被打扰(会话正忙、安静时段…)。**这不是失败** ——
#: 事件已经送到了,是 runtime 自己决定不响应。不该重试。
WAKE_SUPPRESSED = "conversation_suppressed"
#: 投递本身失败了(队列写不进、连接断)。**该重试。**
WAKE_ENQUEUE_FAILED = "enqueue_failed"
#: runtime 明确拒绝(事件类型不认、subject 不属于它…)。不该重试。
WAKE_REJECTED = "rejected"

WAKE_STATUSES: frozenset[str] = frozenset({
    WAKE_ACCEPTED, WAKE_DUPLICATE, WAKE_SUPPRESSED,
    WAKE_ENQUEUE_FAILED, WAKE_REJECTED,
})

#: 这几种状态该重试。其余的是终态。
#: 注意 ``conversation_suppressed`` **不在**里面 —— 事件已经送达,
#: 是 runtime 自己选择不响应,重投只会打扰第二次。
WAKE_RETRYABLE: frozenset[str] = frozenset({WAKE_ENQUEUE_FAILED})


@dataclass(frozen=True)
class WakeReceipt:
    """runtime 对一次投递的应答。"""

    event_id: str
    #: 这是第几次尝试。``event_id`` 跨重试**保持不变**(runtime 靠它幂等),
    #: ``attempt_id`` 每次都变(用来对上具体是哪一次投递的回执)。
    attempt_id: str
    status: str
    received_at: datetime
    #: 宿主自己的引用(job id、任务号…)。kit 不解释它的内容,原样存。
    runtime_ref: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in WAKE_STATUSES:
            raise ContractError(
                [f"status: {self.status!r} is not one of {sorted(WAKE_STATUSES)}"]
            )
        parse_timestamp(self.received_at, field="received_at")

    @property
    def should_retry(self) -> bool:
        return self.status in WAKE_RETRYABLE

    @property
    def consumes_budget(self) -> bool:
        """这次投递该不该正式扣冷却/额度。

        **只有 accepted 算。** 被压制、入队失败、被拒绝都不该扣 —— 用户
        那一轮该说的话没说出去,额度却被吃掉了,是最难查的一类问题。
        """
        return self.status == WAKE_ACCEPTED


__all__ = [
    "INGEST_ACCEPTED", "INGEST_DUPLICATE", "INGEST_CONFLICT", "INGEST_REJECTED",
    "INGEST_STATUSES", "ObservationRejection", "IngestReceipt",
    "WAKE_ACCEPTED", "WAKE_DUPLICATE", "WAKE_SUPPRESSED",
    "WAKE_ENQUEUE_FAILED", "WAKE_REJECTED", "WAKE_STATUSES", "WAKE_RETRYABLE",
    "WakeReceipt",
]
