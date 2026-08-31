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
    StoredObservation,
)
from ..contracts.receipt import WakeReceipt

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
# 十条保证
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
        subject_id="u1", source_account_id="a", source_calendar_id="c",
        source_event_id="e_in", event_fields={"start_at": T0},
        last_seen_sync_id="old",
    )
    outside = CalendarEventMirror(
        subject_id="u1", source_account_id="a", source_calendar_id="c",
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
}

#: 这几条在内存实现上**永远是绿的**，因为内存天然原子、天然无并发。
#: 宿主必须另外用真实数据库证明，见模块开头。
NOT_PROVABLE_IN_MEMORY: frozenset[str] = frozenset({"⑤提供原子边界"})


def run_storage_conformance(factory: StorageFactory) -> list[str]:
    """跑全部十条，返回问题清单（空 = 通过）。

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
