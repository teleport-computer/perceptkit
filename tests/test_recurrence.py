"""重复日程的滚动展开。

产品规范 §12：「Calendar 无限重复事件 | 保存 rule/series，并滚动展开查询窗口」。
先前只有一个 `recurrence_identity` 字段，没有展开逻辑 —— 「每周一的例会」
要么被展开到无限未来存进库，要么就查不到。

**算错重复日程的日期是那种「看起来完全正常」的错**：不报错、不崩，
用户准时出现在一个不存在的会议上。所以这里对「不认识的规则」的处理
是明确拒绝，不是尽力而为。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import CalendarEventMirror
from perceptkit.processing.recurrence import (
    MAX_INSTANCES,
    RecurrenceRule,
    RecurrenceUnsupported,
    expand,
)
from perceptkit.queries import api

SH = timezone(timedelta(hours=8))
MON = datetime(2026, 8, 3, 10, 0, tzinfo=SH)          # 2026-08-03 是周一


def days(rule: dict, *, start: datetime = MON,
         a: str = "2026-08-01", b: str = "2026-09-30") -> list[str]:
    got = expand(start, RecurrenceRule.parse(rule),
                 window_start=date.fromisoformat(a), window_end=date.fromisoformat(b))
    return [d.strftime("%m-%d") for d in got]


# ---------------------------------------------------------------------------
# 认识的那些
# ---------------------------------------------------------------------------

def test_every_monday():
    assert days({"freq": "weekly", "byweekday": [0]})[:4] == \
        ["08-03", "08-10", "08-17", "08-24"]


def test_every_other_monday_skips_a_week():
    """「每两周」要按周数间隔。按天走会把每周都算上 —— 用户每隔一周
    收到一个不存在的会议提醒。"""
    assert days({"freq": "weekly", "byweekday": [0], "interval": 2})[:4] == \
        ["08-03", "08-17", "08-31", "09-14"]


def test_several_weekdays_in_one_rule():
    assert days({"freq": "weekly", "byweekday": [0, 2, 4]})[:5] == \
        ["08-03", "08-05", "08-07", "08-10", "08-12"]


def test_every_third_day():
    assert days({"freq": "daily", "interval": 3})[:4] == \
        ["08-03", "08-06", "08-09", "08-12"]


def test_monthly_lands_on_the_same_day_of_month():
    assert days({"freq": "monthly"}) == ["08-03", "09-03"]


def test_a_monthly_rule_skips_months_that_have_no_such_day():
    """1 月 31 日的规则遇到 2 月 —— 跳过，不退回月末。

    「退回月末」是另一种语义，而且各家日历还不一致。猜一个等于
    在用户日历上凭空造一天。
    """
    got = days({"freq": "monthly"}, start=datetime(2026, 1, 31, 9, 0, tzinfo=SH),
               a="2026-01-01", b="2026-04-30")
    assert "02-28" not in got and "02-29" not in got
    assert got[0] == "01-31"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_until_stops_it():
    assert days({"freq": "weekly", "byweekday": [0], "until": "2026-08-17"}) == \
        ["08-03", "08-10", "08-17"]


def test_count_stops_it_even_without_an_end_date():
    assert days({"freq": "weekly", "byweekday": [0], "count": 2}) == \
        ["08-03", "08-10"]


def test_count_is_counted_from_the_series_start_not_from_the_window():
    """已经发生过的那些也算进 count —— 否则往回查一次，
    次数就被重新数了一遍，规则凭空延长。"""
    got = days({"freq": "weekly", "byweekday": [0], "count": 2},
               a="2026-08-10", b="2026-09-30")
    assert got == ["08-10"]


def test_nothing_before_the_series_starts():
    assert days({"freq": "daily"}, a="2026-07-01", b="2026-08-05") == \
        ["08-03", "08-04", "08-05"]


def test_an_endless_rule_is_capped_rather_than_running_away():
    """「每天，永不结束」配一个十年的窗口就是三千多条直接塞进模型上下文。"""
    got = expand(MON, RecurrenceRule.parse({"freq": "daily"}),
                 window_start=date(2026, 8, 1), window_end=date(2036, 8, 1))
    assert len(got) == MAX_INSTANCES


def test_an_inverted_window_yields_nothing_instead_of_looping():
    assert expand(MON, RecurrenceRule.parse({"freq": "daily"}),
                  window_start=date(2026, 9, 1), window_end=date(2026, 8, 1)) == []


def test_the_time_of_day_and_offset_survive_expansion():
    """每周一早上十点的会，展开之后每一条都还是本地时间早上十点。"""
    got = expand(MON, RecurrenceRule.parse({"freq": "weekly", "byweekday": [0]}),
                 window_start=date(2026, 8, 1), window_end=date(2026, 8, 31))
    assert {d.strftime("%H:%M") for d in got} == {"10:00"}
    assert {d.utcoffset() for d in got} == {timedelta(hours=8)}


# ---------------------------------------------------------------------------
# 不认识的一律拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule, why", [
    ({"freq": "monthly", "bysetpos": 2, "byweekday": [1]}, "序号"),
    ({"freq": "daily", "exdate": ["2026-08-05"]}, "例外"),
    ({"freq": "yearly"}, "freq"),
    ({"freq": "daily", "interval": 0}, "interval"),
])
def test_a_rule_we_do_not_understand_is_refused_not_guessed(rule, why):
    with pytest.raises(RecurrenceUnsupported) as exc:
        RecurrenceRule.parse(rule)
    assert why in str(exc.value)


# ---------------------------------------------------------------------------
# 接到查询上：滚动展开
# ---------------------------------------------------------------------------

def series(rule: dict | None = None) -> CalendarEventMirror:
    fields = {"title": "周会", "start_at": MON}
    if rule is not None:
        fields["recurrence"] = rule
    return CalendarEventMirror(
        subject_id="u1", source_account_id="a", source_calendar_id="c",
        source_event_id="weekly-standup", event_fields=fields,
        recurrence_identity="series-1",
    )


def query(s: InMemoryStorage, a: str, b: str) -> list[dict]:
    rows, _ = api.list_calendar_events(
        s, subject_id="u1",
        start=datetime.fromisoformat(f"{a}T00:00:00+08:00"),
        end=datetime.fromisoformat(f"{b}T23:59:00+08:00"),
    )
    return rows


def test_the_window_decides_how_many_occurrences_come_back():
    """这就是"滚动"那半：库里只存一条规则，窗口往后移就多展开几条。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1",
                             events=[series({"freq": "weekly", "byweekday": [0]})])

    assert len(query(s, "2026-08-01", "2026-08-15")) == 2
    assert len(query(s, "2026-08-01", "2026-09-30")) == 9


