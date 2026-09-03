"""日历 / 提醒的来源镜像 —— 会真的删用户数据的那一段，之前零测试。

镜像不是快照历史：存的是"来源现在还有哪些条目"，来源删了本地就得删。
所以这里有一个删除路径，而删除不可逆 —— 拿一个局部时间窗去删窗口外的东西，
用户会发现自己去年的日程凭空消失。

对应产品规范 §22-15：Calendar 修改和删除能反映到当前日程。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import CalendarEventMirror, ReminderItemMirror
from perceptkit.queries import api

SH = timezone(timedelta(hours=8))


def t(day: str, hhmm: str = "09:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def ev(eid: str, day: str | None, title: str, *, sync: str = "s1",
       revision: int | None = None, account: str = "acct", cal: str = "cal") -> CalendarEventMirror:
    return CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id=account, source_calendar_id=cal,
        source_event_id=eid,
        event_fields={"title": title, "start_at": t(day) if day else None},
        source_revision=revision, last_seen_sync_id=sync,
    )


def full_sync(s: InMemoryStorage, sync_id: str, start: str, end: str) -> int:
    return s.apply_source_snapshot(
        subject_id="u1", source="ios", collection_kind="calendar",
        sync_id=sync_id, coverage_start=t(start), coverage_end=t(end, "23:59"),
        snapshot_kind="full",
    )


def titles(s: InMemoryStorage, **kw) -> list[str]:
    rows, _ = api.list_calendar_events(s, subject_id="u1", **kw)
    return [e["title"] for e in rows]


# ---------------------------------------------------------------------------
# 修改
# ---------------------------------------------------------------------------

def test_editing_an_event_upstream_replaces_it_rather_than_adding_a_second_copy():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[ev("e1", "2026-08-10", "牙医")])
    s.upsert_calendar_events(subject_id="u1",
                             events=[ev("e1", "2026-08-10", "牙医（改到下午）", revision=2)])

    assert titles(s) == ["牙医（改到下午）"]


def test_the_same_event_id_in_two_different_accounts_stays_two_events():
    """不同账户碰巧用同一个 event id 是完全可能的 —— 身份必须带上账户。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[
        ev("shared-id", "2026-08-10", "公司周会", account="work"),
        ev("shared-id", "2026-08-10", "家庭聚餐", account="personal"),
    ])
    assert sorted(titles(s)) == ["公司周会", "家庭聚餐"]


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------

def test_an_event_deleted_upstream_disappears_from_the_current_schedule():
    """规范 §22-15 要的那件事：来源删了，我们这边也得没有。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[
        ev("e1", "2026-08-10", "牙医", sync="s1"),
        ev("e2", "2026-08-11", "体检", sync="s1"),
    ])
    # 第二轮全量同步只见到 e1 —— e2 在来源侧被删了
    s.upsert_calendar_events(subject_id="u1", events=[ev("e1", "2026-08-10", "牙医", sync="s2")])

    removed = full_sync(s, "s2", "2026-08-01", "2026-08-31")
    assert removed == 1
    assert titles(s) == ["牙医"]


def test_a_partial_sync_is_never_allowed_to_delete_anything():
    """增量同步只知道"变了什么"，不知道"还剩什么" —— 它没有资格删。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[ev("e1", "2026-08-10", "牙医", sync="s1")])

    removed = s.apply_source_snapshot(
        subject_id="u1", source="ios", collection_kind="calendar", sync_id="s2",
        coverage_start=t("2026-08-01"), coverage_end=t("2026-08-31", "23:59"),
        snapshot_kind="incremental",
    )
    assert removed == 0 and titles(s) == ["牙医"]


def test_a_sync_covering_one_week_does_not_delete_last_years_events():
    """最容易犯、后果最重的一个错：拿局部窗口去删窗口外的数据。

    只同步了这一周，就只能对这一周的条目下结论。
    """
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[
        ev("old", "2025-03-02", "去年的婚礼", sync="s1"),
        ev("now", "2026-08-10", "牙医", sync="s1"),
    ])
    # 第二轮只覆盖 8 月那一周，而且这一周里什么都没见到
    removed = full_sync(s, "s2", "2026-08-08", "2026-08-14")

    assert removed == 1                       # 只删了窗口内的"牙医"
    assert titles(s) == ["去年的婚礼"]        # 窗口外的原样还在


def test_an_event_with_no_known_time_is_never_deleted():
    """证明不了它落在覆盖范围内，就没有资格删它。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[ev("e1", None, "时间待定", sync="s1")])

    removed = full_sync(s, "s2", "2026-08-01", "2026-08-31")
    assert removed == 0 and titles(s) == ["时间待定"]


def test_one_subjects_sync_never_touches_another_subjects_data():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[ev("e1", "2026-08-10", "u1 的会", sync="s1")])
    other = CalendarEventMirror(
        subject_id="u2", source="ios", source_account_id="acct", source_calendar_id="cal",
        source_event_id="e1", event_fields={"title": "u2 的会", "start_at": t("2026-08-10")},
        last_seen_sync_id="s1",
    )
    s.upsert_calendar_events(subject_id="u2", events=[other])

    full_sync(s, "s2", "2026-08-01", "2026-08-31")
    assert titles(s) == []
    assert [e["title"] for e in api.list_calendar_events(s, subject_id="u2")[0]] == ["u2 的会"]


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def test_the_schedule_comes_back_in_time_order():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[
        ev("c", "2026-08-12", "third"), ev("a", "2026-08-10", "first"),
        ev("b", "2026-08-11", "second"),
    ])
    assert titles(s) == ["first", "second", "third"]


def test_asking_for_a_window_filters_the_schedule():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[
        ev("a", "2026-08-01", "月初"), ev("b", "2026-08-20", "月中"),
    ])
    assert titles(s, start=t("2026-08-15")) == ["月中"]


def test_reading_the_schedule_goes_through_the_port_not_the_storage_internals():
    """读取侧曾经直接摸 InMemoryStorage 的 `.calendar` 字典。

    那样换成真库的实现会**静默返回空**：没有异常、没有日志，
    只有一个"你今天没有任何日程"的错误回答。
    """
    from perceptkit.ports.storage import StoragePort
    assert hasattr(StoragePort, "list_calendar_events")
    assert hasattr(StoragePort, "list_reminders")


def test_completed_reminders_stay_out_of_the_way_unless_asked_for():
    s = InMemoryStorage()
    s.upsert_reminders(subject_id="u1", items=[
        ReminderItemMirror(
            subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
            source_reminder_id="r1",
            reminder_fields={"title": "买牛奶", "is_completed": True,
                             "due_at": t("2026-08-10")}),
        ReminderItemMirror(
            subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
            source_reminder_id="r2",
            reminder_fields={"title": "交房租", "is_completed": False,
                             "due_at": t("2026-08-11")}),
    ])
    assert [r["title"] for r in api.list_reminders(s, subject_id="u1")[0]] == ["交房租"]
    assert len(api.list_reminders(s, subject_id="u1", include_completed=True)[0]) == 2
