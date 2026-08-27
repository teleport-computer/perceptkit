"""标准观测 —— 上报数据经过校验和标准化之后的样子。

这是整套系统里**唯一**的事实载体:current 是它的投影,日聚合是它的派生,
事件是对它的判断。三者都可以从观测重算,反过来不行 —— 聚合是压缩过的,
压缩不可逆。所以"只存 current 和日聚合、不存观测"会让修订、删除、
算法升级后的重算**全部做不了**。

时区为什么单独一个字段,而不是从 ``occurred_at`` 的偏移推:
偏移只够算"这条算哪一天",不够处理夏令时。``+08:00`` 能定位到日期,
但纽约的 ``-04:00`` 和 ``-05:00`` 是同一个时区在不同季节 —— 光看偏移
分不出来,DST 切换那天(那天有 25 小时)就会算错。所以要 IANA 名字。

``timezone`` 缺失时怎么办属于处理层的兜底策略,不在契约里定死 ——
见 ``OPEN-QUESTIONS.md`` B2,这一条还没和产品方对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import availability as _availability
from ._time import TimestampError, parse_timestamp
from .errors import ContractError


@dataclass(frozen=True)
class Observation:
    """一条标准观测(wire 形态 —— producer 发过来的样子)。

    存储形态比这个多几个字段(``observation_id`` / ``subject_id`` /
    ``received_at`` / ``effective_local_date``),那些由宿主或处理层补,
    不由 producer 提供 —— 见 :mod:`perceptkit.contracts.records`。
    """

    signal: str
    signal_schema_version: int
    occurred_at: datetime
    availability: str
    #: ``observed`` 时必填。``0`` / ``False`` / ``[]`` 都是合法值。
    value: dict[str, Any] | None = None
    #: 上游的稳定身份,用于重传幂等。事件型/样本型必填;
    #: 纯状态型(如电量快照)可以没有,由 manifest 声明确定性 identity 策略。
    source_event_id: str | None = None
    #: 发生时的 IANA 时区名(如 ``Asia/Shanghai``)。见模块开头。
    timezone: str | None = None
    #: 来源侧的版本号。只有支持修订的来源(HealthKit / Calendar)才有。
    source_revision: str | int | None = None
    #: ``unavailable`` 时的粗粒度原因。默认不进 agent 的上下文。
    reason: str | None = None
    #: 协议没定义的额外字段。原样保留便于排查,不参与判断。
    extensions: dict[str, Any] = field(default_factory=dict)

    # -- 派生判断（都只读 availability，集中在这里免得每个调用方各判一次） --

    @property
    def is_observed(self) -> bool:
        return _availability.normalize(self.availability) == _availability.OBSERVED

    @property
    def updates_current(self) -> bool:
        """这条该不该更新 current 的数值。"""
        return _availability.updates_current(self.availability)

    @property
    def enters_trend(self) -> bool:
        """这条该不该进趋势和 streak。``no_data`` 不当 0 用。"""
        return _availability.enters_trend(self.availability)

    @classmethod
    def parse(cls, payload: object) -> "Observation":
        """从一个 dict 解析并校验。错误一次报全,不是遇到第一个就抛。"""
        errors: list[str] = []
        if not isinstance(payload, dict):
            raise ContractError(
                [f"observation must be an object, got {type(payload).__name__}"]
            )

        signal = payload.get("signal")
        if not isinstance(signal, str) or not signal.strip():
            errors.append("signal: required, must be a non-empty string")

        version = payload.get("signal_schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            errors.append("signal_schema_version: required, must be an integer")

        occurred_at: datetime | None = None
        try:
            occurred_at = parse_timestamp(payload.get("occurred_at"), field="occurred_at")
        except TimestampError as exc:
            errors.append(str(exc))

        raw_state = payload.get("availability")
        if not isinstance(raw_state, str):
            errors.append("availability: required, must be a string")
            state = _availability.UNAVAILABLE
        elif raw_state not in _availability.LEGACY_STATES:
            errors.append(
                f"availability: {raw_state!r} is not one of "
                f"{sorted(_availability.AVAILABILITY_STATES)}"
            )
            state = _availability.UNAVAILABLE
        else:
            state = _availability.normalize(raw_state)

        value = payload.get("value")
        if state == _availability.OBSERVED:
            # observed 就必须有值。注意 `{}` 是合法的(某些信号的 payload 本来就空),
            # 但 None 不是 —— 那说明 producer 自己也不知道观测到了什么。
            if value is None:
                errors.append("value: required when availability is 'observed'")
            elif not isinstance(value, dict):
                errors.append(
                    f"value: must be an object, got {type(value).__name__}"
                )
        elif value is not None and not isinstance(value, dict):
            errors.append(f"value: must be an object when present, got {type(value).__name__}")

        source_event_id = payload.get("source_event_id")
        if source_event_id is not None and (
            not isinstance(source_event_id, str) or not source_event_id.strip()
        ):
            errors.append("source_event_id: must be a non-empty string when present")

        tz = payload.get("timezone")
        if tz is not None and (not isinstance(tz, str) or not tz.strip()):
            errors.append("timezone: must be a non-empty IANA name when present")

        revision = payload.get("source_revision")
        if revision is not None and not isinstance(revision, (str, int)):
            errors.append("source_revision: must be a string or integer when present")

        reason = payload.get("reason")
        if reason is not None:
            if not isinstance(reason, str):
                errors.append("reason: must be a string when present")
            elif reason not in _availability.UNAVAILABLE_REASONS:
                # 不拒收 —— 只是提醒用粗粒度的那几个。穷举操作系统的每种权限
                # 状态属于 adapter 的诊断,不该硬塞进公共协议。
                errors.append(
                    f"reason: {reason!r} is not one of "
                    f"{sorted(_availability.UNAVAILABLE_REASONS)}"
                )

        if errors:
            raise ContractError(errors)

        known = {
            "signal", "signal_schema_version", "occurred_at", "availability",
            "value", "source_event_id", "timezone", "source_revision", "reason",
        }
        return cls(
            signal=signal.strip(),                 # type: ignore[union-attr]
            signal_schema_version=version,         # type: ignore[arg-type]
            occurred_at=occurred_at,               # type: ignore[arg-type]
            availability=state,
            value=value,
            source_event_id=source_event_id.strip() if source_event_id else None,
            timezone=tz.strip() if tz else None,
            source_revision=revision,
            reason=reason,
            extensions={k: v for k, v in payload.items() if k not in known},
        )


__all__ = ["Observation"]
