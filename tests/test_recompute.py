"""聚合算法升级之后重算历史。

产品规范 §12：「聚合算法升级 | 使用新 aggregation_version 重算，不静默改写语义」。
先前只做了一半 —— 换版本号、按版本过滤，所以不会两种口径混着 fold，
**但没有任何东西真的把历史重算出来**，升级后旧日子永远停在旧口径。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import IngestContext
from perceptkit.kit import PerceptionKit
from perceptkit.manifest.minimal import MINIMAL_SIGNALS
from perceptkit.processing.recompute import details_may_be_incomplete

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 28, 9, 0, tzinfo=SH)


def when(day: str, hhmm: str = "09:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def feed(kit: PerceptionKit, day: str, counts: list[int]) -> None:
    for i, n in enumerate(counts):
        kit.ingest({
            "schema_version": 1, "report_id": f"{day}-{i}", "producer": "ios",
            "observations": [{
                "signal": "steps", "signal_schema_version": 1,
                "occurred_at": when(day, f"{9 + i:02d}:00").isoformat(),
                "availability": "observed", "source_event_id": f"hk-{day}-{i}",
                "local_date": day, "value": {"step_count": n},
            }],
        }, context=IngestContext("u1", when(day, f"{9 + i:02d}:00")))


def total(s: InMemoryStorage, day: str) -> int | None:
    rows = s.get_aggregate(subject_id="u1", signal="steps",
                           start_date=date.fromisoformat(day),
                           end_date=date.fromisoformat(day),
                           aggregation_kind="daily")
    return rows[0].typed_aggregate["step_count"]["total"] if rows else None


def build() -> tuple[PerceptionKit, InMemoryStorage]:
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    feed(kit, "2026-08-26", [1000, 5000, 9000])
    feed(kit, "2026-08-27", [2000, 6000])
    return kit, s


# ---------------------------------------------------------------------------
# 正常重算
# ---------------------------------------------------------------------------

def test_recomputing_reproduces_what_the_incremental_fold_produced():
    """同一份明细、同一个算法，重算出来必须和一条条折出来的一样。

    对不上的话，"重算"就是在改数字而不是在重现它们。
    """
    kit, s = build()
    before = {d: total(s, d) for d in ("2026-08-26", "2026-08-27")}

    out = kit.recompute_aggregates(subject_id="u1", signal="steps",
                                   start=date(2026, 8, 26), end=date(2026, 8, 27),
                                   now=NOW)
    assert out.ok and len(out.rebuilt) == 2
    assert {d: total(s, d) for d in ("2026-08-26", "2026-08-27")} == before


def test_a_rebuilt_aggregate_says_it_was_rebuilt():
    """排查「这个数字怎么变了」时，第一件事就是看它是不是被重算过。"""
    kit, s = build()
    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    row = s.get_aggregate(subject_id="u1", signal="steps",
                          start_date=date(2026, 8, 26), end_date=date(2026, 8, 26),
                          aggregation_kind="daily")[0]
    assert row.source_coverage["recomputed"] is True
    assert row.source_coverage["observations"] == 3


def test_a_new_version_lands_beside_the_old_one_rather_than_erasing_it():
    """旧口径的文档要留着（供对照和回滚），不能被新口径原地改写。"""
    kit, s = build()
    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26),
                             now=NOW, version=2)
    rows = s.get_aggregate(subject_id="u1", signal="steps",
                           start_date=date(2026, 8, 26), end_date=date(2026, 8, 26),
                           aggregation_kind="daily")
    assert {r.aggregation_version for r in rows} == {1, 2}


def test_recomputing_twice_changes_nothing_the_second_time():
    kit, s = build()
    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    once = total(s, "2026-08-26")
    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    assert total(s, "2026-08-26") == once


def test_only_the_days_asked_for_are_touched():
    kit, s = build()
    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    rows = s.get_aggregate(subject_id="u1", signal="steps",
                           start_date=date(2026, 8, 27), end_date=date(2026, 8, 27),
                           aggregation_kind="daily")
    assert rows[0].source_coverage.get("recomputed") is not True


def test_one_persons_recompute_never_reaches_another_persons_days():
    kit, s = build()
    feed_kit = PerceptionKit(storage=s)
    for i, n in enumerate([7777]):
        feed_kit.ingest({
            "schema_version": 1, "report_id": f"u2-{i}", "producer": "ios",
            "observations": [{
                "signal": "steps", "signal_schema_version": 1,
                "occurred_at": when("2026-08-26").isoformat(),
                "availability": "observed", "source_event_id": f"hk-u2-{i}",
                "local_date": "2026-08-26", "value": {"step_count": n},
            }],
        }, context=IngestContext("u2", when("2026-08-26")))

    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    u2 = s.get_aggregate(subject_id="u2", signal="steps",
                         start_date=date(2026, 8, 26), end_date=date(2026, 8, 26),
                         aggregation_kind="daily")[0]
    assert u2.typed_aggregate["step_count"]["total"] == 7777


# ---------------------------------------------------------------------------
# 明细已经被清理掉的那些天 —— 这才是这件事真正危险的地方
# ---------------------------------------------------------------------------

def test_a_day_older_than_the_detail_retention_is_refused_by_default():
    """明细 1 年、聚合永久的信号，两年前那天的明细早没了。

    照样折一遍会得到一个「当天走了 200 步」的永久统计 ——
    数字错了一个数量级，没有任何地方报错，而且旧值已被覆盖、救不回来。
    """
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.recompute_aggregates(
        subject_id="u1", signal="focus_state",          # 明细 365 天、聚合永久
        start=date(2024, 1, 1), end=date(2024, 1, 1), now=NOW,
    )
    assert not out.ok and not out.rebuilt
    assert "明细可能已被清理" in out.skipped[0][1]


def test_the_caller_can_insist_but_has_to_say_so_explicitly():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.recompute_aggregates(
        subject_id="u1", signal="focus_state",
        start=date(2024, 1, 1), end=date(2024, 1, 1), now=NOW,
        allow_incomplete=True,
    )
    assert out.ok and out.rebuilt == [date(2024, 1, 1)]


def test_a_signal_that_keeps_details_forever_is_never_refused():
    """明细永久的信号不存在"明细可能没了"这回事。"""
    steps = MINIMAL_SIGNALS["steps"]
    assert not details_may_be_incomplete(steps, date(2019, 1, 1), today=NOW.date())


def test_the_cutoff_errs_towards_refusing():
    """误判成「可能不全」的代价是拒绝重算一天（可恢复）；
    反过来的代价是写下一个错了一个数量级的永久统计（不可恢复）。"""
    focus = MINIMAL_SIGNALS["focus_state"]          # 明细 365 天
    today = date(2026, 8, 28)
    assert not details_may_be_incomplete(focus, date(2026, 8, 1), today=today)
    assert details_may_be_incomplete(focus, date(2024, 8, 1), today=today)


def test_a_signal_with_no_history_cannot_be_recomputed_at_all():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.recompute_aggregates(
        subject_id="u1", signal="battery",           # current_only
        start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW,
    )
    assert not out.ok and "无从重算" in out.skipped[0][1]


def test_an_unknown_signal_is_reported_not_crashed():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.recompute_aggregates(subject_id="u1", signal="no_such_thing",
                                   start=date(2026, 8, 26), end=date(2026, 8, 26),
                                   now=NOW)
    assert not out.ok and "manifest 里没有" in out.skipped[0][1]


def test_recompute_reads_past_the_first_page_of_details():
    """明细不止一页时只读第一页，就是在算一份残缺统计。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    day = "2026-08-26"
    for i in range(120):                     # 一页 500，这里够小；但仍走翻页逻辑
        kit.ingest({
            "schema_version": 1, "report_id": f"p{i}", "producer": "ios",
            "observations": [{
                "signal": "steps", "signal_schema_version": 1,
                "occurred_at": (when(day) + timedelta(minutes=i)).isoformat(),
                "availability": "observed", "source_event_id": f"hk-p{i}",
                "local_date": day, "value": {"step_count": 100 + i}},
            ],
        }, context=IngestContext("u1", when(day) + timedelta(minutes=i)))

    kit.recompute_aggregates(subject_id="u1", signal="steps",
                             start=date(2026, 8, 26), end=date(2026, 8, 26), now=NOW)
    row = s.get_aggregate(subject_id="u1", signal="steps",
                          start_date=date(2026, 8, 26), end_date=date(2026, 8, 26),
                          aggregation_kind="daily")[0]
    assert row.source_coverage["observations"] == 120
    assert row.typed_aggregate["step_count"]["total"] == 219
