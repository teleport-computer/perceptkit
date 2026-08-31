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
from .processing.pipeline import AGGREGATION_VERSION, IngestOutcome, ingest_report
from .processing.recompute import RecomputeOutcome, recompute_range
from .processing.scheduled import ScheduledOutcome, evaluate_absence, evaluate_daily
from .queries import api as _queries
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

    # -- 时钟驱动的两种规则 ----------------------------------------------
    #
    # 九种规则里有两种主管线跑不到:streak 要按天判(跟着观测跑是一天几千次,
    # 而它一天只可能变化一次)、absence 是【没有数据才该触发】(跟着观测跑
    # 永远等不到自己被调用)。
    #
    # 宿主不用为此多起一个东西 —— 投递那条线本来就需要定时循环,搭上去就行:
    #
    #     while True:
    #         kit.dispatch_pending(worker_id="w1", now=now())
    #         kit.evaluate_absence(subject_id=..., now=now())
    #         sleep(60)

    def evaluate_daily(
        self, *, subject_id: str, local_date: date, now: datetime,
    ) -> ScheduledOutcome:
        """某天的聚合算完后调一次，跑 ``streak`` 这类按天判的规则。"""
        return evaluate_daily(
            storage=self.storage, subject_id=subject_id, local_date=local_date,
            now=now, signals=self.signals, definitions=self.definitions,
            extra_evaluators=self.extra_evaluators,
        )

    def recompute_aggregates(
        self, *, subject_id: str, signal: str, start: date, end: date,
        now: datetime, version: int | None = None,
        allow_incomplete: bool = False,
    ):
        """聚合算法升级之后，按新版本把历史重算一遍。

        **默认拒绝重算明细可能已经被保留期清掉的日子** —— 拿残缺明细折出来的
        永久统计会错一个数量级，而且旧值已经被覆盖、救不回来。真要算就显式
        传 ``allow_incomplete=True``，结果里会标出来。
        """
        return recompute_range(
            storage=self.storage, signals=self.signals, subject_id=subject_id,
            signal=signal, start_date=start, end_date=end,
            version=AGGREGATION_VERSION if version is None else version,
            now=now, allow_incomplete=allow_incomplete,
        )

    def evaluate_absence(
        self, *, subject_id: str, now: datetime,
    ) -> ScheduledOutcome:
        """定时调，跑 ``absence``（该来的没来）。"""
        return evaluate_absence(
            storage=self.storage, subject_id=subject_id, now=now,
            signals=self.signals, definitions=self.definitions,
            extra_evaluators=self.extra_evaluators,
        )

    # -- 读取侧 ----------------------------------------------------------
    #
    # 这条路和写入侧共用存储，方向相反：agent 主动来查。
    # 八个函数的实现在 queries/api.py —— 这里只是绑上 manifest 的薄封装。

    def get_current(self, *, subject_id: str, signals: Sequence[str],
                    now: datetime) -> dict[str, _queries.CurrentView]:
        """取当前值，**带 TTL 判定**：过期的不冒充现在。"""
        return _queries.get_current(
            self.storage, subject_id=subject_id, signals=signals,
            manifest=self.signals, now=now,
        )

    def get_last_known(self, *, subject_id: str, signal: str):
        return _queries.get_last_known(
            self.storage, subject_id=subject_id, signal=signal, manifest=self.signals,
        )

    def list_timeline(self, *, subject_id: str, signal: str, **kw):
        return _queries.list_timeline(
            self.storage, subject_id=subject_id, signal=signal,
            manifest=self.signals, **kw,
        )

    def get_daily(self, *, subject_id: str, signal: str, start: date, end: date):
        """日聚合。空缺的日子**不补零** —— `no_data` 不是 0。"""
        return _queries.get_daily_aggregates(
            self.storage, subject_id=subject_id, signal=signal,
            start_date=start, end_date=end,
        )

    def get_trend(self, *, subject_id: str, signal: str, field: str,
                  start: date, end: date) -> dict[str, Any]:
        """趋势。按 manifest 声明的模型选算法，并报出缺了几天。"""
        return _queries.get_trend(
            self.storage, subject_id=subject_id, signal=signal, field=field,
            manifest=self.signals, start_date=start, end_date=end,
        )

    def list_calendar_events(self, *, subject_id: str, **kw):
        """返回 ``(日程, 下一页游标)``。重复日程按窗口展开，见 processing/recurrence。"""
        return _queries.list_calendar_events(self.storage, subject_id=subject_id, **kw)

    def list_reminders(self, *, subject_id: str, **kw):
        return _queries.list_reminders(self.storage, subject_id=subject_id, **kw)

    def list_events(self, *, subject_id: str, **kw):
        """事件列表，可按投递状态筛、分页。

        「为什么没提醒我」的答案常常是 suppressed 或 rejected，不是 pending。
        """
        return _queries.list_events(self.storage, subject_id=subject_id, **kw)

    def list_definitions(self, *, subject_id: str | None = None, **kw):
        """当前装配了哪些规则。用户能自己配规则，就会问「我那条还在吗」。"""
        return _queries.list_definitions(self.definitions, subject_id=subject_id, **kw)

    def export_subject(self, *, subject_id: str, **kw) -> dict[str, Any]:
        """把一个人的全部数据导出来（「把我的数据给我」那条法定请求）。

        **只含 kit 管的部分。** 宿主自己存的东西要自己追加进去 ——
        返回值里的 `kit_managed_only` 就是提醒这件事的。
        """
        return _queries.export_subject(
            self.storage, subject_id=subject_id, manifest=self.signals, **kw,
        )


__all__ = ["PerceptionKit"]
