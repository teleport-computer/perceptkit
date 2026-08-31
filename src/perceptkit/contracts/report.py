"""上报契约 —— 宿主交给 kit 的东西长什么样。

这一层刻意很薄。它只回答一个问题:**一个宿主怎样把一批已经采集到的数据,
以稳定、可校验、可版本化的方式交给 kit。**

不在这里的(属于 adapter,不属于协议):

    HTTP 路由 · 掉线重传队列 · 加密 / 解密 / enclave / 密钥管理 ·
    设备怎么读 HealthKit / Core Location / EventKit

kit 收到的是**已经过宿主认证、解密和基本传输校验**之后的东西。

两个字段刻意不在信封里:

    subject_id    由宿主从已认证的连接注入,不能信客户端自报身份 ——
                  否则任何人都能往别人账号里写观测。
    received_at   由宿主的接收层生成。producer 报的时间(``reported_at``)
                  可能不准(设备时钟错、离线补传),审计要用宿主自己的钟。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from . import versioning
from ._time import TimestampError, parse_timestamp
from .errors import ContractError
from .observation import Observation


@dataclass(frozen=True)
class ReportEnvelope:
    """一批上报。

    ``observations`` 允许为空:producer 有时只是想说"我还活着,这轮没有新东西",
    这也是有效信息(可以用来更新 coverage),不该被当成错误。
    """

    schema_version: int
    report_id: str
    producer: str
    observations: tuple[Observation, ...] = ()
    #: 宿主生成或匿名化的设备实例标识。**不该是可直接追踪的硬件序列号。**
    producer_instance_id: str | None = None
    #: producer 自己说的上报时刻。仅作参考 —— 权威的是宿主注入的 received_at。
    reported_at: datetime | None = None
    #: 协议没定义的额外字段。原样保留,便于排查,但不参与任何判断。
    extensions: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: object) -> "ReportEnvelope":
        """从一个 dict 解析并校验。

        未知的顶层字段进 ``extensions`` 而不是报错 —— 见
        :mod:`perceptkit.contracts.versioning`,这是有意的向前兼容。
        """
        errors: list[str] = []
        if not isinstance(payload, dict):
            raise ContractError([f"report must be an object, got {type(payload).__name__}"])

        # 版本先判:版本不对,底下的字段语义就无从谈起,继续校验没有意义。
        try:
            schema_version = versioning.check_report_version(payload.get("schema_version"))
        except versioning.UnsupportedSchemaVersion as exc:
            raise ContractError([str(exc)]) from exc

        report_id = payload.get("report_id")
        if not isinstance(report_id, str) or not report_id.strip():
            errors.append("report_id: required, must be a non-empty string")

        producer = payload.get("producer")
        if not isinstance(producer, str) or not producer.strip():
            errors.append("producer: required, must be a non-empty string (e.g. 'ios')")

        instance_id = payload.get("producer_instance_id")
        if instance_id is not None and not isinstance(instance_id, str):
            errors.append("producer_instance_id: must be a string when present")

        reported_at: datetime | None = None
        raw_reported = payload.get("reported_at")
        if raw_reported is not None:
            try:
                reported_at = parse_timestamp(raw_reported, field="reported_at")
            except TimestampError as exc:
                errors.append(str(exc))

        raw_observations = payload.get("observations", [])
        observations: list[Observation] = []
        if not isinstance(raw_observations, (list, tuple)):
            errors.append("observations: must be an array")
        else:
            for index, item in enumerate(raw_observations):
                try:
                    observations.append(Observation.parse(item))
                except ContractError as exc:
                    errors.extend(f"observations[{index}].{e}" for e in exc.errors)

        if errors:
            raise ContractError(errors)

        known = {
            "schema_version", "report_id", "producer",
            "producer_instance_id", "reported_at", "observations",
        }
        extensions = {k: v for k, v in payload.items() if k not in known}

        return cls(
            schema_version=schema_version,
            report_id=report_id.strip(),          # type: ignore[union-attr]
            producer=producer.strip(),            # type: ignore[union-attr]
            observations=tuple(observations),
            producer_instance_id=instance_id,
            reported_at=reported_at,
            extensions=extensions,
        )

    def signals(self) -> Iterable[str]:
        """这批上报涉及哪些 signal。用来只加载相关的 EventDefinition。"""
        seen: set[str] = set()
        for obs in self.observations:
            if obs.signal not in seen:
                seen.add(obs.signal)
                yield obs.signal


__all__ = ["ContractError", "ReportEnvelope"]
