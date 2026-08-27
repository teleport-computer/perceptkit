"""逻辑存储对象 —— 宿主必须能映射出来的东西。

**这些是逻辑职责，不是表结构。** 一项不必对应一张 SQL 表：宿主可以合并、
可以用文档数据库、可以用 SQLite。但必须能满足同样的查询、幂等、重算、
删除和一致性语义 —— 这一点由一致性测试来证明，不靠自觉。

八个对象，各自回答一个问题：

    StoredObservation      发生过什么（唯一的事实来源，其余都是它的派生）
    CurrentProjection      现在是什么状态
    DailyAggregate         这一天/这一段汇总起来是什么
    CalendarEventMirror    外部日历现在有哪些条目
    ReminderItemMirror     外部提醒现在有哪些条目
    SourceSyncState        跟外部来源同步到哪儿了
    DurableDedupeIdentity  这条我处理过没有（明细删了也要能回答）
    EventOutboxEntry       哪些事件还没送到

``IngestReceipt`` / ``WakeReceipt`` 在 :mod:`~perceptkit.contracts.receipt`，
``EventDefinition`` / ``EventRuleState`` 属于规则层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import delivery

# ---------------------------------------------------------------------------
# 事实
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoredObservation:
    """一条落库的标准观测。

    唯一身份推荐 ``(subject_id, source, signal, source_event_id)``；
    上游给不了稳定 id 的，由 manifest 声明确定性 identity 策略。

    **这是唯一的事实来源。** current 是它的投影、日聚合是它的派生，
    两者都能从它重算，反过来不行 —— 聚合是压缩过的，压缩不可逆。
    """

    observation_id: str
    subject_id: str
    signal: str
    signal_schema_version: int
    source: str
    occurred_at: datetime
    received_at: datetime
    availability: str
    #: 按发生时的本地时区归到哪一天。跨时区飞行后**不重排历史** ——
    #: 8 月 20 日永远是"上海时间的 8 月 20 日"。
    effective_local_date: date
    typed_value: dict[str, Any] | None = None
    timezone: str | None = None
    source_event_id: str | None = None
    source_revision: str | int | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# 当前值
# ---------------------------------------------------------------------------

#: 新观测该不该替换掉当前值的三种结论。
REPLACE = "replace"
IGNORE = "ignore"
CONFLICT = "conflict"


@dataclass(frozen=True)
class CurrentProjection:
    """某个 subject / signal / 维度的当前值。

    唯一身份 ``(subject_id, signal, dimension_key)``。``dimension_key`` 用于
    同一信号下有多个并列实体的情况（每个 Wi-Fi 锚点一条、每个健康指标一条）；
    没有这种情况的信号固定用信号名。
    """

    subject_id: str
    signal: str
    dimension_key: str
    typed_value: dict[str, Any] | None
    availability: str
    observed_at: datetime
    received_at: datetime
    #: 超过这个时刻就不能再叫"当前" —— 但仍可作为带 ``as_of`` 的 last known 返回。
    expires_at: datetime | None = None
    source_observation_id: str | None = None
    source_revision: str | int | None = None
    #: 乐观并发用的版本号。宿主的 compare-and-put 靠它。
    version: int = 0
    #: 内容摘要。用来分辨"同一时刻的重传"和"同一时刻的不同内容"。
    content_digest: str | None = None


def _compare_revisions(new: str | int | None, old: str | int | None) -> int | None:
    """比较两个 revision。返回 ``1 / 0 / -1``，**比不了时返回 ``None``**。

    比不了就说比不了，不要编一个顺序出来。之前的写法给"任何字符串都大于
    任何整数"、字符串之间按字典序 —— 那是**稳定**，不是**正确**：
    ``"10" < "2"``，而 HealthKit 的 revision 恰恰可能是数字字符串。
    编出来的顺序会让一次错误的覆盖看起来完全正常。

    比不了的情况交给调用方当 conflict 处理，由人或宿主决定。
    """
    if new is None and old is None:
        return 0
    if old is None:
        return 1                     # 有版本的比没版本的新
    if new is None:
        return -1

    def as_int(v: Any) -> int | None:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v)
        return None

    a, b = as_int(new), as_int(old)
    if a is not None and b is not None:
        return (a > b) - (a < b)
    if isinstance(new, str) and isinstance(old, str):
        # 都是不可解释的字符串(etag 之类)：只能判等,判不了大小。
        return 0 if new == old else None
    return None


def decide_current_update(
    *,
    new_occurred_at: datetime,
    new_revision: str | int | None,
    new_digest: str | None,
    existing: CurrentProjection | None,
) -> str:
    """新观测该 ``REPLACE`` / ``IGNORE`` / 还是报 ``CONFLICT``。

    比较键是 **(occurred_at, source_revision)**，不是只有 occurred_at。

    产品规范这里有个缺口：§7.3 说"只有 occurred_at 更新的数据才能覆盖 Current"，
    但 §4.2 定义了 ``source_revision``、§5.5 又要求"来源侧样本修订应更新对应
    canonical sample"。两条组合起来，**同一时刻的纠错版本永远覆盖不了当前值** ——
    用户在健康 App 里改掉一个误录的体重，我们这边还显示旧的。
    （见 OPEN-QUESTIONS B15，已提给产品方确认。）

    规则：

        更晚的 occurred_at                    → replace
        同 occurred_at + 更高 revision        → replace（这就是修订）
        同 occurred_at + 同 revision + 同内容 → ignore（就是重传）
        同 occurred_at + 同 revision + 异内容 → conflict（不能静默挑一个）
        更早的 occurred_at                    → ignore（迟到数据进历史，不动当前值）
    """
    if existing is None:
        return REPLACE
    if new_occurred_at > existing.observed_at:
        return REPLACE
    if new_occurred_at < existing.observed_at:
        return IGNORE

    order = _compare_revisions(new_revision, existing.source_revision)
    if order is None:
        # 版本形态对不上(一个整数一个 etag、或两个不可解释的字符串各不相同)。
        # 编一个顺序出来会让一次错误的覆盖看起来完全正常。
        return CONFLICT
    if order > 0:
        return REPLACE
    if order < 0:
        return IGNORE

    # 同时刻、同版本：只可能是重传，或者两个来源打架。
    if new_digest is not None and existing.content_digest is not None:
        return IGNORE if new_digest == existing.content_digest else CONFLICT
    # 摘要缺失时不敢断言是重传 —— 静默覆盖会让"到底哪份生效了"永远说不清。
    return CONFLICT


# ---------------------------------------------------------------------------
# 派生
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DailyAggregate:
    """某一天（或某个窗口）的派生统计。

    唯一身份 ``(subject_id, signal, local_date, aggregation_kind, aggregation_version)``。

    ``aggregation_version`` 是为算法升级准备的：改了口径就换版本号重算，
    **不原地改写旧统计的语义** —— 否则同一张表里的历史数据一半是老口径、
    一半是新口径，而且看不出来。
    """

    subject_id: str
    signal: str
    local_date: date
    aggregation_kind: str
    aggregation_version: int
    typed_aggregate: dict[str, Any]
    #: 归属用的时区。跨时区之后旧记录保持原时区，不重排。
    timezone_attribution: str | None = None
    #: 这个聚合覆盖了哪些观测（数量、时间范围）。重算时用来判断完整性。
    source_coverage: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# 外部来源镜像
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarEventMirror:
    """外部日历里现在还存在的一条日程。

    **镜像不是快照历史。** 存的是"来源现在有哪些条目"，条目自己带着
    过去或未来的时间；不存"我们每次同步时看到了什么"。来源删除 → 本地删除。

    唯一身份必须**包含来源账户和日历**：不同账户碰巧用同一个 event id
    是完全可能的。
    """

    subject_id: str
    source_account_id: str
    source_calendar_id: str
    source_event_id: str
    event_fields: dict[str, Any]
    source_revision: str | int | None = None
    #: 重复日程的系列身份。无限重复的日程存规则，不展开到无限未来。
    recurrence_identity: str | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    #: 最后一次在哪轮同步里见过它。用来判断"这次全量同步没见到 = 来源删了"。
    last_seen_sync_id: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ReminderItemMirror:
    """外部提醒里现在还存在的一条待办。"""

    subject_id: str
    source_account_id: str
    source_list_id: str
    source_reminder_id: str
    reminder_fields: dict[str, Any]
    source_revision: str | int | None = None
    last_seen_sync_id: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SourceSyncState:
    """跟某个外部来源同步到哪儿了。

    ``coverage_start`` / ``coverage_end`` 是这次同步**明确覆盖**的范围。
    全量同步只能删除这个范围内消失的条目 —— 拿一个局部窗口去删窗口外的数据，
    是同步实现最容易犯的错，而且删完不可逆。

    ``last_successful_sync_at`` 必须可查询：同步长期失败时，应该显示
    "日历数据已过期"，而不是继续声称它是最新完整的。
    """

    subject_id: str
    source: str
    collection_kind: str
    sync_cursor: str | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    #: ``full`` 或 ``incremental``。全量才有资格删条目。
    snapshot_kind: str | None = None
    last_attempted_at: datetime | None = None
    last_successful_sync_at: datetime | None = None
    last_error_code: str | None = None


# ---------------------------------------------------------------------------
# 去重身份
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DurableDedupeIdentity:
    """明细已经按保留期删掉了，但永久聚合仍不能被旧数据重放重复累计。

    典型场景：照片单条明细只留 7 天，"每日新增几张"永久保存。第 8 天设备
    重放了一批旧上报 —— 明细查不到了，拿什么判断这批处理过？

    答案是一个**不可逆的指纹**：看不出是哪张照片（所以不敏感、可以永久留），
    只回答一个问题——见过没有。

    ``retain_until`` 为 ``None`` 表示永久。**清理任务绝不能删掉它所保护的
    永久聚合还在用的那些身份** —— 那会让重放静默地把数字加两遍。
    """

    subject_id: str
    signal: str
    source: str
    #: 上游身份的不可逆摘要。存摘要不存原 id：原 id 能反查到用户的相册/健康记录。
    source_event_identity_digest: str
    first_applied_at: datetime
    #: 它保护的是哪个聚合范围（如 ``daily_added_count``）。
    aggregate_scope: str | None = None
    retain_until: datetime | None = None


# ---------------------------------------------------------------------------
# 待投递
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventOutboxEntry:
    """一个已经落地、但还没确认送达的事件。

    **先落地再投递**是这套东西唯一的可靠性保证：走到这条记录被提交那一刻，
    事件就丢不了了，之后崩多少次都能重投。

    ``budget_reservation_id`` 是冷却额度的**占位**，不是消耗。它在
    ``claimed`` 时创建、``delivered`` 时兑现、其余终态释放 —— 产品规范只说了
    "accepted 之后提交额度"，没说 accepted 之前那段窗口怎么防并发重复投递。
    """

    event_id: str
    subject_id: str
    definition_id: str
    definition_version: int
    event_type: str
    occurred_at: datetime
    detected_at: datetime
    #: 事件的完整快照。规则后来被改被删，这个事件仍然解释得通。
    fact_snapshot: dict[str, Any]
    delivery_state: str = delivery.PENDING
    #: 同一件事的去重键。runtime 崩溃重投时靠它认出是同一个。
    dedupe_key: str | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    #: 当前租约的持有者和到期时间。到期没进展 → 别人可以接管。
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    #: 这一次认领的令牌。**每次认领都必须换一个新的。**
    #: 存回执时要比对：旧 worker 租约过期后回来，手里拿的是旧令牌，
    #: 不能推进新 worker 已经接管的记录 —— 否则一次超时会变成一次错误的
    #: 状态覆盖，而且看起来完全正常。
    claim_token: str | None = None
    budget_reservation_id: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.delivery_state not in delivery.DELIVERY_STATES:
            raise ValueError(
                f"delivery_state={self.delivery_state!r} 不在 "
                f"{sorted(delivery.DELIVERY_STATES)}"
            )

    @property
    def is_terminal(self) -> bool:
        return delivery.is_terminal(self.delivery_state)


__all__ = [
    "StoredObservation",
    "CurrentProjection", "REPLACE", "IGNORE", "CONFLICT", "decide_current_update",
    "DailyAggregate",
    "CalendarEventMirror", "ReminderItemMirror", "SourceSyncState",
    "DurableDedupeIdentity", "EventOutboxEntry",
]
