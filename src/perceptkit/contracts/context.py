"""可信上下文 —— 那些**绝不能**从上报信封里读的值。

信封是 producer 写的,producer 在用户设备上,设备是可以被改的。所以三样东西
必须由宿主从已认证的连接注入,而不是从信封里读:

    subject_id    这批数据算谁的。信客户端自报 = 任何人都能往别人账号里写观测。
    received_at   宿主自己的钟。producer 报的时间可能不准(设备时钟错、
                  离线补传),审计和乱序判断要用可信的那个。
    auth_scope    这个连接被授权写哪些 signal。用户关掉了健康权限,
                  设备却还在发 health_* —— 得在这里挡掉,不能等写库了才发现。

**为什么做成显式参数而不是全局变量**:如果 ``ingest()`` 的签名里没有它们的位置,
实现只有两条路 —— 去读某个环境全局(那就没法并发、没法测试),或者退回去信
信封里的值(那就是跨用户写入漏洞)。签名上留位置,是唯一能让"不信客户端"
这句话真正成立的做法。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._time import parse_timestamp
from .errors import ContractError


@dataclass(frozen=True)
class IngestContext:
    """一次 ingest 的可信上下文。全部由宿主提供,信封不得覆盖。"""

    #: 这批数据属于谁。宿主从已认证连接解析,**不读信封**。
    subject_id: str
    #: 宿主收到这批数据的时刻。必须是 aware datetime。
    received_at: datetime
    #: 这个连接被授权写哪些 signal。``None`` = 不限制(宿主自己已经把过关了)。
    #: 空集合 = 一个都不许写(权限全关),和 ``None`` 是两回事。
    auth_scope: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ContractError(["subject_id: required, must be a non-empty string"])
        # 走一遍解析,把 naive datetime 挡在门外 —— 用 naive 的钟做乱序判断,
        # 跨时区部署时会静默算错。
        parse_timestamp(self.received_at, field="received_at")

    def allows(self, signal: str) -> bool:
        """这个连接有没有被授权写这个 signal。"""
        return self.auth_scope is None or signal in self.auth_scope


__all__ = ["IngestContext"]
