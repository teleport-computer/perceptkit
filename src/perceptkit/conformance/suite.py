"""一致性测试套件 —— 宿主用它证明自己的 adapter 是对的。

产品规范说得很准：宿主可以用任何数据库，**但需要证明能够满足相同的查询、
幂等、重算、删除和一致性语义**。这个模块就是那个"证明"。

用法（在宿主自己的测试里）::

    from perceptkit.conformance import run_storage_conformance

    def test_my_adapter_is_conformant():
        problems = run_storage_conformance(lambda: MyPostgresStorage(fresh_db()))
        assert not problems, "\\n".join(problems)

---

## 🔴 这套东西能证明什么、不能证明什么

**能证明**：端口语义对不对、调用顺序对不对、给同样的输入是不是给同样的结果。

**不能证明**（必须宿主另外做）：

    真正的事务边界      需要真实数据库 + 在关键写操作之间打断点，
                        然后【从另一条连接】观察：规则状态和发件箱
                        要么都旧/不存在，要么都提交
    并发下只有一个胜者   需要两条独立连接 + 同时发起，
                        断言同 report / 同 event / 新旧 current 只有一个赢
    崩溃恢复            需要模拟"wake 已 accepted、回执还没存下来"就断电

在内存实现上这三类**永远是绿的** —— 内存天然原子、天然无并发。
把它们当验过了，是这套东西最危险的用法。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ..contracts import delivery as _delivery
from ..contracts import receipt as _receipt
from ..contracts.records import (
    CalendarEventMirror,
    CurrentProjection,
    DailyAggregate,
    DurableDedupeIdentity,
    EventOutboxEntry,
    ReminderItemMirror,
    StoredObservation,
)
from ..contracts.receipt import WakeReceipt
from ..contracts.retraction import Retraction
from ..processing.source_sync import DeletedItem as _DeletedItem

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
DAY = date(2026, 8, 27)

StorageFactory = Callable[[], Any]


def _obs(**over: Any) -> StoredObservation:
    base: dict[str, Any] = dict(
        observation_id="obs_1", subject_id="u1", signal="steps",
        signal_schema_version=1, source="ios", occurred_at=T0, received_at=T0,
        availability="observed", effective_local_date=DAY,
        typed_value={"step_count": 100},
    )
    base.update(over)
    return StoredObservation(**base)


def _current(**over: Any) -> CurrentProjection:
    base: dict[str, Any] = dict(
        subject_id="u1", signal="steps", dimension_key="steps",
        typed_value={"step_count": 100}, availability="observed",
        observed_at=T0, received_at=T0, version=0, content_digest="d1",
    )
    base.update(over)
    return CurrentProjection(**base)


def _entry(**over: Any) -> EventOutboxEntry:
    base: dict[str, Any] = dict(
        event_id="evt_1", subject_id="u1", definition_id="d1", definition_version=1,
        event_type="t", occurred_at=T0, detected_at=T0, fact_snapshot={},
    )
    base.update(over)
    return EventOutboxEntry(**base)


# ---------------------------------------------------------------------------
# 十四条保证
# ---------------------------------------------------------------------------

def _g1_report_and_observation_idempotency(new: StorageFactory) -> list[str]:
    """① 同一批上报、同一条观测重传，都不重复处理。"""
    problems: list[str] = []
    s = new()
    first = s.claim_report(subject_id="u1", producer="ios", report_id="r1",
                           payload_digest="d1", received_at=T0)
    if first.status != _receipt.INGEST_ACCEPTED:
        problems.append("①: 第一次认领一批新上报应该 accepted")
    again = s.claim_report(subject_id="u1", producer="ios", report_id="r1",
                           payload_digest="d1", received_at=T0)
    if again.status != _receipt.INGEST_DUPLICATE:
        problems.append("①: 同 identity 同摘要重传应该 duplicate，不重复处理")

    s2 = new()
    if not s2.append_observation(_obs()):
        problems.append("①: 第一次写观测应该返回 True")
    if s2.append_observation(_obs()):
        problems.append("①: 同一个 observation_id 重复写应该返回 False 且不重复落库")
    from dataclasses import replace
    for status, code in (("conflict", "fact_conflict"),
                         ("rejected", "fact_revision_details_incomplete")):
        terminal_store = new()
        claim = terminal_store.claim_report(subject_id="u1", producer="ios", report_id="terminal",
                                            payload_digest="v2:terminal", received_at=T0)
        terminal_store.finalize_report(replace(claim, status=status, error_code=code))
        retry = terminal_store.claim_report(subject_id="u1", producer="ios", report_id="terminal",
                                            payload_digest="v2:terminal", received_at=T0)
        if retry.status != status or retry.error_code != code:
            problems.append("①: terminal report failure must survive identical retry")
    return problems


def _g2_old_does_not_overwrite_new(new: StorageFactory) -> list[str]:
    """② 迟到的旧观测不能覆盖更新的当前值。"""
    problems: list[str] = []
    s = new()
    s.compare_and_put_current(_current(observed_at=T0, version=0), expected_version=-1)
    stale = _current(observed_at=T0 - timedelta(hours=1),
                     typed_value={"step_count": 1}, version=1)
    # 用错误的版本号写 —— 应该被拒。真正的"旧不覆盖新"判断在 kit 的管线里，
    # 这里验的是端口有没有提供那个把手。
    if s.compare_and_put_current(stale, expected_version=99):
        problems.append("②: 版本号对不上时 compare_and_put_current 必须返回 False")
    got = s.get_current(subject_id="u1", signals=["steps"])["steps"]
    if got and got[0].typed_value != {"step_count": 100}:
        problems.append("②: 当前值被一次版本不匹配的写入改掉了")
    return problems


def _g3_same_identity_different_content_conflicts(new: StorageFactory) -> list[str]:
    """③ 同一个上报 identity、不同内容 —— 必须报冲突，不能静默覆盖。"""
    problems: list[str] = []
    s = new()
    s.claim_report(subject_id="u1", producer="ios", report_id="r1",
                   payload_digest="d1", received_at=T0)
    clash = s.claim_report(subject_id="u1", producer="ios", report_id="r1",
                           payload_digest="d2", received_at=T0)
    if clash.status != _receipt.INGEST_CONFLICT:
        problems.append(
            "③: 同 report_id 不同内容必须 conflict —— 静默挑一个覆盖会让"
            "「到底哪份数据生效了」永远说不清"
        )
    migrate = new()
    key = dict(subject_id="u1", producer="ios", report_id="legacy")
    migrate.claim_report(**key, payload_digest="old", received_at=T0)
    if migrate.backfill_report_digest(**key, expected_digest="wrong", payload_digest="v2:new"):
        problems.append("③: receipt backfill must reject wrong expected digest")
    if migrate.claim_report(**key, payload_digest="old", received_at=T0).status != "duplicate":
        problems.append("③: failed receipt backfill must not mutate original receipt")
    if not migrate.backfill_report_digest(**key, expected_digest="old", payload_digest="v2:new"):
        problems.append("③: receipt backfill must accept the matching original digest")
    if not migrate.backfill_report_digest(**key, expected_digest="old", payload_digest="v2:new"):
        problems.append("③: identical receipt backfill must be idempotent")
    if migrate.backfill_report_digest(**key, expected_digest="v2:new", payload_digest="v2:other"):
        problems.append("③: receipt backfill must not overwrite migrated semantic content")
    return problems


def _g4_permanent_aggregates_survive_replay(new: StorageFactory) -> list[str]:
    """④ 永久聚合不会因为旧数据重放而重复累计。"""
    problems: list[str] = []
    s = new()
    ident = DurableDedupeIdentity(
        subject_id="u1", signal="steps", source="ios",
        source_event_identity_digest="abc", first_applied_at=T0,
    )
    if not s.remember_identity(ident):
        problems.append("④: 第一次记住去重身份应该返回 True")
    if s.remember_identity(ident):
        problems.append("④: 重复记住同一个身份应该返回 False")
    if not s.has_seen_identity(subject_id="u1", signal="steps", source="ios",
                               digest="abc"):
        problems.append("④: 记过的身份必须查得到")
    # 关键：明细被保留期清理之后，身份仍然要在 —— 否则重放会把数字加两遍。
    s.append_observation(_obs())
    s.delete_observations(subject_id="u1", signal="steps",
                          before=T0 + timedelta(days=1))
    if not s.has_seen_identity(subject_id="u1", signal="steps", source="ios",
                               digest="abc"):
        problems.append(
            "④: 清理明细把去重身份一起删了 —— 旧数据重放会让永久聚合的数字"
            "加两遍，而且无法回滚"
        )
    return problems


def _g5_atomic_boundary_is_offered(new: StorageFactory) -> list[str]:
    """⑤ 端口提供了原子边界这个把手。

    🔴 **这一条只验"有没有提供"，验不出"真的原子"。** 真正的验证需要
    真实数据库、两条连接、在关键写操作之间打断点，然后从另一条连接观察。
    """
    problems: list[str] = []
    s = new()
    try:
        with s.transaction():
            s.append_observation(_obs())
    except Exception as exc:                       # noqa: BLE001
        problems.append(f"⑤: transaction() 不可用：{exc}")
    return problems


def _g6_event_is_durable_before_dispatch(new: StorageFactory) -> list[str]:
    """⑥ 事件在投递之前已经落地。"""
    problems: list[str] = []
    s = new()
    if not s.enqueue_event(_entry()):
        problems.append("⑥: 第一次入队应该返回 True")
    pending = s.list_pending_events()
    if len(pending) != 1 or pending[0].delivery_state != _delivery.PENDING:
        problems.append("⑥: 刚入队的事件应该处于 pending，且能被列出来")
    return problems


def _g7_delivery_is_idempotent_by_event_id(new: StorageFactory) -> list[str]:
    """⑦ 投递按 event_id 幂等；租约保证同时只有一个 worker 在处理。"""
    problems: list[str] = []
    s = new()
    s.enqueue_event(_entry())
    if s.enqueue_event(_entry()):
        problems.append("⑦: 同一个 event_id 重复入队应该返回 False")

    first = s.claim_pending_event(worker_id="w1", now=T0, lease_seconds=60)
    if first is None:
        problems.append("⑦: 应该能领到那个 pending 事件")
        return problems
    if s.claim_pending_event(worker_id="w2", now=T0, lease_seconds=60) is not None:
        problems.append(
            "⑦: 租约没到期时第二个 worker 不该领到同一个事件 —— "
            "两个都投出去，用户被提醒两次"
        )
    taken = s.claim_pending_event(worker_id="w2", now=T0 + timedelta(seconds=120),
                                  lease_seconds=60)
    if taken is None:
        problems.append("⑦: 租约到期后应该能被别的 worker 接管（原持有者可能已经死了）")
    return problems


def _g8_partial_sync_does_not_delete_outside_its_window(new: StorageFactory) -> list[str]:
    """⑧ 局部同步不会误删覆盖范围外的条目。"""
    problems: list[str] = []
    s = new()
    inside = CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id="e_in", event_fields={"start_at": T0},
        last_seen_sync_id="old",
    )
    outside = CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id="e_out", event_fields={"start_at": T0 - timedelta(days=400)},
        last_seen_sync_id="old",
    )
    s.upsert_calendar_events(subject_id="u1", events=[inside, outside])
    s.apply_source_snapshot(
        subject_id="u1", source="ios", collection_kind="calendar", sync_id="new",
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
        snapshot_kind="full",
    )
    # 🔴 用端口方法验，**不摸具体实现的内部属性**。
    #    先前这里读的是 InMemoryStorage 的 `.calendar` 字典 —— 换成任何
    #    真实现都读不到，于是 remaining 恒为空集，这一条对每个真 adapter
    #    都报一个假失败。一套"检查别人有没有做对"的工具，自己先得走公开接口。
    remaining = {
        e.source_event_id
        for e in s.list_calendar_events(subject_id="u1", limit=100)
    }
    if "e_out" not in remaining:
        problems.append(
            "⑧: 全量同步删掉了覆盖范围【外】的条目 —— 用户会发现自己去年的"
            "日程凭空消失，而且不可逆"
        )
    if "e_in" in remaining:
        problems.append("⑧: 覆盖范围内这轮没见到的条目应该被删掉")

    # 增量同步没有资格删任何东西：它只知道"变了什么"，不知道"还剩什么"。
    s2 = new()
    s2.upsert_calendar_events(subject_id="u1", events=[inside])
    removed = s2.apply_source_snapshot(
        subject_id="u1", source="ios", collection_kind="calendar", sync_id="new",
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
        snapshot_kind="incremental",
    )
    if removed:
        problems.append("⑧: 增量同步不该删除任何条目")
    return problems


def _g9_retention_cleanup_spares_what_permanent_aggregates_need(
    new: StorageFactory,
) -> list[str]:
    """⑨ 保留期清理不会破坏永久聚合的正确性。"""
    problems: list[str] = []
    s = new()
    s.append_observation(_obs())
    s.remember_identity(DurableDedupeIdentity(
        subject_id="u1", signal="steps", source="ios",
        source_event_identity_digest="abc", first_applied_at=T0,
    ))
    s.put_aggregate(DailyAggregate(
        subject_id="u1", signal="steps", local_date=DAY, aggregation_kind="daily",
        aggregation_version=1, typed_aggregate={"step_count": {"total": 100}},
    ))
    s.delete_observations(subject_id="u1", before=T0 + timedelta(days=1))

    if not s.get_aggregate(subject_id="u1", signal="steps",
                           start_date=DAY, end_date=DAY):
        problems.append("⑨: 清理明细把永久聚合也删了")
    if not s.has_seen_identity(subject_id="u1", signal="steps", source="ios",
                               digest="abc"):
        problems.append("⑨: 清理明细把去重身份也删了")
    return problems


def _g10_subject_isolation_and_purge(new: StorageFactory) -> list[str]:
    """⑩ 用户之间互不可见；删除一个用户能删干净。"""
    problems: list[str] = []
    s = new()
    s.append_observation(_obs(subject_id="u1", observation_id="o1"))
    s.append_observation(_obs(subject_id="u2", observation_id="o2"))
    s.compare_and_put_current(_current(subject_id="u1"), expected_version=-1)
    s.compare_and_put_current(_current(subject_id="u2"), expected_version=-1)
    s.remember_identity(DurableDedupeIdentity(
        subject_id="u1", signal="steps", source="ios",
        source_event_identity_digest="abc", first_applied_at=T0,
    ))

    # 跨用户负面测试：u2 不该看到 u1 的东西。
    rows, _ = s.list_observations(subject_id="u2", signal="steps")
    if any(o.subject_id != "u2" for o in rows):
        problems.append("⑩: 列观测时看到了别的用户的数据")
    if s.has_seen_identity(subject_id="u2", signal="steps", source="ios",
                           digest="abc"):
        problems.append("⑩: 去重身份跨用户串了 —— u2 的新数据会被当成重复丢掉")

    s.purge_subject(subject_id="u1")
    left, _ = s.list_observations(subject_id="u1", signal="steps")
    if left:
        problems.append("⑩: 删除用户之后还留着观测")
    if s.has_seen_identity(subject_id="u1", signal="steps", source="ios",
                           digest="abc"):
        problems.append("⑩: 删除用户之后还留着去重身份")
    still, _ = s.list_observations(subject_id="u2", signal="steps")
    if not still:
        problems.append("⑩: 删 u1 把 u2 的数据也删了")
    return problems


def _g11_both_source_mirrors_round_trip(new: StorageFactory) -> list[str]:
    """⑪ 两个来源镜像都能写进去、读回来。

    看起来不值一条。它值 —— **这一条是从一个真实现上倒推出来的**：

        io 的 Postgres adapter 写提醒时读了 `r.source_created_at`，
        读回来时又把它当构造参数传回去。`ReminderItemMirror` 上根本没有
        这个字段（日历那个有，提醒那个没有）。写会抛、读也会抛，
        **整条提醒镜像从来没通过过一次**，而这套套件全绿。

    ⑧ 已经在用日历了，所以日历那半一直被覆盖着；提醒那半一次都没被碰过。
    一个只测一半的套件，给出的是「都测过了」的印象。
    """
    problems: list[str] = []
    s = new()
    s.upsert_calendar_events(subject_id="u1", events=[CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id="e1", event_fields={"title": "站会", "start_at": T0},
    )])
    got = list(s.list_calendar_events(subject_id="u1", limit=10))
    if not any(e.source_event_id == "e1" for e in got):
        problems.append("⑪: 日历条目写进去之后读不回来")

    s2 = new()
    s2.upsert_reminders(subject_id="u1", items=[ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id="r1", reminder_fields={"title": "买牛奶",
                                                  "is_completed": False},
    )])
    back = list(s2.list_reminders(subject_id="u1", limit=10))
    if not any(r.source_reminder_id == "r1" for r in back):
        problems.append(
            "⑪: 提醒条目写进去之后读不回来 —— 提醒镜像整条不通"
        )
    # 已完成的默认不出现，除非明说要。
    s2.upsert_reminders(subject_id="u1", items=[ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id="r2", reminder_fields={"title": "交房租",
                                                  "is_completed": True},
    )])
    default = {r.source_reminder_id
               for r in s2.list_reminders(subject_id="u1", limit=10)}
    if "r2" in default:
        problems.append("⑪: 已完成的提醒默认不该出现在待办列表里")
    with_done = {r.source_reminder_id for r in s2.list_reminders(
        subject_id="u1", include_completed=True, limit=10)}
    if "r2" not in with_done:
        problems.append("⑪: include_completed=True 时应该能读到已完成的提醒")
    return problems


def _g12_terminal_events_and_offsets_are_queryable(new: StorageFactory) -> list[str]:
    """⑫ 终态事件查得到，翻页翻得过读取批次。

    两件事都是"静默给错答案"，不是"报错"：

        终态查不到  「为什么没提醒我」的答案通常是 suppressed（撞了安静时段）
                    或 rejected（宿主拒了），而这两个都是终态。宿主只实现了
                    「待投递」那个查询的话，排查看到的是"压根没产生过这个
                    事件"，方向直接错了。
        翻页翻不动  offset 不真的下推到存储，游标就只在第一批里打转。
                    用户看到的是"我八月没有日程"，不是"结果被截断了"。

    宿主容易只把 ``offset`` 加进签名、body 里照旧忽略 —— 签名对了、行为没变，
    而 Protocol 不会告诉你。所以这里验的是**行为**，不是方法在不在。
    """
    problems: list[str] = []
    s = new()
    base = T0
    # 回执状态和投递状态是**两套词表**：runtime 回 conversation_suppressed，
    # 落到发件箱上叫 suppressed。这里照真实那条路走，不直接改状态字段。
    wanted = {
        "ev0": (_receipt.WAKE_SUPPRESSED, _delivery.SUPPRESSED),  # 撞了安静时段
        "ev1": (_receipt.WAKE_REJECTED, _delivery.REJECTED),      # 宿主拒了
        "ev2": (_receipt.WAKE_ACCEPTED, _delivery.DELIVERED),
        "ev3": (None, _delivery.PENDING),
    }
    for i, (eid, (status, state)) in enumerate(wanted.items()):
        s.enqueue_event(_entry(event_id=eid, event_type="t",
                               occurred_at=base + timedelta(minutes=i)))
        if status is None:
            continue
        claimed = s.claim_pending_event(worker_id="w", now=base,
                                        lease_seconds=60)
        if claimed is None:
            problems.append("⑫: 领不到刚入队的事件，后面测不了")
            return problems
        s.record_wake_receipt(
            receipt=WakeReceipt(event_id=claimed.event_id,
                                attempt_id=f"a{i}", status=status,
                                received_at=base),
            next_state=state, claim_token=claimed.claim_token,
        )

    seen = {e.event_id: e.delivery_state
            for e in s.list_events(subject_id="u1", limit=50)}
    for eid, (_status, state) in wanted.items():
        if eid not in seen:
            problems.append(
                f"⑫: list_events 看不到 {state} 的事件 —— "
                f"「为什么没提醒我」这个问题就答不出来"
            )
    only = [e.event_id for e in s.list_events(
        subject_id="u1", delivery_states=[_delivery.SUPPRESSED], limit=50)]
    if only != ["ev0"]:
        problems.append("⑫: 按投递状态筛没生效")

    # offset 必须真的下推：连着两页不能给出同一条。
    first = [e.event_id for e in s.list_events(subject_id="u1", limit=2, offset=0)]
    second = [e.event_id for e in s.list_events(subject_id="u1", limit=2, offset=2)]
    if set(first) & set(second) or len(first) + len(second) != 4:
        problems.append("⑫: list_events 的 offset 没有下推到存储，翻页会重复或漏")

    s2 = new()
    s2.upsert_calendar_events(subject_id="u1", events=[CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id=f"e{i}",
        event_fields={"title": f"e{i}", "start_at": base + timedelta(minutes=i)},
    ) for i in range(4)])
    p1 = [e.source_event_id for e in s2.list_calendar_events(
        subject_id="u1", limit=2, offset=0)]
    p2 = [e.source_event_id for e in s2.list_calendar_events(
        subject_id="u1", limit=2, offset=2)]
    if set(p1) & set(p2) or len(set(p1) | set(p2)) != 4:
        problems.append("⑫: list_calendar_events 的 offset 没有下推到存储")

    s3 = new()
    s3.upsert_reminders(subject_id="u1", items=[ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id=f"r{i}",
        reminder_fields={"title": f"r{i}", "is_completed": False,
                         "due_at": base + timedelta(minutes=i)},
    ) for i in range(4)])
    q1 = [r.source_reminder_id for r in s3.list_reminders(
        subject_id="u1", limit=2, offset=0)]
    q2 = [r.source_reminder_id for r in s3.list_reminders(
        subject_id="u1", limit=2, offset=2)]
    if set(q1) & set(q2) or len(set(q1) | set(q2)) != 4:
        problems.append("⑫: list_reminders 的 offset 没有下推到存储")
    return problems


def _g13_deletes_hit_exactly_their_own_scope(new: StorageFactory) -> list[str]:
    """⑬ 来源删除只命中它自己的那一条，撤回只作用于它指名的那条事实。

    两种删除都**不可逆**，而且错了之后用户看到的是"我的东西凭空少了"，
    没有任何东西说得清为什么。所以每个宿主都必须自己证明范围是全的。

        来源条目   范围是五段：subject + source + account + collection + item
                   少 source 一次 ios 的删除命中 Google 里同 id 的条目
                   少 account 删工作账户的会，私人日历同 id 的一起没
                   少 collection 同账户下两个日历撞 id 时一起没
        事实撤回   同一条源事实上只作用一次，且必须幂等 ——
                   重传/崩溃重放不能算两次

    内存实现上这两条都很容易"看起来对"：真实存储要自己写 SQL，
    少一个 AND 就是删过头，而测试如果只放一条数据是发现不了的。
    """
    problems: list[str] = []
    s = new()
    # 三条只差一层范围的日历条目，其余完全相同。
    variants = [
        ("ios", "work", "cal-1", "撞 id 的三条：这条才该被删"),
        ("ios", "personal", "cal-2", "同来源、不同账户"),
        ("google", "work", "cal-1", "同账户名、不同来源"),
    ]
    s.upsert_calendar_events(subject_id="u1", events=[
        CalendarEventMirror(
            subject_id="u1", source=src, source_account_id=acct,
            source_calendar_id=cal, source_event_id="evt-1",
            event_fields={"title": title, "start_at": T0},
            last_seen_sync_id="r0")
        for src, acct, cal, title in variants
    ])
    n = int(s.delete_source_items(
        subject_id="u1", source="ios", collection_kind="calendar",
        deleted_items=[_DeletedItem("work", "cal-1", "evt-1")]) or 0)
    left = {(e.source, e.source_account_id)
            for e in s.list_calendar_events(subject_id="u1", limit=10)}
    if n != 1:
        problems.append(f"⑬: 该删 1 条，实际删了 {n} 条 —— 范围少了一层")
    if ("ios", "personal") not in left:
        problems.append(
            "⑬: 删工作账户的条目，把同来源另一个账户里同 id 的也删了")
    if ("google", "work") not in left:
        problems.append(
            "⑬: 删 ios 的条目，把另一个来源系统里同 id 的也删了")

    # 撤回：只作用于指名的那条，且幂等。
    #
    # 只放一条撤回是证不了范围的 —— 少写一个 AND 的实现照样全绿。
    # 所以这里在**每一个**范围维度上都放一条"长得几乎一样"的兄弟数据：
    # 同 id 不同用户 / 同 id 不同信号 / 同 id 不同来源。前两个漏了是串号
    # （删我的体重把你的也撤了），第三个漏了是连坐 —— 撤 iOS 的一条，
    # Google 里同 id 的那条事实跟着没，这个 bug 在 tombstone 上真出过。
    s2 = new()
    target = Retraction("u1", "health_weight", "hk-A", "ios", T0)
    siblings = [
        Retraction("u2", "health_weight", "hk-A", "ios", T0),    # 同 id，别人的
        Retraction("u1", "health_bmi", "hk-A", "ios", T0),       # 同 id，另一个信号
        Retraction("u1", "health_weight", "hk-A", "google", T0),  # 同 id，另一个来源
        Retraction("u1", "health_weight", "hk-B", "ios", T0),    # 同一处，另一条
    ]
    if not s2.record_retraction(target):
        problems.append("⑬: 第一次记撤回应该返回 True")
    if s2.record_retraction(target):
        problems.append(
            "⑬: 同一条撤回记了两次都返回 True —— 重传会被当成两次撤回")
    for sib in siblings:
        if not s2.record_retraction(sib):
            problems.append(
                f"⑬: {sib.subject_id}/{sib.signal}/{sib.source} 只是和已有那条"
                " id 相同，被误判成重复 —— 幂等键少了一个维度")

    hit = [(x.subject_id, x.signal, x.source, x.source_event_id)
           for x in s2.list_retractions(subject_id="u1", signal="health_weight",
                                        source_event_ids=["hk-A"])]
    stray = [h for h in hit if h[0] != "u1"]
    if stray:
        problems.append(f"⑬: 查撤回带出了别人的记录 {stray} —— 少了 subject 这层")
    if any(h[1] != "health_weight" for h in hit):
        problems.append(f"⑬: 查撤回带出了别的信号 {hit} —— 少了 signal 这层")
    if any(h[3] != "hk-A" for h in hit):
        problems.append(f"⑬: 查撤回带出了没点名的条目 {hit}")
    if not hit:
        problems.append("⑬: 按身份查撤回一条都没查到")
    # source 不是查询条件（契约见 ports.storage.list_retractions）——
    # 它必须**原样带回来**，由调用方按 (source, id) 比对。存储把它丢了
    # 或者归一成同一个值，调用方就分不出 iOS 和 Google 那两条，
    # 撤一条连坐两条。
    sources = {h[2] for h in hit}
    if sources != {"ios", "google"}:
        problems.append(
            f"⑬: 同 id 不同来源的两条撤回回来时 source 是 {sources}，"
            "应该原样保留 ios 和 google —— 丢了它调用方无法避免连坐")
    return problems


def _g14_a_repeated_transition_is_a_new_event_a_replay_is_not(
    new: StorageFactory,
) -> list[str]:
    """⑭ 同一个跳变再发生一次是新事件；同一条观测重放一次不是。

    这一条**走的是整条 kit 管线**，不是单个端口 —— 事件 id 由 kit 算、由
    宿主的发件箱按 id 去重，缺陷只在两者合起来时才出现：

        0.6.0 及以前   值变化型规则的 id 只看"旧值->新值"。第二天
                       "家 -> 公司"和第一天同 id，宿主的
                       ``ON CONFLICT (event_id) DO NOTHING`` 把它静默吞掉
        宿主自己犯     发件箱不按 event_id、而按 (规则, 前值, 现值) 之类
                       自己拼的键去重，是同一个错

    反过来也要守住：同一条观测从同一个规则状态再求值一遍（崩溃重放、
    非原子的 adapter），必须还是同一个 id、被发件箱挡下。
    """
    from ..contracts.context import IngestContext
    from ..contracts.report import ReportEnvelope
    from ..manifest.minimal import MINIMAL_SIGNALS
    from ..processing.dispatch import evaluate_and_enqueue
    from ..processing.normalize import normalize_observations
    from ..processing.pipeline import ingest_report
    from ..rules.types import EventDefinition, Lifecycle

    problems: list[str] = []
    event_type = "conformance.arrived_at_anchor"
    rule = EventDefinition(
        definition_id="conformance.anchor_changed", version=1,
        signal="proximity_anchor", condition_type="changed",
        field_name="anchor_id", event_type=event_type,
        lifecycle=Lifecycle(scope="forever", fire="every", rearm="never"),
    )

    def report(anchor_id: str, at: datetime, rid: str) -> ReportEnvelope:
        return ReportEnvelope.parse({
            "schema_version": 1, "report_id": rid, "producer": "ios",
            "observations": [{
                "signal": "proximity_anchor", "signal_schema_version": 1,
                "occurred_at": at.isoformat(), "availability": "observed",
                "value": {"anchor_id": anchor_id, "anchor_type": "wifi",
                          "is_connected": True},
            }],
        })

    def ingest(storage: Any, env: ReportEnvelope, at: datetime) -> Any:
        return ingest_report(
            env, context=IngestContext(subject_id="u1", received_at=at),
            storage=storage, signals=MINIMAL_SIGNALS, definitions=[rule],
        )

    def arrivals(storage: Any) -> int:
        return len(storage.list_events(subject_id="u1", event_type=event_type,
                                       limit=50))

    # 家 -> 公司 -> 家 -> 公司（第二天）。第二次"家 -> 公司"是一次新的到达。
    s = new()
    commute = [("home", T0), ("office", T0 + timedelta(hours=1)),
               ("home", T0 + timedelta(hours=9)),
               ("office", T0 + timedelta(days=1, hours=1))]
    for i, (where, at) in enumerate(commute):
        ingest(s, report(where, at, f"commute-{i}"), at)
    got = arrivals(s)
    if got != 3:
        problems.append(
            f"⑭: 家->公司->家->公司 应该在发件箱里留下 3 个事件，实际 {got} —— "
            "第二次「家->公司」被当成了第一次的重复（事件 id 或发件箱的去重键"
            "只看了跳变的前后值，没看是哪一条观测触发的）"
        )

    # 客户端换个 report_id 重传最后那条观测：不是新事件。
    last_where, last_at = commute[-1]
    ingest(s, report(last_where, last_at, "commute-retry"),
           last_at + timedelta(minutes=1))
    if arrivals(s) != got:
        problems.append("⑭: 同一条观测重传一次，发件箱多出了一个事件")

    # 同一条观测从同一个规则状态再求值一遍：同一个 id，被发件箱挡下。
    s2 = new()
    ingest(s2, report("home", T0, "replay-0"), T0)
    scope = "forever@v1"
    before = s2.get_rule_state(subject_id="u1", definition_id=rule.definition_id,
                               scope_key=scope)
    at = T0 + timedelta(hours=1)
    ctx = IngestContext(subject_id="u1", received_at=at)
    item = normalize_observations(
        report("office", at, "replay-1").observations, context=ctx,
        signals=MINIMAL_SIGNALS, source="ios",
    ).normalized[0]
    ids = []
    for _ in range(2):
        from ..processing.mutation import rule_keys
        with s2.mutation_transaction() as mutation:
            mutation.acquire(rule_keys([item], [rule]))
            s2.put_rule_state(subject_id="u1", definition_id=rule.definition_id,
                              scope_key=scope, state=dict(before or {}))
            out = evaluate_and_enqueue(item, context=ctx, storage=s2,
                                       definitions=[rule], mutation=mutation)
        ids += [e.event_id for e in out.events]
    if arrivals(s2) != 1 or len(ids) != 1:
        problems.append(
            f"⑭: 同一条观测从同一个状态重放，发件箱里有 {arrivals(s2)} 个事件、"
            f"入队成功 {len(ids)} 次，应该都是 1 —— 崩溃重放会让用户被提醒两次"
        )
    return problems


def _g15_mutation_and_aggregate_cas(new: StorageFactory) -> list[str]:
    """Sequential contract only; real competing connections remain host proof."""
    from ..contracts.mutation import aggregate_key, fact_key, RetryableMutationError

    problems = []
    s = new()
    row = DailyAggregate(subject_id="u1", signal="steps", local_date=DAY,
                         aggregation_kind="daily", aggregation_version=7,
                         typed_aggregate={"n": 1}, updated_at=T0)
    def get():
        return next(a for a in s.get_aggregate(subject_id="u1", signal="steps",
                    start_date=DAY, end_date=DAY) if a.aggregation_version == 7)
    with s.mutation_transaction() as owner:
        owner.acquire([aggregate_key("u1", "steps", DAY, "daily", 7)])
        if not s.compare_and_put_aggregate(row, expected_version=-1) or get().version != 0:
            problems.append("aggregate CAS: missing=-1, first successful write=0")
        before = get()
        if s.compare_and_put_aggregate(replace(row, typed_aggregate={"n": 99}), expected_version=-1) or get() != before:
            problems.append("aggregate CAS: failed compare must change nothing")
        if not s.compare_and_put_aggregate(row, expected_version=0) or get().version != 1:
            problems.append("aggregate CAS: successful compare increments write version")
        s.put_aggregate(row)
        if get().version != 2 or get().aggregation_version != 7:
            problems.append("aggregate CAS: ordinary put increments write version, not algorithm version")
        before = get()
        if s.compare_and_put_aggregate(row, expected_version=1) or get() != before:
            problems.append("aggregate CAS: ordinary put must stale a prior read")
    # A rollback-only owner must not commit just because application code caught
    # its failure, and an expired capability must never acquire another lock.
    try:
        with s.mutation_transaction() as owner:
            owner.acquire([aggregate_key("u1", "steps", DAY, "daily", 7)])
            s.put_aggregate(replace(row, typed_aggregate={"n": 22}))
            try:
                owner.acquire([fact_key("u1", "steps", "ios", "late")])
            except RetryableMutationError:
                pass
            else:
                problems.append("mutation ownership: descending acquisition must abort")
    except RetryableMutationError:
        pass
    else:
        problems.append("mutation ownership: caught ownership failure still rolls back")
    if get() != before:
        problems.append("mutation ownership: aborted operation left a partial write")
    try:
        owner.acquire([aggregate_key("u1", "steps", DAY, "daily", 7)])
    except RetryableMutationError:
        pass
    else:
        problems.append("mutation ownership: owner remained usable after transaction exit")
    return problems


GUARANTEES: dict[str, Callable[[StorageFactory], list[str]]] = {
    "①上报与观测幂等": _g1_report_and_observation_idempotency,
    "②旧数据不覆盖新当前值": _g2_old_does_not_overwrite_new,
    "③同身份异内容报冲突": _g3_same_identity_different_content_conflicts,
    "④永久聚合抗重放": _g4_permanent_aggregates_survive_replay,
    "⑤提供原子边界": _g5_atomic_boundary_is_offered,
    "⑥事件投递前已落地": _g6_event_is_durable_before_dispatch,
    "⑦投递按 event_id 幂等": _g7_delivery_is_idempotent_by_event_id,
    "⑧局部同步不误删": _g8_partial_sync_does_not_delete_outside_its_window,
    "⑨清理不破坏永久聚合": _g9_retention_cleanup_spares_what_permanent_aggregates_need,
    "⑩用户隔离与删除": _g10_subject_isolation_and_purge,
    "⑪两个来源镜像都能往返": _g11_both_source_mirrors_round_trip,
    "⑫终态可查与翻页下推": _g12_terminal_events_and_offsets_are_queryable,
    "⑬删除只命中自己的范围": _g13_deletes_hit_exactly_their_own_scope,
    "⑭同一跳变再发生是新事件": _g14_a_repeated_transition_is_a_new_event_a_replay_is_not,
    "⑮mutation ownership与aggregate CAS": _g15_mutation_and_aggregate_cas,
}

#: 这几条在内存实现上**永远是绿的**，因为内存天然原子、天然无并发。
#: 宿主必须另外用真实数据库证明，见模块开头。
NOT_PROVABLE_IN_MEMORY: frozenset[str] = frozenset({"⑤提供原子边界"})


def run_storage_conformance(factory: StorageFactory) -> list[str]:
    """跑全部十五条，返回问题清单（空 = 通过）。

    返回列表而不是抛异常：一次看到全部缺口，比逐个修再重跑快得多。
    """
    problems: list[str] = []
    for name, check in GUARANTEES.items():
        try:
            problems += [f"{name} {p}" for p in check(factory)]
        except Exception as exc:                   # noqa: BLE001
            problems.append(f"{name}: 检查本身抛异常了 —— {type(exc).__name__}: {exc}")
    return problems


__all__ = ["run_storage_conformance", "GUARANTEES", "NOT_PROVABLE_IN_MEMORY"]
