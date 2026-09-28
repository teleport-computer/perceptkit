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
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Mapping, Sequence

from .contracts.context import IngestContext
from .contracts.report import ReportEnvelope
from .manifest.minimal import MINIMAL_SIGNALS
from .manifest.checks import require_public_dimension_fields
from .manifest.types import SignalDefinition
from .ports.storage import StoragePort
from .ports.wake import WakePort
from .processing.dispatch import DispatchOutcome, drain
from .processing.pipeline import AGGREGATION_VERSION, IngestOutcome, ingest_report
from .processing.recompute import RecomputeOutcome, recompute_range
from .processing.retract import apply_retractions
from .processing.source_sync import sync_source_mirror
from .retention import plan_retention
from .processing.scheduled import ScheduledOutcome, evaluate_absence, evaluate_daily
from .queries import api as _queries
from .rules.types import EventDefinition


@dataclass
class PerceptionKit:
    """把端口、manifest 和规则装配起来。"""

    storage: StoragePort
    wake: WakePort | None = None
    #: 信号声明。默认是 ``MINIMAL_SIGNALS``（33 个，覆盖四种存储形态）；
    #: 宿主应当传自己的完整 manifest。
    signals: Mapping[str, SignalDefinition] = field(
        default_factory=lambda: dict(MINIMAL_SIGNALS)
    )
    #: 规则。可以是一份固定的表（对所有人相同 + 带 subject_id 的只给那个人），
    #: 也可以是一个 :class:`~perceptkit.ports.definitions.DefinitionProviderPort`
    #: —— 后者让"用户自己配规则、改完立刻生效、删了之后历史事件仍解释得清"
    #: 有一条标准接法，而不是每家宿主自己发明一套。
    definitions: Any = ()
    #: 宿主注册的自定义 evaluator。普通用户配置仍然只能用声明式模板。
    extra_evaluators: Mapping[str, Callable[..., Any]] | None = None
    #: Only omitted timezone may use this validated IANA Host fallback (D09).
    timezone_fallback: str | None = None
    max_observations: int = 200

    def __post_init__(self) -> None:
        require_public_dimension_fields(self.signals)

    @property
    def _definitions(self):
        """把 `definitions` 统一成 provider —— 求值处只认一种形状。

        **惰性解析而不是构造时缓存**：宿主会在运行时直接
        ``kit.definitions = [...]`` 换规则（热更新最朴素的形态）。构造时
        缓存的话那种赋值就悄悄不生效了 —— 用户改了规则却没反应，
        而且没有任何地方报错。

        按对象身份缓存，所以没换的时候不会每次重新包一遍。
        """
        from .ports.definitions import as_provider
        source = self.definitions
        cached = getattr(self, "_definitions_cache", None)
        if cached is not None and cached[0] is source:
            return cached[1]
        provider = as_provider(source)
        object.__setattr__(self, "_definitions_cache", (source, provider))
        # 换一份规则时，把**上一版**存进历史档。事件只记 (id, 版本)，
        # 不留档的话：用户把"超 71kg 提醒"改成 75 之后，上周那条提醒就再也
        # 解释不清为什么发；整条删掉时更是直接变成一串无从追溯的 id
        # （外部审查 F11）。留的是"回看"，不是"继续生效"—— 生效与否永远
        # 只问当前 provider，两者混了的话用户删掉的规则会继续叫醒他。
        archive = getattr(self, "_definition_archive", None)
        if archive is None:
            archive = {}
            object.__setattr__(self, "_definition_archive", archive)
        for d in getattr(provider, "__iter__", lambda: ())():
            archive.setdefault((d.definition_id, d.version), d)
        return provider

    def definitions_for(self, subject_id: str) -> Sequence[EventDefinition]:
        """这个人当前生效的规则。宿主传 provider 时每次都会重新问它 ——
        所以"用户改完规则多久生效"由宿主的缓存策略决定，kit 不替它决定。"""
        return self._definitions.definitions_for(subject_id)

    def definition_at(self, definition_id: str, version: int):
        """按 id + 版本回看一条规则，**包括已经删掉的**。

        事件只记 id + 版本；答不出来的话，规则删掉之后那些历史事件就变成
        一串无从追溯的 id，用户问「这条为什么叫醒我」再也答不了。
        """
        found = self._definitions.definition_at(definition_id, version)
        if found is not None:
            return found
        # 当前这份里没有 —— 去历史档里找。宿主自己的 provider 如果保留了
        # 历史，上面那一步就已经命中了；没保留的（含最朴素的
        # `kit.definitions = [...]` 热替换）由这里兜住。
        return getattr(self, "_definition_archive", {}).get(
            (definition_id, version))

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
            definitions=self.definitions_for(context.subject_id),
            extra_evaluators=self.extra_evaluators,
            timezone_fallback=self.timezone_fallback,
            max_observations=self.max_observations,
            definition_at=self.definition_at,
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
            now=now, signals=self.signals,
            definitions=self.definitions_for(subject_id),
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

    def sync_source_mirror(self, batch, *, context: IngestContext):
        """把一批日历/提醒的来源数据落进镜像，并推进同步状态。

        和 :meth:`ingest` 对等的那个入口 —— 来源镜像走的是完全不同的一条路
        （它存「来源现在有哪些条目」，不是「我们每次看到了什么」），
        但同样有一串**错了不报错、而且大多不可逆**的规则：增量不许删、
        全量只在自己声明的范围内删、失败的批次什么都不动也不推进游标、
        三个动作必须在同一个事务里。

        见 ``processing.source_sync`` 的模块文档 —— 每条规则都配了它对应的
        那个故障长什么样。

        **不解析来源格式。** 苹果日历、Google、Exchange 的条目长得完全不一样，
        翻译成标准镜像记录是宿主的活。
        """
        return sync_source_mirror(self.storage, batch, context=context)

    def apply_retractions(self, retractions, *, now: datetime,
                          recompute: bool = True):
        """来源撤回了几条事实：记下来、当前值重选、返回受影响的天数。

        和 :meth:`ingest` 是两条路：ingest 说"这是一条新读数"，这个说
        "之前那条不作数了"。**刻意不做成 availability 的第四个状态** ——
        那个状态位回答的是"这次有没有拿到数"，和"之前那条还作不作数"
        是两个正交的问题；混在一起会让旧宿主把撤回当成传感器故障，
        于是被删掉的数值作为 last_known 继续显示出来。

        默认**直接把受影响那几天的聚合重算并写回**（``recompute=False``
        可以关掉，由调用方自己按更大的范围重算）。重算那条路已经会排除
        被撤回的观测。

        Only source_event_id identity strategy with a valid ID supports this
        deletion envelope end-to-end. Singleton/deterministic/unknown strategies raise
        UnsupportedRetractionIdentityError before any batch write; observed_at
        is deletion audit time, never a substitute for the original Fact time.
        """
        # 🔴 受影响那几天的聚合**真的会重算并写回**，而且跟记撤回在同一个
        # 事务里。早先只返回一个"有几天受影响"的计数，调用方拿不到是哪几天，
        # 于是谁也没去重算；后来虽然重算了，却是在事务**外面**做的 ——
        # 撤回提交了、重算崩了，两边再也对不上，下一轮还以为上一轮成功了。
        def _rebuild(subject_id: str, signal: str, day) -> None:
            # apply_retractions has already acquired ALL affected aggregate
            # resources. Reuse that transaction, never create a nested owner.
            from .processing.recompute import _recompute_owned_range
            _recompute_owned_range(
                self.storage, self.signals,
                subject_id=subject_id, signal=signal,
                start_date=day, end_date=day, now=now, version=AGGREGATION_VERSION,
                # 明细可能已经按保留期清掉了。清掉之后重算会得到一份
                # 残缺统计，而那比"没重算"更糟 —— 旧值已经被覆盖。
                allow_incomplete=False,
            )

        outcome = apply_retractions(
            self.storage, list(retractions),
            signals=dict(self.signals), now=now,
            # 重算跟着撤回走在**同一个事务**里，见 apply_retractions。
            on_affected_day=_rebuild if recompute else None,
            definitions_for=self.definitions_for, definition_at=self.definition_at,
            extra_evaluators=self.extra_evaluators,
        )
        return outcome

    def run_retention(
        self, *, subject_id: str, now: datetime, dry_run: bool = True,
    ) -> dict[str, Any]:
        """按 manifest 清理过期数据。**默认只试跑。**

        规则在 ``retention.plan_retention``（纯函数），删除走存储端口 ——
        **什么时候跑仍然是宿主的事**，这里没有任何调度。给这个入口是因为
        规则只该有一份：早先 kit 只声明保留期、不提供执行，于是每个宿主
        自己照 manifest 推导一遍，而这条路上每个坑错了都不报错
        （明细和聚合是两个保留期、PERMANENT 要跳过、没声明的不许猜、
        去重身份不能跟着明细删）。

        ``dry_run=True`` 是默认值，不是谨慎癖：这是这个包里**唯一**会永久
        删用户数据的动作，而保留期的 bug 从外面完全看不见 —— 系统照常工作，
        用户只是安静地少了历史，直到有人问一个数据已经答不出的问题。
        先看一眼数字，再决定要不要真删。

        按 subject 清，和这个端口所有其他方法一样。宿主要全量清就自己循环 ——
        跨用户的一条 DELETE 少写一个 WHERE 就会删掉别人的数据，
        而这个包里没有一个地方允许那种写法存在。
        """
        plan = plan_retention(self.signals, now=now)
        removed: dict[str, int] = {}
        if not dry_run:
            for action in plan.actions:
                if action.kind == "observations":
                    n = self.storage.delete_observations(
                        subject_id=subject_id, signal=action.signal,
                        before=datetime.combine(action.before, time.min,
                                                tzinfo=timezone.utc),
                    )
                else:
                    n = self.storage.delete_aggregates(
                        subject_id=subject_id, signal=action.signal,
                        before=action.before,
                    )
                if n:
                    removed[f"{action.signal}.{action.kind}"] = (
                        removed.get(f"{action.signal}.{action.kind}", 0) + int(n))
        return {
            "applied": not dry_run,
            "planned": [
                {"signal": a.signal, "kind": a.kind, "before": a.before.isoformat()}
                for a in plan.actions
            ],
            # 故意不删的也要列出来 —— 一份只说"删了 0 条"的报告，读不出
            # 「是没到期，还是规则写错了」。
            "skipped": [{"signal": s.signal, "code": s.code, "detail": s.detail}
                        for s in plan.skipped],
            "removed": removed,
        }

    def evaluate_absence(
        self, *, subject_id: str, now: datetime,
    ) -> ScheduledOutcome:
        """定时调，跑 ``absence``（该来的没来）。"""
        return evaluate_absence(
            storage=self.storage, subject_id=subject_id, now=now,
            signals=self.signals,
            definitions=self.definitions_for(subject_id),
            extra_evaluators=self.extra_evaluators,
        )

    # -- 读取侧 ----------------------------------------------------------
    #
    # 这条路和写入侧共用存储，方向相反：agent 主动来查。
    # 八个函数的实现在 queries/api.py —— 这里只是绑上 manifest 的薄封装。

    def list_conflicts(self, *, subject_id: str, signal: str | None = None,
                       status: str | None = None):
        """Durable quarantined candidates and their immutable resolution audit."""
        return list(self.storage.list_conflicts(subject_id=subject_id, signal=signal, status=status))

    def get_current(self, *, subject_id: str, signals: Sequence[str],
                    now: datetime) -> dict[str, list[_queries.CurrentView]]:
        """取当前值，**带 TTL 判定**：过期的不冒充现在。"""
        return _queries.get_current(
            self.storage, subject_id=subject_id, signals=signals,
            manifest=self.signals, now=now,
        )

    def get_last_known(self, *, subject_id: str, signal: str) -> list[_queries.CurrentView]:
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
        source = (self.definitions_for(subject_id) if subject_id is not None
                  else tuple(self._definitions))
        return _queries.list_definitions(source, subject_id=subject_id, **kw)

    def export_subject(self, *, subject_id: str, **kw) -> dict[str, Any]:
        """把一个人的全部数据导出来（「把我的数据给我」那条法定请求）。

        **只含 kit 管的部分。** 宿主自己存的东西要自己追加进去 ——
        返回值里的 `kit_managed_only` 就是提醒这件事的。
        """
        return _queries.export_subject(
            self.storage, subject_id=subject_id, manifest=self.signals, **kw,
        )


__all__ = ["PerceptionKit"]
