"""时钟驱动的两种规则：streak（连续 N 天）和 absence（该来的没来）。

这两条以前是"写了但主流程里跑不通"的状态 —— 最容易被忘掉、最后变成技术债。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.rules import EventDefinition

SH = timezone(timedelta(hours=8))


def t(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


SHORT_SLEEP_STREAK = EventDefinition.parse({
    "id": "three_short_nights", "version": 1,
    "source": {"signal": "health_sleep", "field": "duration_minutes"},
    "condition": {
        "type": "streak",
        "operator": "lt", "value": 360,        # 每天的条件：睡眠少于 6 小时
        "params": {"periods": 3},              # 连续 3 天
    },
    "event": {"type": "health.short_sleep_streak"},
})

NO_WEIGHT_FOR_DAYS = EventDefinition.parse({
    "id": "weight_gone_quiet", "version": 1,
    "source": {"signal": "health_body", "field": "weight_kg"},
    "condition": {"type": "absence", "value": 259200},   # 3 天
    "lifecycle": {"scope": "local_day", "fire": "once"},
    "event": {"type": "health.weight_not_logged"},
})


def _prev(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def sleep_report(minutes: int, day: str, rid: str) -> dict:
    """一觉是跨午夜的：前一天 23:40 睡，当天 07:20 醒 —— 整段归【醒来】那天。"""
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "health_sleep", "signal_schema_version": 1,
            "occurred_at": t("07:20", day).isoformat(),
            "availability": "observed", "source_event_id": f"hk-sleep-{day}",
            "value": {
                "stage": "asleep", "duration_minutes": minutes,
                "start_at": t("23:40", _prev(day)).isoformat(),
                "end_at": t("07:20", day).isoformat(),
            },
        }],
    }


def feed_sleep(kit, nights: list[tuple[str, int]]):
    for i, (day, minutes) in enumerate(nights):
        kit.ingest(sleep_report(minutes, day, f"r{i}"),
                   context=IngestContext("u1", t("08:00", day)))


# ---------------------------------------------------------------------------
# streak
# ---------------------------------------------------------------------------

def test_a_malformed_interval_only_rejects_that_one_observation():
    """producer 发来结束早于开始的区间 —— 只拒这一条，不能炸掉整批。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    bad = sleep_report(300, "2026-08-27", "r1")
    bad["observations"][0]["value"]["start_at"] = t("23:40", "2026-08-27").isoformat()
    out = kit.ingest(bad, context=IngestContext("u1", t("08:00", "2026-08-27")))
    assert out.rejected and not out.applied


def test_a_streak_fires_exactly_when_it_reaches_the_threshold():
    """"连续三天没睡好"应该提醒一次 —— 不是从第三天起天天念叨。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[SHORT_SLEEP_STREAK])
    feed_sleep(kit, [("2026-08-25", 300), ("2026-08-26", 320), ("2026-08-27", 310)])

    day1 = kit.evaluate_daily(subject_id="u1", local_date=date(2026, 8, 25),
                              now=t("09:00", "2026-08-25"))
    day2 = kit.evaluate_daily(subject_id="u1", local_date=date(2026, 8, 26),
                              now=t("09:00", "2026-08-26"))
    day3 = kit.evaluate_daily(subject_id="u1", local_date=date(2026, 8, 27),
                              now=t("09:00", "2026-08-27"))

    assert not day1.events and not day2.events
    assert len(day3.events) == 1
    assert day3.events[0].type == "health.short_sleep_streak"


def test_a_good_night_breaks_the_streak():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[SHORT_SLEEP_STREAK])
    feed_sleep(kit, [("2026-08-25", 300), ("2026-08-26", 480), ("2026-08-27", 310)])

    out = kit.evaluate_daily(subject_id="u1", local_date=date(2026, 8, 27),
                             now=t("09:00", "2026-08-27"))
    assert not out.events


def test_a_missing_night_breaks_the_streak_rather_than_being_skipped():
    """没戴表那天既不算"满足"也不算"跳过" —— 那不是连续三天。
    把缺失当成任何一种，都是在替用户编事实。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[SHORT_SLEEP_STREAK])
    feed_sleep(kit, [("2026-08-25", 300), ("2026-08-27", 310)])   # 26 号缺

    out = kit.evaluate_daily(subject_id="u1", local_date=date(2026, 8, 27),
                             now=t("09:00", "2026-08-27"))
    assert not out.events


