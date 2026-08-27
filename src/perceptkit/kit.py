"""接入口 —— 一个新 runtime 要打交道的全部东西。

    kit = PerceptionKit(storage=my_storage, wake=my_runtime, definitions=my_rules)
    result = kit.ingest(report, context=IngestContext(subject_id=..., received_at=...))

**宿主不需要读这个包的源码就能接上。** 顺序、幂等、一致性都在管线里，
宿主只填 ``StoragePort`` 和 ``WakePort`` 的方法体，再配几条规则。

上报和投递是**分开的两件事**：``ingest`` 同步做到"事件已落地并提交"就返回，
``dispatch`` 由宿主自己的 worker 驱动。这样上报接口的延迟只取决于数据库，
不取决于 agent runtime —— runtime 一慢，上报接口跟着超时、客户端重传、
雪上加霜。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping, Sequence

from .contracts.context import IngestContext
from .contracts.report import ReportEnvelope
from .manifest.minimal import MINIMAL_SIGNALS
from .manifest.types import SignalDefinition
from .ports.storage import StoragePort
from .ports.wake import WakePort
from .processing.dispatch import DispatchOutcome, drain
from .processing.pipeline import IngestOutcome, ingest_report
from .rules.types import EventDefinition


@dataclass
class PerceptionKit:
    """把端口、manifest 和规则装配起来。"""

    storage: StoragePort
    wake: WakePort | None = None
    #: 信号声明。默认是最小集（五个信号，覆盖两种存储形态）；
    #: 宿主应当传自己的完整 manifest。
    signals: Mapping[str, SignalDefinition] = field(
        default_factory=lambda: dict(MINIMAL_SIGNALS)
    )
    definitions: Sequence[EventDefinition] = ()
    #: 宿主注册的自定义 evaluator。普通用户配置仍然只能用声明式模板。
    extra_evaluators: Mapping[str, Callable[..., Any]] | None = None
    #: 观测没带时区时用什么兜底。见 OPEN-QUESTIONS B2 —— 这一条还没和
    #: 产品方对齐，所以由宿主传，不在包里写死。
    timezone_fallback: str | None = None
    max_observations: int = 200

    # -- 写入侧 ----------------------------------------------------------

    def ingest(
        self,
        report: ReportEnvelope | Mapping[str, Any],
        *,
        context: IngestContext,
        dispatch: bool = False,
        worker_id: str = "inline",
    ) -> IngestOutcome:
        """收一批上报，走完落地为止的全部步骤。

        ``dispatch=False``（默认）时**不投递** —— 事件留在发件箱，由宿主的
        worker 去投。这不是偷懒：同步投递会把 agent runtime 的延迟直接叠加到
        上报接口上。想同步投的宿主传 ``dispatch=True``，但要清楚代价。
        """
        envelope = (report if isinstance(report, ReportEnvelope)
                    else ReportEnvelope.parse(report))
        outcome = ingest_report(
            envelope,
            context=context,
            storage=self.storage,
            signals=self.signals,
            definitions=self.definitions,
            extra_evaluators=self.extra_evaluators,
            timezone_fallback=self.timezone_fallback,
            max_observations=self.max_observations,
        )
        if dispatch and outcome.events:
            if self.wake is None:
                raise ValueError("dispatch=True 需要一个 WakePort")
            self.dispatch_pending(worker_id=worker_id, now=context.received_at)
        return outcome

    # -- 投递侧 ----------------------------------------------------------

    def dispatch_pending(
        self, *, worker_id: str, now: datetime,
        limit: int = 100, lease_seconds: float = 60.0,
    ) -> DispatchOutcome:
        """把发件箱里能投的都投一遍。宿主的 worker 循环调它。

        ``now`` 由调用方传 —— 这个包不读时钟，否则重放和测试都做不了。
        """
        if self.wake is None:
            raise ValueError("没有 WakePort，无法投递")
        return drain(
            storage=self.storage, wake=self.wake, worker_id=worker_id,
            now=now, limit=limit, lease_seconds=lease_seconds,
        )

    # -- 读取侧 ----------------------------------------------------------
    #
    # 这条路和写入侧共用存储，方向相反：agent 主动来查。
    # 完整的八个查询函数在批 5，这里先给最常用的两个。

    def get_current(
        self, *, subject_id: str, signals: Sequence[str], now: datetime,
    ) -> dict[str, dict[str, Any]]:
        """取当前值，**带 TTL 判定**。

        过期的值不会冒充当前事实：返回 ``state="stale"`` 加上带 ``as_of`` 的
        ``last_known``。让模型自己说"你电量还有 87%"（其实是四小时前的），
        是这类系统最常见的一种说错话。
        """
        out: dict[str, dict[str, Any]] = {}
        raw = self.storage.get_current(subject_id=subject_id, signals=list(signals))
        for signal, projections in raw.items():
            for proj in projections:
                fresh = proj.expires_at is None or proj.expires_at > now
                out[signal] = {
                    "state": "fresh" if fresh else "stale",
                    "value": proj.typed_value if fresh else None,
                    "availability": proj.availability,
                    "last_known": {
                        "value": proj.typed_value,
                        "as_of": proj.observed_at.isoformat(),
                    },
                }
        for signal in signals:
            out.setdefault(signal, {
                "state": "no_data", "value": None,
                "availability": "no_data", "last_known": None,
            })
        return out

    def get_daily(
        self, *, subject_id: str, signal: str, start: date, end: date,
    ) -> list[dict[str, Any]]:
        """取日聚合。空缺的日子**不补零** —— ``no_data`` 不是 0。"""
        rows = self.storage.get_aggregate(
            subject_id=subject_id, signal=signal,
            start_date=start, end_date=end, aggregation_kind="daily",
        )
        return [
            {"date": r.local_date.isoformat(), "value": r.typed_aggregate,
             "coverage": r.source_coverage}
            for r in sorted(rows, key=lambda r: r.local_date)
        ]


__all__ = ["PerceptionKit"]
