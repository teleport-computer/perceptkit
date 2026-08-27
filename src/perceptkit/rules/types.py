"""规则的形状 —— 定义、状态、求值结果。

**定义和发生是两件事，必须分开。**

    EventDefinition   用户或宿主说"什么条件算一件事"。会被改、被删、被停用。
    PerceptionEvent   某条规则命中后产生的**不可变事实**。规则后来变了，
                      已经发生的事实仍然解释得通 —— 所以事件里带条件快照，
                      不带指向定义的活引用。

**不做通用表达式 DSL。** 九种模板已经覆盖真实需求，而 DSL 意味着要解析、
要防注入、要考虑求值超时 —— 用户配的规则跑在服务端，那是一整类新的安全面。
需要更复杂逻辑的宿主可以注册代码型 evaluator，但那要求宿主自己信任那段代码。

**规则用 dict/JSON 表达，不是 YAML。** 这个包零依赖（``dependencies = []``，
有 AST 测试盯着），引 PyYAML 当场破坏"任何宿主都能直接嵌入"这句话。
宿主想用 YAML 写规则完全可以 —— 自己 ``yaml.safe_load`` 成 dict 再传进来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contracts.errors import ContractError

#: 规则在什么范围内计一次。``local_day`` 每天重置，``forever`` 永不重置。
SCOPES: frozenset[str] = frozenset({"local_day", "forever"})

#: 一个范围内触发几次。
FIRE_MODES: frozenset[str] = frozenset({"once", "every"})

#: 什么时候重新武装。
REARM_MODES: frozenset[str] = frozenset({"next_scope", "cooldown", "never"})


@dataclass(frozen=True)
class Lifecycle:
    """一条规则的触发节奏。

    默认是"每天一次、次日重新武装" —— 这是绝大多数感知规则想要的：
    今天走够 3000 步提醒一次，明天重新算。
    """

    scope: str = "local_day"
    fire: str = "once"
    rearm: str = "next_scope"
    cooldown_seconds: float = 0.0

    def __post_init__(self) -> None:
        problems = []
        if self.scope not in SCOPES:
            problems.append(f"scope={self.scope!r} 不在 {sorted(SCOPES)}")
        if self.fire not in FIRE_MODES:
            problems.append(f"fire={self.fire!r} 不在 {sorted(FIRE_MODES)}")
        if self.rearm not in REARM_MODES:
            problems.append(f"rearm={self.rearm!r} 不在 {sorted(REARM_MODES)}")
        if self.cooldown_seconds < 0:
            problems.append("cooldown_seconds 不能为负")
        if problems:
            raise ContractError(problems)


def default_lifecycle_for(condition_type: str) -> Lifecycle:
    """没有显式写 lifecycle 时用什么默认值。

    ``occurrence`` 型默认 ``fire="every"`` —— 它的"不重复"已经由去重键保证了,
    再叠一层"每个范围只触发一次"是双重限制:"久别之后重新在场"一天可能发生
    三次,不该只报第一次。产品规范的两个示例正好印证:步数那条显式写了
    ``fire: once``,解锁那条整个 lifecycle 块都没写。

    其余规则默认"每天一次、次日重新武装" —— 绝大多数感知规则想要的就是这个。
    """
    if condition_type == "occurrence":
        return Lifecycle(fire="every")
    return Lifecycle()


@dataclass(frozen=True)
class EventDefinition:
    """用户或宿主定义的一条规则。"""

    definition_id: str
    version: int
    signal: str
    condition_type: str
    event_type: str
    enabled: bool = True
    #: 盯哪个字段。``occurrence`` 型规则盯的是整条信号的到达，为 ``None``。
    field_name: str | None = None
    operator: str | None = None
    value: Any = None
    #: 没写时按 condition_type 取默认值,见 default_lifecycle_for。
    lifecycle: Lifecycle | None = None
    wake_enabled: bool = True
    #: 只属于某个用户的规则；``None`` = 宿主级，对所有用户生效。
    subject_id: str | None = None
    #: ``occurrence`` 型用什么去重。默认用 ``source_event_id``。
    dedupe_field: str = "source_event_id"

    def __post_init__(self) -> None:
        if self.lifecycle is None:
            object.__setattr__(
                self, "lifecycle", default_lifecycle_for(self.condition_type)
            )

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> "EventDefinition":
        """从 dict 解析。宿主想用 YAML 就自己 ``safe_load`` 成 dict 再进来。"""
        if not isinstance(payload, Mapping):
            raise ContractError(["definition 必须是一个对象"])
        problems: list[str] = []

        def need(key: str, types: tuple[type, ...], label: str) -> Any:
            raw = payload.get(key)
            if not isinstance(raw, types) or (isinstance(raw, str) and not raw.strip()):
                problems.append(f"{key}: 必填，{label}")
                return None
            return raw

        definition_id = need("id", (str,), "非空字符串")
        version = need("version", (int,), "整数")
        source = payload.get("source") or {}
        condition = payload.get("condition") or {}
        event = payload.get("event") or {}
        wake = payload.get("wake") or {}

        signal = source.get("signal") if isinstance(source, Mapping) else None
        if not isinstance(signal, str) or not signal.strip():
            problems.append("source.signal: 必填")

        condition_type = condition.get("type") if isinstance(condition, Mapping) else None
        if not isinstance(condition_type, str) or not condition_type.strip():
            problems.append("condition.type: 必填")

        event_type = event.get("type") if isinstance(event, Mapping) else None
        if not isinstance(event_type, str) or not event_type.strip():
            problems.append("event.type: 必填")

        if problems:
            raise ContractError(problems)

        raw_lifecycle = payload.get("lifecycle")
        if raw_lifecycle is None:
            lifecycle = default_lifecycle_for(condition_type)   # type: ignore[arg-type]
        else:
            fallback = default_lifecycle_for(condition_type)    # type: ignore[arg-type]
            lifecycle = Lifecycle(
                scope=raw_lifecycle.get("scope", fallback.scope),
                fire=raw_lifecycle.get("fire", fallback.fire),
                rearm=raw_lifecycle.get("rearm", fallback.rearm),
                cooldown_seconds=float(raw_lifecycle.get("cooldown_seconds", 0) or 0),
            )
        return cls(
            definition_id=definition_id,          # type: ignore[arg-type]
            version=version,                      # type: ignore[arg-type]
            signal=signal,                        # type: ignore[arg-type]
            condition_type=condition_type,        # type: ignore[arg-type]
            event_type=event_type,                # type: ignore[arg-type]
            enabled=bool(payload.get("enabled", True)),
            field_name=source.get("field"),
            operator=condition.get("operator"),
            value=condition.get("value"),
            lifecycle=lifecycle,
            wake_enabled=bool(wake.get("enabled", True)),
            subject_id=payload.get("subject_id"),
            dedupe_field=(payload.get("deduplication") or {}).get("key", "source_event_id"),
        )


@dataclass(frozen=True)
class RuleState:
    """一条规则在某个范围内的状态。

    ``scope_key`` 是"哪一天"（或 ``forever``）。换了一天就是一条新状态，
    所以"今天已经触发过"不会影响明天 —— 这就是 ``rearm=next_scope``。
    """

    previous_value: Any = None
    fired_in_scope: bool = False
    last_fired_at: str | None = None
    #: ``occurrence`` 型见过哪些去重键。有上限，见 ``MAX_SEEN_KEYS``。
    seen_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_value": self.previous_value,
            "fired_in_scope": self.fired_in_scope,
            "last_fired_at": self.last_fired_at,
            "seen_keys": list(self.seen_keys),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "RuleState":
        raw = raw or {}
        return cls(
            previous_value=raw.get("previous_value"),
            fired_in_scope=bool(raw.get("fired_in_scope")),
            last_fired_at=raw.get("last_fired_at"),
            seen_keys=tuple(raw.get("seen_keys") or ()),
        )


#: ``occurrence`` 型记住多少个去重键。不设上限的话，一个高频信号会让这条
#: 状态记录无限膨胀 —— 而它每次求值都要被读出来。
MAX_SEEN_KEYS = 200


@dataclass(frozen=True)
class RuleResult:
    """一次求值的结果。"""

    fired: bool
    state: RuleState
    previous: Any = None
    current: Any = None
    #: 为什么触发（或为什么没触发）。进事件信封，也进排查日志。
    reason: str | None = None


__all__ = [
    "SCOPES", "FIRE_MODES", "REARM_MODES", "MAX_SEEN_KEYS",
    "Lifecycle", "default_lifecycle_for",
    "EventDefinition", "RuleState", "RuleResult",
]