def test_the_daily_condition_and_the_period_count_are_separate_knobs():
    """`operator`/`value` 是「每天的条件」，`params.periods` 是「连续几天」。
    挤在一个字段里表达不了。"""
    assert SHORT_SLEEP_STREAK.value == 360           # 每天：< 6 小时
    assert SHORT_SLEEP_STREAK.params["periods"] == 3  # 连续：3 天


def test_streak_lookback_is_bounded():
    """不设上限的话，一条「连续 N 天」的规则会在每次日聚合时把整个历史读一遍。"""
    from perceptkit.processing.scheduled import MAX_STREAK_LOOKBACK_DAYS
    assert MAX_STREAK_LOOKBACK_DAYS > 0


# ---------------------------------------------------------------------------
# absence
# ---------------------------------------------------------------------------

def weight_report(kg: float, day: str, rid: str) -> dict:
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "health_body", "signal_schema_version": 1,
            "occurred_at": t("08:00", day).isoformat(),
            "availability": "observed", "source_event_id": f"hk-w-{day}",
            "value": {"weight_kg": kg},
        }],
    }


def test_absence_fires_only_once_the_silence_is_long_enough():
    """这条规则的前提就是「没有新数据」—— 跟着观测跑永远等不到它。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[NO_WEIGHT_FOR_DAYS])
    kit.ingest(weight_report(70.0, "2026-08-20", "r1"),
               context=IngestContext("u1", t("08:00", "2026-08-20")))

    quiet = kit.evaluate_absence(subject_id="u1", now=t("08:00", "2026-08-22"))
    assert not quiet.events                      # 才两天

    loud = kit.evaluate_absence(subject_id="u1", now=t("09:00", "2026-08-24"))
    assert len(loud.events) == 1
    assert loud.events[0].type == "health.weight_not_logged"


def test_absence_says_nothing_to_a_user_who_never_logged_anything():
    """"你三天没记录体重了"对一个从没记过体重的用户来说是句莫名其妙的话。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[NO_WEIGHT_FOR_DAYS])
    out = kit.evaluate_absence(subject_id="u1", now=t("09:00", "2026-08-24"))
    assert not out.events
    assert any("从来没有过数据" in (reason or "") for _, reason in out.misses)


def test_absence_does_not_nag_every_tick():
    """定时器每分钟跑一次，不能每次都提醒 —— 生命周期挡住了。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[NO_WEIGHT_FOR_DAYS])
    kit.ingest(weight_report(70.0, "2026-08-20", "r1"),
               context=IngestContext("u1", t("08:00", "2026-08-20")))

    first = kit.evaluate_absence(subject_id="u1", now=t("09:00", "2026-08-24"))
    second = kit.evaluate_absence(subject_id="u1", now=t("09:05", "2026-08-24"))
    assert len(first.events) == 1 and not second.events


def test_silence_is_measured_from_the_current_value_not_the_detail_rows():
    """明细可能已经按保留期清理掉了，当前值一定还在。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[NO_WEIGHT_FOR_DAYS])
    kit.ingest(weight_report(70.0, "2026-08-20", "r1"),
               context=IngestContext("u1", t("08:00", "2026-08-20")))
    s.observations.clear()                       # 模拟保留期清理

    out = kit.evaluate_absence(subject_id="u1", now=t("09:00", "2026-08-24"))
    assert len(out.events) == 1


# ---------------------------------------------------------------------------
# 接入形态
# ---------------------------------------------------------------------------

def test_both_entries_hang_off_the_loop_the_host_already_needs():
    """宿主不用为这两条多起一个东西 —— 投递那条线本来就要定时循环。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[NO_WEIGHT_FOR_DAYS])
    for name in ("dispatch_pending", "evaluate_absence", "evaluate_daily"):
        assert callable(getattr(kit, name))
