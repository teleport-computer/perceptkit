"""感知事件 —— kit 交给宿主的**唯一**产物。

规则可以随便加、随便改,但这个信封必须稳。宿主接的是信封,不是规则 ——
信封稳定,用户新配十条规则,宿主一行代码都不用动。

事件只表达四件事:

    发生了什么          type / signal / field
    哪条规则命中        definition_id + definition_version
    新旧事实是什么      previous / current
    为什么符合条件      condition

事件**不**表达:agent 该说什么、要不要给用户发通知、用哪个模型、
走宿主的哪条 runtime。那些是宿主的决定 —— **戳醒不等于该开口**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import versioning
from ._time import to_iso


@dataclass(frozen=True)
class EventCondition:
    """命中时的判据快照 —— 让宿主(和事后排查的人)看得懂为什么触发。

    存快照而不是存规则的引用:规则会被改、被删,而事件是**已经发生的事实**,
    不该因为规则后来变了就解释不通。
    """

    type: str
    operator: str | None = None
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.operator is not None:
            out["operator"] = self.operator
        if self.value is not None:
            out["value"] = self.value
        return out


@dataclass(frozen=True)
class PerceptionEvent:
    """一次规则命中产生的不可变事实。"""

    event_id: str
    definition_id: str
    definition_version: int
    subject_id: str
    type: str
    signal: str
    occurred_at: datetime
    received_at: datetime
    condition: EventCondition
    #: 触发这条规则的字段。``occurrence`` 型规则(整条信号的到达本身就是事件)
    #: 没有具体字段,为 ``None``。
    field_name: str | None = None
    #: 变化前后的值。``occurrence`` 型两者都是 ``None``。
    previous: Any = None
    current: Any = None
    #: 受控的附加事实(scope、单位等)。**有大小和字段白名单**,
    #: 不许把整个存储 doc 透传进来 —— 那既撑爆上下文,也漏隐私。
    context: dict[str, Any] = field(default_factory=dict)
    schema_version: int = versioning.EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """序列化成宿主可以直接投递的形状。"""
        return {
            "event_id": self.event_id,
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "subject_id": self.subject_id,
            "type": self.type,
            "signal": self.signal,
            "field": self.field_name,
            "occurred_at": to_iso(self.occurred_at),
            "received_at": to_iso(self.received_at),
            "previous": self.previous,
            "current": self.current,
            "condition": self.condition.to_dict(),
            "context": dict(self.context),
            "schema_version": self.schema_version,
        }


__all__ = ["EventCondition", "PerceptionEvent"]
