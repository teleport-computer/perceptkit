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
from typing import Any, Mapping

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


#: 事件 ``context`` 允许出现的键。**白名单，不是建议** —— 事件会被存下来、
#: 投出去、进模型上下文，任何"顺手多带一点"都会同时撑上下文和漏隐私。
#: 宿主要更多信息，应该拿 ``signal`` + ``occurred_at`` 自己去查，而不是让
#: kit 把存储 doc 塞进信封。
ALLOWED_CONTEXT_KEYS = (
    "scope",
    # 触发字段的单位。产品规范 §13 的信封示例里就带着它，而且它是必要的：
    # 一个只说 "current: 3012" 的事件，读到的人（和模型）分不出这是步数、
    # 毫升还是分钟 —— manifest 花那么大力气要求数值必须有单位，
    # 到了事件这一层丢掉，等于前功尽弃。
    "unit",
    "reason",
    "streak_length",
    "silent_seconds",
)

#: 单个 context 值序列化后的字符数上限。主要防的是 ``reason`` ——
#: 它来自 evaluator，而宿主可以注册自己的 evaluator，返回什么完全不受我们控制。
MAX_CONTEXT_VALUE_CHARS = 200


def safe_context(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把任意来源的 context 收敛成信封允许的形状。

    白名单外的键**丢掉**（不是报错：一条规则多带了个字段，不该让整个事件消失）；
    超长的值截断并留一个显式的省略号，让读到的人知道这里被截过。
    """
    out: dict[str, Any] = {}
    for key in ALLOWED_CONTEXT_KEYS:
        if raw is None or key not in raw:
            continue
        value = raw[key]
        # 值是 None 的键**整个省掉**，不写成 `"unit": null`。
        # 布尔和枚举本来就没有单位，给它编一个空位比不给更糟 ——
        # 读到的人会以为"这里本该有个单位，只是没填"。
        # 和信封里 condition 省略 operator 是同一条约定。
        if value is None:
            continue
        if isinstance(value, str) and len(value) > MAX_CONTEXT_VALUE_CHARS:
            value = value[:MAX_CONTEXT_VALUE_CHARS] + "…(截断)"
        out[key] = value
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
    #: 受控的附加事实。键限定在 ``ALLOWED_CONTEXT_KEYS``、值有长度上限,
    #: 都由 ``safe_context`` 收敛 —— 不许把整个存储 doc 透传进来,
    #: 那既撑爆上下文,也漏隐私。
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


__all__ = ["ALLOWED_CONTEXT_KEYS", "MAX_CONTEXT_VALUE_CHARS",
           "EventCondition", "PerceptionEvent", "safe_context"]
