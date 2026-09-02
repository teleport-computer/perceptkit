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


@runtime_checkable
class StoragePort(Protocol):
    """宿主的存储适配器。

    方法分六组：批级幂等 / 观测 / 当前值 / 聚合 / 来源镜像 / 事件与投递。
    """

    # -- 事务 ------------------------------------------------------------

    def transaction(self) -> ContextManager[None]:
        """一个原子边界。

        kit 会把"写规则状态 + 写待发件箱"这类**必须一起成功**的操作包在
        同一个 ``with`` 里。宿主如果做不到真正的单事务，必须提供可证明的
        补偿/对账机制，并在一致性测试里证明它 —— 不能默认它不会出问题。

        产品规范这里留了活口（"处于同一原子边界，**或有可证明的恢复机制**"），
        所以不强制单事务，但强制"能证明"。
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
        旧统计的语义**。
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
