"""产品规范 §12「边界情况」里没有测试守着的那几条。

这一整节的共同点：**出错的时候不会崩、不会报错**，只会让某个数字悄悄不对。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import IngestContext
from perceptkit.kit import PerceptionKit
from perceptkit.processing.normalize import (
    CLOCK_FUTURE_LIMIT_SEC,
    CLOCK_TOLERANCE_SEC,
    check_clock,
)
from perceptkit.queries import api

SH = timezone(timedelta(hours=8))


def when(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def steps_report(count, *, occurred: datetime, rid: str,
                 availability: str = "observed", day: str = "2026-08-27") -> dict:
    obs = {
        "signal": "steps", "signal_schema_version": 1,
        "occurred_at": occurred.isoformat(), "availability": availability,
        "source_event_id": f"hk-{rid}", "local_date": day,
    }
    if availability == "observed":
        obs["value"] = {"step_count": count}
    return {"schema_version": 1, "report_id": rid, "producer": "ios",
            "observations": [obs]}


def fresh() -> tuple[PerceptionKit, InMemoryStorage]:
    s = InMemoryStorage()
    return PerceptionKit(storage=s), s


def send(kit, payload, received: datetime, subject: str = "u1"):
    return kit.ingest(payload, context=IngestContext(subject, received))


# ---------------------------------------------------------------------------
# §12-9  Producer 时钟明显错误
# ---------------------------------------------------------------------------

def test_a_phone_whose_clock_says_next_year_is_refused():
    """手机时间被设到 2027 年，收下它的后果：今天的步数写进 2027-08-28。

    明年那天翻历史会凭空多出一天，今天的记录永远找不到，
    而且从头到尾没有任何地方报错。
    """
    kit, s = fresh()
    out = send(kit, steps_report(8000, occurred=when("09:00", "2027-08-27"), rid="r1"),
               when("09:00"))
    assert out.rejected and not out.applied
    assert any("未来的时间一定是错的" in r for _, reasons in out.rejected for r in reasons)


def test_three_months_of_offline_backfill_is_accepted_without_complaint():
    """过去的时间不设上限 —— 三个月没开 app 一次性补传是完全正常的。"""
    kit, s = fresh()
    out = send(kit, steps_report(8000, occurred=when("09:00", "2026-05-27"), rid="r1",
                                 day="2026-05-27"),
               when("09:00"))
    assert out.applied and not out.rejected


def test_a_small_skew_passes_silently():
    """正常的时钟漂移和网络延迟不该产生噪音。"""
    reject, warn = check_clock(when("09:00"), when("09:05"), "steps")
    assert reject is None and warn is None


def test_a_middling_skew_is_accepted_but_flagged():
    """离线补传会产生偏差，所以照收 —— 但归属日期不一定可信，要说出来。"""
    reject, warn = check_clock(when("09:00"), when("13:00"), "steps")
    assert reject is None and warn and "时间可疑" in warn


def test_the_asymmetry_is_deliberate_and_stated():
    """未来一侧卡死、过去一侧不卡，这个不对称是这条规则的全部意义。"""
    assert CLOCK_FUTURE_LIMIT_SEC > CLOCK_TOLERANCE_SEC
    far_past = check_clock(when("09:00", "2020-01-01"), when("09:00"), "steps")
    far_future = check_clock(when("09:00", "2030-01-01"), when("09:00"), "steps")
    assert far_past[0] is None                 # 收
    assert far_future[0] is not None           # 拒


def test_one_bad_clock_does_not_take_down_the_rest_of_the_batch():
    """一批里有一条时间离谱，只拒那一条。"""
    kit, s = fresh()
    payload = steps_report(8000, occurred=when("09:00"), rid="r1")
    payload["observations"].append({
        "signal": "steps", "signal_schema_version": 1,
        "occurred_at": when("09:00", "2030-01-01").isoformat(),
        "availability": "observed", "source_event_id": "hk-bad",
        "local_date": "2030-01-01", "value": {"step_count": 1},
    })
    out = send(kit, payload, when("09:00"))
    assert len(out.applied) == 1 and len(out.rejected) == 1


# ---------------------------------------------------------------------------
# §12-12  unavailable 不覆盖最后可靠数值
# ---------------------------------------------------------------------------

def test_losing_permission_does_not_erase_what_we_last_knew():
    """"现在读不到"和"从来没有过"是两件事。

    用户撤销权限之后，agent 应该能说"你上次是 8000 步，现在读不到了"，
    而不是"你没有步数数据"。
    """
    kit, s = fresh()
    send(kit, steps_report(8000, occurred=when("09:00"), rid="r1"), when("09:00"))
    send(kit, steps_report(None, occurred=when("10:00"), rid="r2",
                           availability="unavailable"), when("10:00"))

    view = kit.get_current(subject_id="u1", signals=["steps"], now=when("10:05"))["steps"]
    assert view.state == "unavailable"
    assert view.value is None
    assert view.last_known is not None and view.last_known["step_count"] == 8000


# ---------------------------------------------------------------------------
# §12-16  步数当日回退 / 重置
# ---------------------------------------------------------------------------

def test_a_midday_counter_reset_never_produces_a_negative_day():
    """累计型的数字会重置（换设备、重装、系统抽风）。

    当天总数取"见过的最大值"，所以重置之后再往上走不会把当天算成负的、
    也不会把已经走过的路清零。
    """
    from perceptkit import history
    doc = {}
    for reading in (3000, 8000, 0, 500):
        doc = history._merge_cumulative(doc, {"step_count": reading})
    assert doc["step_count"]["total"] == 8000


def test_a_cumulative_field_keeps_climbing_normally():
    from perceptkit import history
    doc = {}
    for reading in (1000, 4000, 9000):
        doc = history._merge_cumulative(doc, {"step_count": reading})
    assert doc["step_count"]["total"] == 9000


# ---------------------------------------------------------------------------
# §12-11  no_data 不当成 0
# ---------------------------------------------------------------------------

def test_no_data_is_not_zero_anywhere_it_matters():
    kit, s = fresh()
    send(kit, steps_report(0, occurred=when("09:00"), rid="r1"), when("09:00"))
    send(kit, steps_report(None, occurred=when("10:00"), rid="r2",
                           availability="no_data", day="2026-08-27"), when("10:00"))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="steps",
                                manifest=kit.signals)
    got = [(r["availability"], (r["value"] or {}).get("step_count")) for r in rows]
    assert got == [("observed", 0), ("no_data", None)]


def test_a_late_arriving_unavailable_does_not_unseat_a_newer_reading():
    """撤权限的那条上报如果比某次读数更早，它讲的不是当前的故事。"""
    kit, s = fresh()
    send(kit, steps_report(None, occurred=when("08:00"), rid="a",
                           availability="unavailable"), when("09:20"))
    send(kit, steps_report(8000, occurred=when("09:00"), rid="b"), when("09:20"))
    send(kit, steps_report(None, occurred=when("08:30"), rid="c",
                           availability="unavailable"), when("09:21"))

    view = kit.get_current(subject_id="u1", signals=["steps"], now=when("09:25"))["steps"]
    assert view.state == "fresh" and view.value["step_count"] == 8000


def test_becoming_unavailable_does_not_fire_value_change_rules():
    """拿"读不到了"去喂 `changed` 型规则，会把撤权限当成一次真实变化。"""
    from perceptkit.rules import EventDefinition

    changed_rule = EventDefinition.parse({
        "id": "steps_changed", "version": 1,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "changed"},
        "event": {"type": "activity.steps_changed"},
    })
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[changed_rule])
    send(kit, steps_report(8000, occurred=when("09:00"), rid="r1"), when("09:00"))
    out = send(kit, steps_report(None, occurred=when("09:10"), rid="r2",
                                 availability="unavailable"), when("09:10"))
    assert out.events == []
