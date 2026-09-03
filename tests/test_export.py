"""按 subject 导出 —— 产品规范 §8 要的是「定位、导出和删除」，导出那一半原来没有。

没有它，「把我的数据给我」只能靠调用方自己把八个查询拼一遍，
而**拼漏一个就是少给了用户一部分数据，还没人会发现**。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import IngestContext
from perceptkit.contracts.records import CalendarEventMirror, ReminderItemMirror
from perceptkit.kit import PerceptionKit

SH = timezone(timedelta(hours=8))


def when(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def build() -> tuple[PerceptionKit, InMemoryStorage]:
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest({
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [
            {"signal": "steps", "signal_schema_version": 1,
             "occurred_at": when("09:00").isoformat(), "availability": "observed",
             "source_event_id": "hk-1", "local_date": "2026-08-27",
             "value": {"step_count": 8000}},
            {"signal": "location_city", "signal_schema_version": 1,
             "occurred_at": when("09:00").isoformat(), "availability": "observed",
             "value": {"locality": "上海", "country_code": "CN",
                       "coordinate": {"lat": 31.23, "lon": 121.47}}},
        ],
    }, context=IngestContext("u1", when("09:00")))
    s.upsert_calendar_events(subject_id="u1", events=[CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id="e1",
        event_fields={"title": "牙医", "start_at": when("15:00")})])
    s.upsert_reminders(subject_id="u1", items=[ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id="r1",
        reminder_fields={"title": "交房租", "is_completed": True})])
    return kit, s


def test_the_export_reaches_every_kind_of_thing_we_hold():
    kit, s = build()
    dump = kit.export_subject(subject_id="u1")

    assert dump["subject_id"] == "u1"
    assert "steps" in dump["observations"]
    assert "location_city" in dump["observations"]
    assert dump["current"]["steps"]["last_known"]["step_count"] == 8000
    assert [e["title"] for e in dump["calendar_events"]] == ["牙医"]
    assert [r["title"] for r in dump["reminders"]] == ["交房租"]


def test_a_completed_reminder_is_still_the_users_data():
    """默认查询会把已完成的挡掉，但导出不是查询 —— 那也是他的数据。"""
    kit, s = build()
    assert kit.export_subject(subject_id="u1")["reminders"]


def test_the_export_still_withholds_what_was_never_stored():
    """精确坐标在写入边界就被丢了，所以导出里也不会有 —— 不是我们藏着不给。"""
    kit, s = build()
    dump = kit.export_subject(subject_id="u1")
    city = dump["observations"]["location_city"][0]["value"]
    assert city["locality"] == "上海"
    assert "coordinate" not in city


def test_the_export_never_reaches_into_someone_elses_data():
    kit, s = build()
    kit.ingest({
        "schema_version": 1, "report_id": "r2", "producer": "ios",
        "observations": [{"signal": "steps", "signal_schema_version": 1,
                          "occurred_at": when("09:00").isoformat(),
                          "availability": "observed", "source_event_id": "hk-2",
                          "local_date": "2026-08-27",
                          "value": {"step_count": 55}}],
    }, context=IngestContext("u2", when("09:00")))

    dump = kit.export_subject(subject_id="u2")
    counts = [r["value"]["step_count"] for r in dump["observations"]["steps"]]
    assert counts == [55]


def test_the_export_says_it_is_only_half_the_story():
    """宿主自己存的东西（原始载荷、加密信封、它的业务表）不在这里。

    不标出来的话，调用方会把这份 dump 当成"用户的全部数据"直接交出去。
    """
    kit, s = build()
    assert kit.export_subject(subject_id="u1")["kit_managed_only"] is True


def test_an_empty_subject_exports_cleanly_rather_than_erroring():
    kit, s = build()
    dump = kit.export_subject(subject_id="nobody")
    assert dump["observations"] == {} and dump["calendar_events"] == []


def test_deleting_after_exporting_leaves_nothing_behind():
    """导出和删除是一对：先给用户，再删干净。"""
    kit, s = build()
    assert kit.export_subject(subject_id="u1")["observations"]
    s.purge_subject(subject_id="u1")
    after = kit.export_subject(subject_id="u1")
    assert after["observations"] == {} and after["current"] == {}
    assert after["calendar_events"] == [] and after["reminders"] == []
