"""跨租户隔离 —— 产品规范 §20 把「cross-tenant negative tests」列进了最低交付物，
而在这之前主管线上一条都没有。

这一类 bug 的特点是**不会崩、不会报错**，只会把一个人的数据算进另一个人头上。
每条测试都写成负面形式：不是"u1 能看到自己的"，是"u1 绝对看不到 u2 的"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import IngestContext
from perceptkit.kit import PerceptionKit
from perceptkit.queries import api
from perceptkit.rules import EventDefinition

SH = timezone(timedelta(hours=8))


def when(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


STEPS_3000 = EventDefinition.parse({
    "id": "daily_steps_3000", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
    "lifecycle": {"scope": "local_day", "fire": "once", "rearm": "next_scope"},
    "event": {"type": "activity.step_goal_reached"},
})


def report(count: int, hhmm: str = "09:00", *, rid: str = "r1",
           day: str = "2026-08-27") -> dict:
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "steps", "signal_schema_version": 1,
            "occurred_at": when(hhmm, day).isoformat(),
            "availability": "observed",
            "source_event_id": f"hk-{day}-{hhmm}-{count}",
            "local_date": day,
            "value": {"step_count": count},
        }],
    }


def kit_and_storage() -> tuple[PerceptionKit, InMemoryStorage]:
    s = InMemoryStorage()
    return PerceptionKit(storage=s, definitions=[STEPS_3000]), s


def send(kit: PerceptionKit, subject: str, payload: dict, hhmm: str = "09:00"):
    return kit.ingest(payload, context=IngestContext(subject, when(hhmm)))


# ---------------------------------------------------------------------------
# 当前值
# ---------------------------------------------------------------------------

def test_two_people_walking_do_not_share_a_step_count():
    kit, s = kit_and_storage()
    send(kit, "u1", report(8000, rid="a"))
    send(kit, "u2", report(200, rid="b"))

    u1 = kit.get_current(subject_id="u1", signals=["steps"], now=when("09:30"))
    u2 = kit.get_current(subject_id="u2", signals=["steps"], now=when("09:30"))
    assert u1["steps"].value["step_count"] == 8000
    assert u2["steps"].value["step_count"] == 200


def test_a_newer_reading_from_another_person_never_overwrites_mine():
    """当前值的比较键要是漏了 subject，晚到的那个人就会覆盖掉先来的人。"""
    kit, s = kit_and_storage()
    send(kit, "u1", report(8000, "09:00", rid="a"))
    # u2 的这条更晚 —— 比较键漏了 subject 的话，它会把 u1 的 8000 顶掉
    send(kit, "u2", report(11, "09:30", rid="b"), hhmm="09:30")

    u1 = kit.get_current(subject_id="u1", signals=["steps"], now=when("09:40"))
    assert u1["steps"].value["step_count"] == 8000


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_two_people_can_use_the_same_report_id_without_erasing_each_other():
    """两台设备各自从 1 开始编号 report_id 是完全正常的。

    幂等键漏掉 subject 的话，第二个人的上报会被当成"这批处理过了"直接丢掉 ——
    没有报错，只有一个人的数据永远进不来。
    """
    kit, s = kit_and_storage()
    first = send(kit, "u1", report(8000, rid="report-1"))
    second = send(kit, "u2", report(200, rid="report-1"))

    assert first.applied and second.applied
    u2 = kit.get_current(subject_id="u2", signals=["steps"], now=when("09:30"))
    assert u2["steps"].value["step_count"] == 200


def test_the_same_source_event_id_from_two_people_is_two_observations():
    """HealthKit 的样本 id 在不同设备上可能重复 —— 观测身份也要带 subject。

    这里刻意让两个人的 `source_event_id` **完全相同**：身份漏了 subject 的话，
    第二个人的观测会被当成重复丢掉，一条错误日志都不会有。
    """
    kit, s = kit_and_storage()

    def collide(rid: str) -> dict:
        # 同一个样本 id、同一个时刻、同一个值 —— 除了 subject，
        # 这两条上报**一模一样**。撞不上才说明身份里真的带了 subject。
        payload = report(8000, rid=rid)
        payload["observations"][0]["source_event_id"] = "hk-sample-collision"
        return payload

    send(kit, "u1", collide("a"))
    send(kit, "u2", collide("b"))

    u1, _ = api.list_timeline(s, subject_id="u1", signal="steps",
                              manifest=kit.signals)
    u2, _ = api.list_timeline(s, subject_id="u2", signal="steps",
                              manifest=kit.signals)
    assert len(u1) == 1 and len(u2) == 1


# ---------------------------------------------------------------------------
# 规则状态
# ---------------------------------------------------------------------------

def test_one_person_hitting_the_goal_does_not_consume_another_persons_once_a_day():
    """`fire: once` 的额度是每人每天一次，不是全系统每天一次。

    规则状态的键漏掉 subject，就变成"今天已经有人达标了，所以你不算"。
    """
    kit, s = kit_and_storage()
    # threshold_crossing 要有前值才谈得上"跨过" —— 每人先来一条门槛以下的
    send(kit, "u1", report(100, "08:00", rid="a0"), hhmm="08:00")
    send(kit, "u2", report(100, "08:00", rid="b0"), hhmm="08:00")
    a = send(kit, "u1", report(3200, rid="a"))
    b = send(kit, "u2", report(3500, rid="b"))

    assert len(a.events) == 1 and len(b.events) == 1
    assert {e.subject_id for e in a.events} == {"u1"}
    assert {e.subject_id for e in b.events} == {"u2"}


def test_two_peoples_events_never_share_an_event_id():
    """事件 id 撞了，投递侧的幂等就会把第二个人的事件当成重复丢掉。"""
    kit, s = kit_and_storage()
    send(kit, "u1", report(100, "08:00", rid="a0"), hhmm="08:00")
    send(kit, "u2", report(100, "08:00", rid="b0"), hhmm="08:00")
    a = send(kit, "u1", report(3200, rid="a"))
    b = send(kit, "u2", report(3200, rid="b"))
    assert a.events[0].event_id != b.events[0].event_id


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def test_a_timeline_query_never_reaches_into_another_persons_history():
    kit, s = kit_and_storage()
    for i, hhmm in enumerate(("09:00", "10:00", "11:00")):
        send(kit, "u1", report(100 * i, hhmm, rid=f"u1-{i}"), hhmm=hhmm)
    send(kit, "u2", report(999, "09:00", rid="u2-0"))

    rows, _ = api.list_timeline(s, subject_id="u2", signal="steps",
                                manifest=kit.signals)
    assert [r["value"]["step_count"] for r in rows] == [999]


def test_pending_events_are_listed_per_person():
    kit, s = kit_and_storage()
    send(kit, "u1", report(100, "08:00", rid="a0"), hhmm="08:00")
    send(kit, "u2", report(100, "08:00", rid="b0"), hhmm="08:00")
    send(kit, "u1", report(3200, rid="a"))
    send(kit, "u2", report(3500, rid="b"))

    assert len(api.list_events(s, subject_id="u1")) == 1
    assert len(api.list_events(s, subject_id="u2")) == 1


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------

def test_deleting_one_persons_data_leaves_everyone_else_untouched():
    """规范 §9.4 第 10 条。这条错了是不可逆的 —— 别人的数据真的没了。"""
    kit, s = kit_and_storage()
    send(kit, "u1", report(8000, rid="a"))
    send(kit, "u2", report(200, rid="b"))

    s.purge_subject(subject_id="u1")

    gone = kit.get_current(subject_id="u1", signals=["steps"], now=when("09:30"))
    assert gone["steps"].state == "no_data" and gone["steps"].value is None
    u2 = kit.get_current(subject_id="u2", signals=["steps"], now=when("09:30"))
    assert u2["steps"].value["step_count"] == 200


def test_a_retention_sweep_for_one_person_does_not_sweep_another():
    kit, s = kit_and_storage()
    send(kit, "u1", report(8000, rid="a"))
    send(kit, "u2", report(200, rid="b"))

    s.delete_observations(subject_id="u1", signal="steps")

    u1, _ = api.list_timeline(s, subject_id="u1", signal="steps", manifest=kit.signals)
    u2, _ = api.list_timeline(s, subject_id="u2", signal="steps", manifest=kit.signals)
    assert u1 == [] and len(u2) == 1
