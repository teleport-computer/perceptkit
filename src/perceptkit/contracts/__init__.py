"""契约层 —— 数据在 kit 边界上的样子。

四个信封,对应管线的四个交接点:

    ReportEnvelope   宿主 → kit      一批采集到的数据
    Observation      kit 内部        校验、标准化之后的事实
    PerceptionEvent  kit → 宿主      一次规则命中
    WakeReceipt      宿主 → kit      runtime 对投递的应答

外加两样不属于信封、但必须在契约层出现的东西:

    IngestContext    绝不能从信封里读的可信值(谁的数据、宿主的钟、授权范围)
    IngestReceipt    一批上报的处理结果,用于重传幂等

**这一层只描述形状和校验规则,不做任何 I/O。** 谁在什么时候调、
落到哪张表,分别是 ``processing`` 和 ``ports`` 的事。
"""
from __future__ import annotations

from .availability import (
    AVAILABILITY_STATES,
    LEGACY_STATES,
    NO_DATA,
    OBSERVED,
    UNAVAILABLE,
    UNAVAILABLE_REASONS,
    enters_trend,
    normalize,
    updates_current,
)
from . import delivery, records
from .context import IngestContext
from .errors import ContractError
from .event import EventCondition, PerceptionEvent
from .observation import Observation
from .receipt import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    INGEST_REJECTED,
    WAKE_ACCEPTED,
    WAKE_DUPLICATE,
    WAKE_ENQUEUE_FAILED,
    WAKE_REJECTED,
    WAKE_RETRYABLE,
    WAKE_SUPPRESSED,
    IngestReceipt,
    WakeReceipt,
)
from .records import (
    CONFLICT,
    IGNORE,
    REPLACE,
    CalendarEventMirror,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
    decide_current_update,
)
from .report import ReportEnvelope
from .versioning import (
    EVENT_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    SUPPORTED_REPORT_VERSIONS,
    UnsupportedSchemaVersion,
    check_report_version,
)

__all__ = [
    # availability
    "OBSERVED", "NO_DATA", "UNAVAILABLE",
    "AVAILABILITY_STATES", "LEGACY_STATES", "UNAVAILABLE_REASONS",
    "normalize", "updates_current", "enters_trend",
    # envelopes
    "ReportEnvelope", "Observation", "PerceptionEvent", "EventCondition",
    # trusted context + receipts
    "IngestContext", "IngestReceipt", "WakeReceipt",
    "INGEST_ACCEPTED", "INGEST_DUPLICATE", "INGEST_CONFLICT", "INGEST_REJECTED",
    "WAKE_ACCEPTED", "WAKE_DUPLICATE", "WAKE_SUPPRESSED",
    "WAKE_ENQUEUE_FAILED", "WAKE_REJECTED", "WAKE_RETRYABLE",
    # versioning
    "REPORT_SCHEMA_VERSION", "EVENT_SCHEMA_VERSION", "SUPPORTED_REPORT_VERSIONS",
    "UnsupportedSchemaVersion", "check_report_version",
    # 逻辑存储对象
    "StoredObservation", "CurrentProjection", "DailyAggregate",
    "CalendarEventMirror", "ReminderItemMirror", "SourceSyncState",
    "DurableDedupeIdentity", "EventOutboxEntry",
    "decide_current_update", "REPLACE", "IGNORE", "CONFLICT",
    # 投递状态机
    "delivery", "records",
    # errors
    "ContractError",
]
