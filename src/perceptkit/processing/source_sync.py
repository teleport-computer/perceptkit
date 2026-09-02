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
    #: 非空 = 这一批**没有成功拿到**。见 ``SyncOutcome`` 的文档。
    error_code: str | None = None


@dataclass
class SyncOutcome:
    """这一轮做了什么。``failed`` 时 ``upserted`` / ``deleted`` 一定是 0。"""

    upserted: int = 0
    deleted: int = 0
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
                       cursor=batch.cursor if batch.cursor is not None
                       else prior_cursor)


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
    items = [replace(i, last_seen_sync_id=batch.sync_id) for i in items]
    kinds = {type(i) for i in items}
    if kinds == {CalendarEventMirror}:
        storage.upsert_calendar_events(subject_id=context.subject_id,
                                       events=items)
    elif kinds == {ReminderItemMirror}:
        storage.upsert_reminders(subject_id=context.subject_id, items=items)
    else:
        # 混着来说明宿主那边的翻译出了问题。挑一部分写进去、剩下的丢掉，
        # 会得到一份"成功了"的半份镜像。
        raise SyncContractError(
            f"一批里混了 {sorted(k.__name__ for k in kinds)}："
            f"一次同步只处理一种集合，混着来会写出一份看起来成功的半份镜像"
        )
    return len(items)


__all__ = [
    "CALENDAR", "REMINDERS", "FULL", "INCREMENTAL",
    "SyncBatch", "SyncOutcome", "SyncContractError", "sync_source_mirror",
]
