"""存储端口 —— 宿主要填的方法体。

**这里只定行为，不定 SQL。** 用 PostgreSQL、SQLite、文档数据库、甚至内存，
都行；但必须满足同样的查询、幂等、重算、删除和一致性语义 —— 这一点由
``perceptkit.conformance`` 的测试来证明，不靠自觉。

宿主实现的每个方法都是**孤立的一件事**（写一条、读一批、提交一次）。
"先落地再投递""迟到数据不覆盖当前值""同一时刻不同内容要报冲突"这些顺序
和规则不在宿主手里 —— 它们在 kit 的处理管线里，宿主没有那个入口。
这不是不信任宿主，是让"写错的那条路根本不存在"。

**每个方法都必须按 subject 隔离。** ``subject_id`` 一律来自
:class:`~perceptkit.contracts.context.IngestContext`，绝不来自上报信封 ——
信封是设备写的，设备可以被改。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, ContextManager, Protocol, Sequence, runtime_checkable

from ..contracts.records import (
    CalendarEventMirror,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    SourceSyncState,
    StoredObservation,
)
from ..contracts.receipt import IngestReceipt, WakeReceipt
from ..contracts.retraction import Retraction


@runtime_checkable
class StoragePort(Protocol):
    """宿主的存储适配器。

    方法分六组：批级幂等 / 观测 / 当前值 / 聚合 / 来源镜像 / 事件与投递。
    """

    # -- 事务 ------------------------------------------------------------

    def transaction(self) -> ContextManager[None]:
        """一个原子边界。

        kit 会把"写规则状态 + 写待发件箱"这类**必须一起成功**的操作包在
        同一个 ``with`` 里。异常（包括 RetryableProjectionError）退出时，
        本次 report、Observation、identity、Current、Aggregate、RuleState、
        Outbox 必须全部回滚；其他事务不能看到部分提交。调用方重试整个 report。
        当前同步协议没有 durable rebuild，因此不能用 warning 或补偿承诺代替回滚。
        """
        ...

    # -- 批级幂等 --------------------------------------------------------

    def claim_report(
        self, *, subject_id: str, producer: str, report_id: str, payload_digest: str,
        received_at: datetime,
    ) -> IngestReceipt:
        """认领一批上报，同时回答"这批处理过没有"。

        同 identity + 同摘要 → 返回原来那份回执（``duplicate``），**不重复处理**。
        同 identity + 异摘要 → ``conflict``，不能静默挑一个覆盖。
        没见过         → ``accepted``，并占住这个 identity。

        必须是原子的 check-and-claim：两个并发请求带同一个 ``report_id``
        进来，只能有一个拿到 ``accepted``。
        """
        ...

    # -- 观测 ------------------------------------------------------------

    def append_observation(self, observation: StoredObservation) -> bool:
        """追加一条观测。已经存在（同一去重身份）时返回 ``False`` 且不重复写。

        返回值不是可有可无的：调用方靠它决定要不要去更新聚合 ——
        重复的观测如果也去加一遍日总数，那就是重复累计。
        """
        ...

    def list_observations(
        self, *, subject_id: str, signal: str,
        start: datetime | None = None, end: datetime | None = None,
        cursor: str | None = None, limit: int = 100,
    ) -> tuple[Sequence[StoredObservation], str | None]:
        """按时间取观测。返回 ``(结果, 下一页游标)``。

        **必须分页。** agent 问一句"我这个月都去过哪"，不设上限就是几千条
        直接塞进模型上下文。
        """
        ...

    def delete_observations(
        self, *, subject_id: str, signal: str | None = None,
        before: datetime | None = None,
    ) -> int:
        """按保留期清理明细，返回删了多少条。

        🔴 **绝不能删掉永久聚合还在依赖的唯一事实，也不能删掉去重身份。**
        删了去重身份，旧数据重放时会把永久聚合的数字加两遍，而且无法回滚。
        """
        ...

    # -- 当前值 ----------------------------------------------------------

    def get_current(
        self, *, subject_id: str, signals: Sequence[str],
    ) -> dict[str, Sequence[CurrentProjection]]:
        """取当前值。TTL 判定不在这里做 —— 这里只负责把存的东西读出来，
        "过期了算不算当前"由查询层按 manifest 判。
        """
        ...

    def compare_and_put_current(
        self, projection: CurrentProjection, *, expected_version: int,
    ) -> bool:
        """乐观并发地写当前值。版本对不上返回 ``False``，调用方重读重试。

        为什么不是简单的 upsert：两条并发上报（一条新一条旧）同时到达时，
        简单覆盖的结果取决于谁后写完 —— 旧数据可能赢。带版本号才能保证
        "只有一个胜者，而且是应该赢的那个"。
        """
        ...

    # -- 聚合 ------------------------------------------------------------

    def delete_aggregates(
        self, *, subject_id: str, signal: str, before: date,
    ) -> int:
        """按**聚合的**保留期清理日聚合，返回删了多少条。

        和 :meth:`delete_observations` 是两个动作，因为是两个保留期：典型形态
        就是「明细 1 年、聚合永久」。没有这个方法的话，宿主要么自己写 SQL
        （于是每个宿主各自重新推导一遍规则），要么干脆不清 —— 而
        「有限保留期的聚合永远不删」不会报错，只会让库一直长。

        🔴 **``aggregate_retention_days`` 是 PERMANENT 的信号绝不能进来。**
        判定在 kit 里（``run_retention``），不指望每个宿主自己记得。
        """
        ...

    def get_aggregate(
        self, *, subject_id: str, signal: str,
        start_date: date, end_date: date,
        aggregation_kind: str | None = None,
    ) -> Sequence[DailyAggregate]:
        ...

    def put_aggregate(self, aggregate: DailyAggregate) -> None:
        """写入或替换一个聚合。

        按 ``(subject, signal, date, kind, aggregation_version)`` 覆盖 ——
        换了 ``aggregation_version`` 就是新的一份，旧的留着，**不原地改写
        旧统计的语义**。每次写入必须将写入 version 从现存值加一（新行为 0），
        使已读旧值的增量 CAS 失败并重读。重算与事实变更仍须由调用方序列化。
        """
        ...

    def compare_and_put_aggregate(
        self, aggregate: DailyAggregate, *, expected_version: int,
    ) -> bool:
        """原子比较同一 aggregate key 的写入 version 并写入。

        不存在以 -1 比较；成功写入 version=expected_version+1。
        失败返回 False 且不得改写；Kit 重读后重新 fold，耗尽则回滚整个事务。
        aggregation_version 是算法口径，不能当作并发 version。
        """
        ...

    # -- 去重身份 --------------------------------------------------------
    #
    # 产品规范的端口清单里没有这两个，但它的一致性保证第 4 条（"永久聚合不会
    # 因重放重复累计"）和第 9 条（"清理不会误删 dedupe 身份"）离开它们没法实现。

    def remember_identity(self, identity: DurableDedupeIdentity) -> bool:
        """记住"这条我处理过了"。已经记过返回 ``False``。"""
        ...

    def has_seen_identity(
        self, *, subject_id: str, signal: str, source: str, digest: str,
    ) -> bool:
        """这条处理过没有。明细已按保留期删掉之后，这是唯一还能回答的东西。"""
        ...

    def list_identities(
        self, *, subject_id: str, signal: str, source: str, fact_key: str,
    ) -> Sequence[DurableDedupeIdentity]:
        """Return this Fact's revisions plus unmapped legacy identities in scope.

        Fact revision metadata must survive detail retention. Old opaque digests
        have fact_key=None; never silently omit them or infer their source time
        from an incoming upload. Adapters should index fact_key and the unmapped
        subset, not scan all permanent identities for every new observation.
        """
        ...

    def backfill_identity(self, identity: DurableDedupeIdentity) -> None:
        """Attach recovered Fact metadata to an existing legacy identity atomically.

        Only absent metadata may be filled; conflicting existing metadata must
        raise and roll back. It must not create an unseen delivery identity.
        """
        ...

    # -- 来源镜像 --------------------------------------------------------

    def get_sync_state(
        self, *, subject_id: str, source: str, collection_kind: str,
    ) -> SourceSyncState | None:
        ...

    def put_sync_state(self, state: SourceSyncState) -> None:
        ...

    def upsert_calendar_events(
        self, *, subject_id: str, events: Sequence[CalendarEventMirror],
    ) -> None:
        ...

    def upsert_reminders(
        self, *, subject_id: str, items: Sequence[ReminderItemMirror],
    ) -> None:
        ...

    def list_calendar_events(
        self, *, subject_id: str,
        start: datetime | None = None, end: datetime | None = None,
        limit: int = 50, offset: int = 0,
    ) -> Sequence[CalendarEventMirror]:
        """镜像里现在还存在的日程，按开始时间排序。

        产品规范的端口清单里只有写入没有读取 —— 但读取侧要用，不给它一个
        端口方法，实现就只能去摸具体存储的内部结构，换个宿主就静默返回空。

        🔴 ``offset`` 必须真的下推到存储。分页的语义是"一页最多这么多"，
        不是"这个人最多只能看到这么多" —— 读一批固定上限回来再在内存里切页，
        游标就只在那一批里打转，第 N+1 条**永远**取不到，而且不报错：
        用户看到的是"我八月没有日程"，不是"结果被截断了"。
        """
        ...

    def list_reminders(
        self, *, subject_id: str, include_completed: bool = False,
        limit: int = 50, offset: int = 0,
    ) -> Sequence[ReminderItemMirror]:
        """镜像里现在还存在的提醒事项。``offset`` 的要求同上。"""
        ...

    def record_retraction(self, retraction: "Retraction") -> bool:
        """记下来源撤回了哪条事实。已经记过返回 ``False``。

        **只追加，不就地删除观测。** 撤回是一条新事实（"那条不作数了"），
        不是把旧事实抹掉 —— 抹掉的话"这天为什么有个缺口"就再也答不出来，
        而 agent 需要能说"那天曾经有条记录，后来被来源删了"。

        必须**幂等**：同一条撤回被观察到两次（重传、崩溃重放）不能算两次。

        🔴 和它引发的后续动作在**同一个事务**里：当前值重选、受影响日期
        重算。分开提交的话会出现"撤回记下了但当前值还显示着被删的数值"，
        而下一轮不会去修 —— 它以为上一轮成功了。
        """
        ...

    def scrub_event_snapshots(
        self, *, subject_id: str, signal: str,
        source: str, source_event_id: str,
    ) -> int:
        """把被撤回那条事实触发过的事件里的**原值**抹掉，返回改了几条。

        用户在健康 app 里删掉一条体重之后，"体重 72kg 触发了涨重提醒"
        这条记录里的 72 也不该再留着 —— 那是"删除不再提供原值"的延伸
        （hx 2026-09-17 拍板）。

        **记录本身留着**：只把 ``fact_snapshot`` 里的数值换成"已删除"的标记，
        整条删掉的话"这条提醒当初为什么发"就再也解释不清了。
        已经投递出去的消息不回收 —— 那是已经发生的事。

        ⚠️ **可选方法。** 宿主没实现时 kit 跳过并照常完成撤回的其余部分 ——
        那等于"事件记录里的旧值还留着"，是个已知缺口，不是故障。
        """
        ...

    def list_retractions(
        self, *, subject_id: str, signal: str,
        source_event_ids: Sequence[str] | None = None,
    ) -> Sequence["Retraction"]:
        """这个用户这个信号上，哪些源事实被撤回了。

        重算要用：折当天的聚合时，被撤回的那些观测不能算进去。

        🔴 **返回的 Retraction 带着 source，调用方必须按 (source, id) 比对。**
        只按 id 比会连坐：同一个 subject 下 iOS 和 Google 完全可能用同一个
        source_event_id —— 撤回 iOS 那条，Google 那条也跟着从当前值和聚合里
        消失，而用户只会发现"我的体重记录凭空少了一条"。
        """
        ...

    def delete_source_items(
        self, *, subject_id: str, source: str, collection_kind: str,
        deleted_items: Sequence["DeletedItem"],
    ) -> int:
        """删掉来源**明确说删了**的那几条，返回删了几条。

        和 :meth:`apply_source_snapshot` 是两件事，别合并：

            全量收尾   "覆盖范围内、这轮没见到的" —— 推断出来的，所以只有
                       全量有资格，而且必须限定在声明的范围内
            这个方法   "来源说这条删了" —— 确定的事实，增量也必须执行

        没有这个方法的话，增量同步只能选：要么一条都不删（用户在手机上
        删掉的日程，在 agent 眼里永远还在，还会一直出现在"接下来有什么
        安排"里），要么拿局部列表当全量删（更糟，且不可逆）。

        🔴 **范围是完整的五段**：subject + source + account + collection +
        item id。少任何一层都会命中同名的兄弟条目 ——

            少 source      一次 ios 的删除命中 Google 里同 id 的条目
            少 account     删掉工作账户的一个会，私人日历里同 id 的安排一起没
            少 collection  同一账户下两个日历撞 id 时一起没

        每一种都不可逆，而且用户只会发现"我的日程凭空少了"。
        """
        ...

    def apply_source_snapshot(
        self, *, subject_id: str, source: str, collection_kind: str,
        sync_id: str, coverage_start: datetime, coverage_end: datetime,
        snapshot_kind: str,
    ) -> int:
        """全量同步收尾：删掉**覆盖范围内**这轮没见到的条目，返回删了几条。

        🔴 ``coverage_start`` / ``coverage_end`` 是硬边界。拿一个局部窗口去删
        窗口外的数据，是同步实现最容易犯的错，而且删完不可逆 —— 用户会发现
        自己去年的日程凭空消失了。

        ``snapshot_kind`` 不是 ``full`` 时，这个方法必须什么都不删。
        """
        ...

    # -- 规则状态 --------------------------------------------------------

    def get_rule_state(
        self, *, subject_id: str, definition_id: str, scope_key: str,
    ) -> dict[str, Any] | None:
        ...

    def put_rule_state(
        self, *, subject_id: str, definition_id: str, scope_key: str,
        state: dict[str, Any],
    ) -> None:
        """写规则状态。

        **必须和 ``enqueue_event`` 在同一个事务里。** 分开的话，可能出现
        "状态说已经触发过了，但事件没进发件箱" —— 那这次触发就永远丢了，
        而且规则要等到下一个 scope 才会 rearm。
        """
        ...

    # -- 事件与投递 ------------------------------------------------------

    def enqueue_event(self, entry: EventOutboxEntry) -> bool:
        """把事件写进待发件箱。同 ``event_id`` 已存在时返回 ``False``。

        **提交成功那一刻，事件就丢不了了。** 之后崩多少次都能重投。
        """
        ...

    def claim_pending_event(
        self, *, worker_id: str, now: datetime, lease_seconds: float,
    ) -> EventOutboxEntry | None:
        """领一个待投递的事件，拿一个到期的租约。

        必须原子地做三件事：挑一个 ``pending``（或租约已过期的 ``claimed``）、
        置为 ``claimed``、写上 ``lease_owner`` 和 ``lease_expires_at``。

        租约过期能被别人接管，是因为原持有者可能已经死了；而"到期才接管"
        保证了正常情况下同一个事件同时只有一个 worker 在处理。
        """
        ...

    def record_wake_receipt(
        self, *, receipt: WakeReceipt, next_state: str,
        claim_token: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """存回执并推进投递状态。返回 ``False`` 表示令牌过期、状态未改。

        **必须和"兑现或释放冷却额度占位"在同一个事务里。** 分开的话，
        "已送达但额度没扣"和"额度扣了但状态还是 pending"两种错都会出现，
        后者更糟：用户被打扰了两次。

        **``claim_token`` 对不上时只能记审计，不能改状态。** 旧 worker 租约
        过期、事件被别人接管之后它才返回 —— 让它推进状态，等于一次超时
        变成一次错误的覆盖，而且看起来完全正常。
        """
        ...

    def list_pending_events(
        self, *, subject_id: str | None = None, limit: int = 100,
    ) -> Sequence[EventOutboxEntry]:
        """列出还没送达的事件。给宿主的 worker 和 backlog 告警用。

        **只给 worker 用。** 排查要看的是 suppressed / rejected 这些终态，
        那些事件按定义不在这里 —— 排查走 :meth:`list_events`。
        """
        ...

    def list_events(
        self, *, subject_id: str,
        delivery_states: Sequence[str] | None = None,
        event_type: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
        limit: int = 50, offset: int = 0,
    ) -> Sequence[EventOutboxEntry]:
        """**任何投递状态**的事件，按 ``occurred_at`` 倒序（新的在前）。

        这是"为什么没提醒我"的唯一答案来源。那个问题的答案通常**不是**
        pending，而是 suppressed（撞了安静时段/冷却）或 rejected（宿主拒了）
        —— 而这两种恰好都是终态，用 :meth:`list_pending_events` 一条都看不到，
        看上去就像这个事件"压根没产生过"，排查直接走进死胡同。

        三个筛选条件（状态 / 类型 / 时间窗）和 ``offset`` **都必须下推到存储**。
        取一批回来再在内存里筛，等于"只在最近这批里找 suppressed"，
        找不到不代表没有。
        """
        ...

    # -- 用户数据 --------------------------------------------------------

    def purge_subject(self, *, subject_id: str) -> dict[str, int]:
        """删掉这个用户的全部数据，返回各类删了多少条。

        必须覆盖：观测、当前值、聚合、来源镜像、同步状态、去重身份、
        规则状态、待发件箱、回执。**漏一类就是删不干净**，而"删除我的数据"
        这件事没有部分成功。
        """
        ...


__all__ = ["StoragePort"]
