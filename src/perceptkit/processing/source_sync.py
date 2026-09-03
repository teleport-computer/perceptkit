"""来源镜像的同步编排 —— 日历和提醒走的那条路。

写入侧原本只有四个原语（``upsert_calendar_events`` / ``upsert_reminders`` /
``apply_source_snapshot`` / ``put_sync_state``），没有和 ``ingest()`` 对等的
公开入口。于是每个宿主自己拼：

    收到来源数据 → 解析身份 → upsert → 写同步状态 → 全量时按范围删 → 处理失败

**这条路上的每个坑，错了都不报错，而且大多不可逆。** 那正是它该收进 kit 的理由：

    增量当全量删       拿一个局部窗口去删窗口外的数据 —— 用户发现自己去年的
                       日程凭空消失了，而系统一切正常
    失败还推进游标     这一批没拿到，游标却往前走了 —— 那段数据**永远**不会
                       再被同步一次，而且没有任何地方记得它缺过
    失败还删条目       来源临时不可达被当成"来源侧删光了"
    删了但没提交       镜像里少一批条目、同步状态却说这轮成功了；
                       下一轮增量同步不会补，因为它以为上一轮是完整的
    没有覆盖范围就删   "全量"是相对于某个范围说的。没有范围的全量删除，
                       删的是这个用户在这个来源下的**全部**条目

所以这个模块只做一件事：把这几条规则变成一条路，而不是六个宿主各写一遍。

**它不解析来源格式。** 苹果日历、Google、Exchange 的条目长得完全不一样，
翻译成 ``CalendarEventMirror`` / ``ReminderItemMirror`` 是宿主的活 ——
kit 只管拿到标准条目之后的顺序和边界。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Sequence

from ..contracts.context import IngestContext
from ..contracts.records import (
    CalendarEventMirror,
    ReminderItemMirror,
    SourceSyncState,
)
from ..ports.storage import StoragePort

#: 全量：这一批覆盖范围内的条目就是来源的全部，范围内没见到的可以删。
FULL = "full"
#: 增量：只带了变化的部分，**一条都不许删**。
INCREMENTAL = "incremental"

CALENDAR = "calendar"
REMINDERS = "reminders"

#: 集合种类 -> 它唯一接受的条目类型。
_ITEM_TYPE = {CALENDAR: CalendarEventMirror, REMINDERS: ReminderItemMirror}


class SyncContractError(ValueError):
    """这一批的声明本身自相矛盾，处理它会造成不可逆的损失。

    刻意抛异常而不是返回一个"部分成功" —— 这几种情况下继续做的代价是删掉
    用户的真实数据，而那没有"部分"可言。
    """


@dataclass
class SyncBatch:
    """一批来源镜像数据，以及它自己声明的边界。

    ``coverage_start`` / ``coverage_end`` 对 ``full`` 是**必填**：
    "全量"永远是相对于某个范围说的，没有范围的全量删除删的是全部。
    """

    source: str
    collection_kind: str
    sync_id: str
    snapshot_kind: str = INCREMENTAL
    items: Sequence[Any] = field(default_factory=tuple)
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    cursor: str | None = None
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    #: 来源**明确说**被删掉的条目身份。见 ``sync_source_mirror`` 的文档。
    #:
    #: 和「这一批里没出现」是两回事：没出现推断不出删除（增量只知道变了什么，
    #: 不知道还剩什么），但来源的 change feed 明确传来一条删除时，
    #: 那是确定的事实，必须执行 —— 否则用户在手机上删掉的日程，
    #: 在 agent 眼里永远还在。
    deleted_item_ids: Sequence[str] = field(default_factory=tuple)
    #: 非空 = 这一批**没有成功拿到**。见 ``SyncOutcome`` 的文档。
    error_code: str | None = None


@dataclass
class SyncOutcome:
    """这一轮做了什么。``failed`` 时 ``upserted`` / ``deleted`` 一定是 0。"""

    upserted: int = 0
    #: 全量收尾按范围删掉的条数。
    deleted: int = 0
    #: 按来源明确的 tombstone 删掉的条数。和上面一个分开数 —— 一个是
    #: "这轮没见到所以删"，一个是"来源说删了"，混在一起就分不清
    #: 某次异常删除是范围判断出错还是来源真的删了。
    tombstoned: int = 0
    failed: bool = False
    #: 记下来的错误码，原样来自这一批。
    error_code: str | None = None
    #: 游标推进到哪了。失败时**保持原值**，不是 None —— 置空等于让下一轮
    #: 从头拉一遍，而那对一个只是临时不可达的来源是纯粹的浪费。
    cursor: str | None = None


def _existing_cursor(storage: StoragePort, ctx: IngestContext,
                     batch: SyncBatch) -> tuple[str | None, datetime | None]:
    prior = storage.get_sync_state(
        subject_id=ctx.subject_id, source=batch.source,
        collection_kind=batch.collection_kind,
    )
    if prior is None:
        return None, None
    return prior.sync_cursor, prior.last_successful_sync_at


def sync_source_mirror(
    storage: StoragePort, batch: SyncBatch, *, context: IngestContext,
) -> SyncOutcome:
    """把一批来源镜像数据落进去，并把同步状态推到该有的位置。

    顺序是固定的，而且**整个包在一个事务里**：写条目、按范围删、写同步状态
    要么全都成立、要么全都不成立。分开提交的话，"镜像里少一批条目、
    同步状态却说这轮成功了"就会出现，而下一轮增量同步不会去补 ——
    它以为上一轮是完整的。
    """
    kind = batch.snapshot_kind or INCREMENTAL
    if kind not in (FULL, INCREMENTAL):
        raise SyncContractError(
            f"snapshot_kind={kind!r}：只有 {FULL!r} 和 {INCREMENTAL!r} 两种。"
            f"不认识的种类不能当成增量放过去 —— 万一它的本意是全量，"
            f"该删的没删，镜像会一直留着来源已经删掉的条目"
        )
    if kind == FULL and batch.error_code is None and (
            batch.coverage_start is None or batch.coverage_end is None):
        raise SyncContractError(
            "全量同步必须给 coverage_start 和 coverage_end。"
            "「全量」永远是相对于某个范围说的 —— 没有范围的全量删除，"
            "删的是这个用户在这个来源下的全部条目，且不可逆"
        )
    if (batch.coverage_start is not None and batch.coverage_end is not None
            and batch.coverage_start > batch.coverage_end):
        raise SyncContractError(
            f"coverage_start({batch.coverage_start}) 晚于 "
            f"coverage_end({batch.coverage_end})：这个范围是空的，"
            f"照它删会删掉范围外的一切或者什么都不删，两种都不该猜"
        )

    prior_cursor, prior_ok_at = _existing_cursor(storage, context, batch)
    now = batch.completed_at or batch.attempted_at or context.received_at

    # ── 失败的一批：记下来，但**什么都不动** ──────────────────────────
    #
    # 不 upsert（这一批的内容不可信）、不删（来源临时不可达不等于来源侧删光了）、
    # 不推进游标（推进了那段数据就永远不会被再同步一次，而且没有任何地方
    # 记得它缺过）、不动 last_successful_sync_at（否则"日历数据已过期"
    # 这个判断永远为假）。
    if batch.error_code is not None:
        with storage.transaction():
            storage.put_sync_state(SourceSyncState(
                subject_id=context.subject_id,
                source=batch.source,
                collection_kind=batch.collection_kind,
                sync_cursor=prior_cursor,
                coverage_start=batch.coverage_start,
                coverage_end=batch.coverage_end,
                snapshot_kind=kind,
                last_attempted_at=batch.attempted_at or now,
                last_successful_sync_at=prior_ok_at,
                last_error_code=batch.error_code,
            ))
        return SyncOutcome(failed=True, error_code=batch.error_code,
                           cursor=prior_cursor)

    with storage.transaction():
        upserted = _upsert(storage, batch, context)
        # 来源明确的删除，全量和增量都执行 —— 它不是推断出来的。
        tombstoned = _apply_tombstones(storage, batch, context)
        deleted = 0
        if kind == FULL:
            # 只有全量才有资格删，而且只在它自己声明的范围内。
            deleted = int(storage.apply_source_snapshot(
                subject_id=context.subject_id,
                source=batch.source,
                collection_kind=batch.collection_kind,
                sync_id=batch.sync_id,
                coverage_start=batch.coverage_start,
                coverage_end=batch.coverage_end,
                snapshot_kind=kind,
            ) or 0)
        storage.put_sync_state(SourceSyncState(
            subject_id=context.subject_id,
            source=batch.source,
            collection_kind=batch.collection_kind,
            sync_cursor=batch.cursor if batch.cursor is not None else prior_cursor,
            coverage_start=batch.coverage_start,
            coverage_end=batch.coverage_end,
            snapshot_kind=kind,
            last_attempted_at=batch.attempted_at or now,
            last_successful_sync_at=now,
            # 成功了就把上一次的错误清掉 —— 留着的话，一次早就恢复的
            # 故障会一直挂在状态里，看的人分不清是历史还是现在。
            last_error_code=None,
        ))
    return SyncOutcome(upserted=upserted, deleted=deleted,
                       tombstoned=tombstoned,
                       cursor=batch.cursor if batch.cursor is not None
                       else prior_cursor)


def _apply_tombstones(storage: StoragePort, batch: SyncBatch,
                      context: IngestContext) -> int:
    """执行来源**明确传来**的删除。

    这和全量收尾的删除是两件事，别合并：

        全量收尾   "覆盖范围内、这轮没见到的" —— 推断出来的，所以只有全量
                   有资格，而且必须限定在声明的范围内
        tombstone  "来源说这条删了" —— 确定的事实，增量也必须执行

    早先把增量定义成「一条都不许删」，防住了「拿局部列表当全量」，
    但同时也堵死了这条：用户在手机上删掉的日程，在 agent 眼里永远还在，
    而且它会一直出现在"接下来有什么安排"里。
    """
    ids = [str(i) for i in (batch.deleted_item_ids or ()) if str(i).strip()]
    if not ids:
        return 0
    return int(storage.delete_source_items(
        subject_id=context.subject_id, source=batch.source,
        collection_kind=batch.collection_kind, source_item_ids=ids,
    ) or 0)


def _upsert(storage: StoragePort, batch: SyncBatch,
            context: IngestContext) -> int:
    items = list(batch.items)
    if not items:
        return 0
    # 🔴 **这一批的条目必须打上这一批的 sync_id。**
    #
    # 全量收尾删的是「覆盖范围内、这轮没见到的」，判据就是这个字段。
    # 不打的话，刚写进去的条目在同一个事务里被自己的快照收尾删掉 ——
    # 一次"成功"的全量同步，结果是镜像空了。
    # 这个由 kit 打，不指望宿主记得：忘了不报错，只是数据没了。
    # 这一批声明的 source 和 sync_id 由 kit 盖上去，不要求宿主在每个条目上
    # 再写一遍。两者忘了都不报错，但后果不一样：
    #   sync_id 忘了 → 刚写进去的被自己的快照收尾删掉
    #   source  忘了 → 这批条目落在别的来源名下，下次那个来源的全量同步删掉它们
    # 🔴 subject 一律用**可信上下文**的，不用条目自己带的那个。
    #
    # 条目是宿主从来源数据翻译出来的，它带的 subject_id 最好的情况是冗余、
    # 最坏的情况是把 A 的日程写进 B 的花园。可信 subject 只有一个来源：
    # IngestContext —— 那是宿主鉴权之后填的，不经过来源数据也不经过模型。
    # 校验一致再拒绝也行，但覆盖更彻底：没有"该信哪个"这个问题存在。
    items = [replace(i, subject_id=context.subject_id, source=batch.source,
                     last_seen_sync_id=batch.sync_id)
             for i in items]
    kinds = {type(i) for i in items}
    if len(kinds) > 1:
        # 混着来说明宿主那边的翻译出了问题。挑一部分写进去、剩下的丢掉，
        # 会得到一份"成功了"的半份镜像。
        raise SyncContractError(
            f"一批里混了 {sorted(k.__name__ for k in kinds)}："
            f"一次同步只处理一种集合，混着来会写出一份看起来成功的半份镜像"
        )
    # 🔴 声明的集合种类必须和条目类型对上。
    #
    # 不校验的话，「collection_kind=reminders + 一批日历条目」会被照单全收：
    # 日历表被写进去了，而**提醒的同步游标往前推进了** —— 数据和游标从此
    # 互相矛盾，下一轮增量提醒同步会以为上一轮成功了，那段提醒永远补不回来。
    expected = _ITEM_TYPE.get(batch.collection_kind)
    if expected is None:
        raise SyncContractError(
            f"不认识的 collection_kind={batch.collection_kind!r}："
            f"只有 {CALENDAR!r} 和 {REMINDERS!r}。放过去的话，"
            f"这批数据会落在一个没人读的地方，而同步状态说成功了"
        )
    actual = kinds.pop()
    if actual is not expected:
        raise SyncContractError(
            f"collection_kind={batch.collection_kind!r} 声明的是 "
            f"{expected.__name__}，实际给的是 {actual.__name__}："
            f"照做会把数据写进一张表、把另一张表的游标往前推"
        )
    if expected is CalendarEventMirror:
        storage.upsert_calendar_events(subject_id=context.subject_id,
                                       events=items)
    else:
        storage.upsert_reminders(subject_id=context.subject_id, items=items)
    return len(items)


__all__ = [
    "CALENDAR", "REMINDERS", "FULL", "INCREMENTAL",
    "SyncBatch", "SyncOutcome", "SyncContractError", "sync_source_mirror",
]
