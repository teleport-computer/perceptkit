"""端到端：上报 → 观测 → 当前值 → 历史 → 规则命中 → 事件 → 投递 → 回执。

产品规范 §20.7 明确要求这条链路能整条走通，并且点名说"当前 quickstart 只串联
纯函数，不能代表插件已经可接入"。这个文件就是对那句话的回应。

它同时是**接入模板**：新宿主照着这里实现两个端口，就能跑起来。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit import contracts
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import IngestContext
from perceptkit.contracts.delivery import DeliveryAttempt
from perceptkit.contracts.event import PerceptionEvent
from perceptkit.contracts.receipt import WakeReceipt
from perceptkit.kit import PerceptionKit
from perceptkit.rules import EventDefinition

SH = timezone(timedelta(hours=8))


def at(hhmm: str, day: str = "2026-08-27") -> str:
    return f"{day}T{hhmm}:00+08:00"


def now(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(at(hhmm, day))


STEPS_3000 = EventDefinition.parse({
    "id": "daily_steps_3000", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
    "lifecycle": {"scope": "local_day", "fire": "once", "rearm": "next_scope"},
    "event": {"type": "activity.step_goal_reached"},
    "wake": {"enabled": True},
})

STEPS_5000 = EventDefinition.parse({
    "id": "daily_steps_5000", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 5000},
    "event": {"type": "activity.step_goal_reached"},
})


class RecordingRuntime:
    """一个最小的 WakePort 实现 —— 宿主要写的就是这么多。"""

    def __init__(self, behaviour="accept"):
        self.behaviour = behaviour
        self.seen: list[str] = []
        self.attempts: list[DeliveryAttempt] = []

    def wake(self, event: PerceptionEvent, attempt: DeliveryAttempt) -> WakeReceipt:
        self.attempts.append(attempt)
        # 幂等：runtime 认得这个 event_id 就返回 duplicate，不重复处理。
        if event.event_id in self.seen:
            status = contracts.WAKE_DUPLICATE
        elif self.behaviour == "boom":
            raise RuntimeError("队列挂了")
        elif self.behaviour == "busy":
            status = contracts.WAKE_SUPPRESSED
        else:
            status = contracts.WAKE_ACCEPTED
            self.seen.append(event.event_id)
        return WakeReceipt(
            event_id=event.event_id, attempt_id=attempt.attempt_id,
            status=status, received_at=event.received_at,
            runtime_ref=f"job_{len(self.seen)}",
        )


def make(behaviour="accept", definitions=(STEPS_3000,)):
    storage = InMemoryStorage()
    runtime = RecordingRuntime(behaviour)
    return storage, runtime, PerceptionKit(
        storage=storage, wake=runtime, definitions=list(definitions)
    )


def steps_report(count, *, hhmm="10:30", rid="r1", sample=None, day="2026-08-27"):
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "steps", "signal_schema_version": 1,
            "occurred_at": at(hhmm, day), "availability": "observed",
            "source_event_id": sample or f"hk-{day}-{hhmm}",
            "value": {"step_count": count, "local_date": day},
        }],
    }


# ---------------------------------------------------------------------------
# 整条链路
# ---------------------------------------------------------------------------

def test_the_whole_chain_runs_end_to_end():
    """规范 §20.7 要的那条链路。"""
    storage, runtime, kit = make()

    kit.ingest(steps_report(2400, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    out = kit.ingest(steps_report(3012, hhmm="10:30", rid="r2"),
                     context=IngestContext("u1", now("10:30")))

    # 观测落了、当前值动了、聚合有了、事件进了发件箱
    assert len(storage.observations) == 2
    assert len(out.events) == 1
    assert out.events[0].type == "activity.step_goal_reached"
    assert out.events[0].previous == 2400 and out.events[0].current == 3012
    assert len(storage.list_pending_events()) == 1

    # ingest 默认不投 —— 事件已落地，投递归宿主的 worker
    assert runtime.seen == []

    result = kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    assert len(result.delivered) == 1
    assert len(runtime.seen) == 1
    assert len(storage.receipts) == 1
    assert not storage.list_pending_events()


def test_current_and_history_are_queryable_after_ingest():
    storage, _, kit = make()
    kit.ingest(steps_report(3012), context=IngestContext("u1", now("10:30")))

    current = kit.get_current(subject_id="u1", signals=["steps"], now=now("10:35"))
    assert current["steps"]["state"] == "fresh"
    assert current["steps"]["value"]["step_count"] == 3012

    daily = kit.get_daily(subject_id="u1", signal="steps",
                          start=date(2026, 8, 27), end=date(2026, 8, 27))
    assert daily[0]["value"]["step_count"]["total"] == 3012


def test_a_stale_current_value_does_not_pretend_to_be_now():
    """让模型说"你电量还有 87%"（其实是四小时前的），是这类系统最常见的说错话。"""
    storage, _, kit = make()
    kit.ingest(steps_report(3012, hhmm="10:30"),
               context=IngestContext("u1", now("10:30")))
    # steps 的 TTL 是 1 小时
    later = kit.get_current(subject_id="u1", signals=["steps"], now=now("13:00"))
    assert later["steps"]["state"] == "stale"
    assert later["steps"]["value"] is None
    assert later["steps"]["last_known"]["as_of"].startswith("2026-08-27T10:30")


def test_a_signal_with_no_data_reports_that_instead_of_guessing():
    _, _, kit = make()
    got = kit.get_current(subject_id="u1", signals=["battery"], now=now("10:00"))
    assert got["battery"]["state"] == "no_data"


# ---------------------------------------------------------------------------
# 规范 §22 的完成定义
# ---------------------------------------------------------------------------

def test_two_thresholds_fire_independently_and_neither_repeats(  # §22-8/9/10
):
    storage, runtime, kit = make(definitions=(STEPS_3000, STEPS_5000))
    ctx = lambda h: IngestContext("u1", now(h))

    kit.ingest(steps_report(2999, hhmm="09:00"), context=ctx("09:00"))
    a = kit.ingest(steps_report(3000, hhmm="10:00", rid="r2"), context=ctx("10:00"))
    b = kit.ingest(steps_report(4999, hhmm="11:00", rid="r3"), context=ctx("11:00"))
    c = kit.ingest(steps_report(5000, hhmm="12:00", rid="r4"), context=ctx("12:00"))
    d = kit.ingest(steps_report(5001, hhmm="13:00", rid="r5"), context=ctx("13:00"))

    assert [e.definition_id for e in a.events] == ["daily_steps_3000"]
    assert b.events == []
    assert [e.definition_id for e in c.events] == ["daily_steps_5000"]
    assert d.events == []                      # §22-10：同天后续不重复


def test_the_rule_rearms_the_next_day():                        # §22-11
    storage, _, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3000, hhmm="10:00", rid="r2"),
               context=IngestContext("u1", now("10:00")))

    kit.ingest(steps_report(500, hhmm="09:00", rid="r3", day="2026-08-28"),
               context=IngestContext("u1", now("09:00", "2026-08-28")))
    tomorrow = kit.ingest(
        steps_report(3100, hhmm="10:00", rid="r4", day="2026-08-28"),
        context=IngestContext("u1", now("10:00", "2026-08-28")),
    )
    assert len(tomorrow.events) == 1           # 换了一天，重新武装


def test_the_event_is_durable_before_any_delivery_is_attempted():  # §22-13
    """先落地再投递：走到 ingest 返回那一刻，事件就丢不了了。"""
    storage, runtime, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))
    assert len(storage.outbox) == 1
    assert runtime.seen == []                  # 还一次都没投


def test_a_crash_before_the_receipt_lands_does_not_double_wake():  # §22-14
    """投出去之后、回执存下来之前崩溃 —— 重启后一定会再投一次。
    runtime 靠 event_id 认出是同一个，不重复处理。"""
    storage, runtime, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    event_id = next(iter(storage.outbox))
    runtime.seen.append(event_id)              # runtime 其实已经处理过了
    from dataclasses import replace
    from perceptkit.contracts import delivery
    storage.outbox[event_id] = replace(        # 但我们这边回执没存下来
        storage.outbox[event_id], delivery_state=delivery.PENDING)

    result = kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    assert result.delivered == [event_id]      # duplicate 也算送达
    assert len(runtime.seen) == 1              # 没有被重复处理


def test_the_event_id_is_stable_so_replays_collapse():
    """随机 id 的话，一次重放就是一个新事件，用户被提醒两次。"""
    from perceptkit.processing import event_id_for
    args = dict(subject_id="u1", definition=STEPS_3000, scope="2026-08-27",
                trigger="2999->3000")
    assert event_id_for(**args) == event_id_for(**args)
    assert event_id_for(**{**args, "scope": "2026-08-28"}) != event_id_for(**args)


# ---------------------------------------------------------------------------
# 投递的失败路径
# ---------------------------------------------------------------------------

def test_a_failed_delivery_goes_back_to_pending_with_backoff():
    storage, runtime, kit = make("boom")
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    result = kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    assert len(result.retrying) == 1
    entry = next(iter(storage.outbox.values()))
    assert entry.delivery_state == "pending"
    assert entry.next_attempt_at > now("10:31")     # 退避了，不是立刻重试


def test_a_wake_port_that_raises_is_treated_as_failure_not_success():
    """"结果未知"和"失败"要走同一条路 —— 盲目当成功会让事件永远送不到。"""
    storage, _, kit = make("boom")
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))
    kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    assert storage.receipts[-1].status == "enqueue_failed"
    assert not storage.receipts[-1].consumes_budget


def test_a_suppressed_wake_is_terminal_and_costs_no_budget():
    """runtime 收到了但选择不响应。重投等于打扰第二次；额度也不该扣。"""
    storage, _, kit = make("busy")
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    result = kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    assert len(result.suppressed) == 1
    entry = next(iter(storage.outbox.values()))
    assert entry.delivery_state == "suppressed"
    assert entry.budget_reservation_id is None      # 占位释放了，没兑现
    assert not storage.list_pending_events()        # 终态，不会再被捞


def test_only_a_delivered_event_keeps_its_budget_reservation():
    storage, _, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))
    kit.dispatch_pending(worker_id="w1", now=now("10:31"))
    entry = next(iter(storage.outbox.values()))
    assert entry.delivery_state == "delivered"
    assert entry.budget_reservation_id is not None


def test_retries_eventually_stop_instead_of_looping_forever():
    storage, _, kit = make("boom")
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    clock = now("10:31")
    for _ in range(10):
        clock += timedelta(hours=1)            # 跳过退避
        kit.dispatch_pending(worker_id="w1", now=clock)
    entry = next(iter(storage.outbox.values()))
    assert entry.delivery_state == "dead_letter"
    assert not storage.list_pending_events()


def test_a_claimed_event_is_not_handed_to_a_second_worker():
    """租约没到期之前，同一个事件只能有一个 worker 在处理 ——
    否则两个 worker 都投出去，用户被提醒两次。"""
    storage, _, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    first = storage.claim_pending_event(worker_id="w1", now=now("10:31"),
                                        lease_seconds=60)
    assert first is not None
    assert storage.claim_pending_event(worker_id="w2", now=now("10:31"),
                                       lease_seconds=60) is None


def test_an_expired_lease_lets_another_worker_take_over():
    """原持有者可能已经死了 —— 到期不接管的话，事件永远卡在 claimed。"""
    storage, _, kit = make()
    kit.ingest(steps_report(2999, hhmm="09:00"),
               context=IngestContext("u1", now("09:00")))
    kit.ingest(steps_report(3012, rid="r2"), context=IngestContext("u1", now("10:30")))

    storage.claim_pending_event(worker_id="w1", now=now("10:31"), lease_seconds=60)
    taken = storage.claim_pending_event(worker_id="w2", now=now("10:35"),
                                        lease_seconds=60)
    assert taken is not None and taken.lease_owner == "w2"


# ---------------------------------------------------------------------------
# 接入体验
# ---------------------------------------------------------------------------

def test_a_host_only_has_to_implement_two_ports():
    """规范 §3.1 要的接入体验。这个测试就是那份"接入模板"。"""
    from perceptkit.ports import StoragePort, WakePort
    assert isinstance(InMemoryStorage(), StoragePort)
    assert isinstance(RecordingRuntime(), WakePort)


def test_dispatching_without_a_wake_port_fails_loudly():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, definitions=[STEPS_3000])
    with pytest.raises(ValueError):
        kit.dispatch_pending(worker_id="w1", now=now("10:00"))