def test_every_expanded_occurrence_carries_the_series_it_came_from():
    """展开出来的每一条都要能指回原来那条规则 —— 否则改了规则之后，
    没人知道该动哪些实例。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1",
                             events=[series({"freq": "weekly", "byweekday": [0]})])
    rows = query(s, "2026-08-01", "2026-08-31")
    assert all(r["recurrence_identity"] == "series-1" for r in rows)
    assert all(r["recurrence_expanded"] for r in rows)


def test_a_rule_we_cannot_expand_comes_back_as_the_series_with_a_reason():
    """展不开就把系列交出去并说清原因，不猜日期。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1",
                             events=[series({"freq": "monthly", "bysetpos": 2})])
    rows = query(s, "2026-08-01", "2026-08-31")
    assert len(rows) == 1
    assert rows[0]["recurrence_expanded"] is False
    assert "序号" in rows[0]["recurrence_note"]


def test_a_plain_event_is_untouched():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[series(None)])
    rows = query(s, "2026-08-01", "2026-08-31")
    assert len(rows) == 1 and "recurrence_expanded" not in rows[0]


def test_asking_without_a_window_does_not_expand_anything():
    """「把所有重复日程都给我」对一条无限重复的规则没有答案 ——
    给出来的只会是一个被上限截断出来的假象。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1",
                             events=[series({"freq": "weekly", "byweekday": [0]})])
    rows, _ = api.list_calendar_events(s, subject_id="u1")
    assert len(rows) == 1 and "recurrence_expanded" not in rows[0]
