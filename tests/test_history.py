

# ---------------------------------------------------------------------------
# 睡眠：时长由来源直接给出，按阶段分桶求和
#
# 外部复核（2026-09-07）在方案阶段抓到的两条，都会产出**错的数字而不报错**。
# ---------------------------------------------------------------------------

def _fold_sleep(pairs):
    from perceptkit.processing.aggregate import fold_into_day
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    sig = MINIMAL_SIGNALS["health_sleep"]
    doc = None
    for stage, minutes in pairs:
        doc = fold_into_day(doc, sig, {"stage": stage, "duration_minutes": minutes},
                            ts=1787824800.0)
    return doc


def test_total_sleep_is_the_sum_of_the_stages_not_the_longest_one():
    """🔴 这条抓的是「答一个错的数字」。

    `duration_minutes` 曾经声明成 `daily_total`，而 daily_total 走 CUMULATIVE、
    当天代表值取 **max**。于是 core 250 / deep 70 / rem 110 会被答成
    「昨晚睡了 250 分钟」而不是 430 —— 不报错、不拒收，就是个错数。
    """
    doc = _fold_sleep([("core", 250), ("deep", 70), ("rem", 110)])
    assert doc["duration_minutes"]["total"] == 430


def test_each_stage_keeps_its_own_bucket():
    doc = _fold_sleep([("core", 250), ("deep", 70), ("rem", 110)])
    assert doc["minutes"] == {"core": 250.0, "deep": 70.0, "rem": 110.0}


def test_the_buckets_are_not_inferred_from_the_gap_between_observations():
    """驻留算法（duration_by_state）靠相邻观测的时间差反推时长。睡眠的几条
    观测时刻完全相同 → 差值为 0 → 桶被写成 {"core": 0.0}。

    **0.0 比空的更糟：它看起来像数据。** 下游读到「深睡 0 分钟」不会觉得
    有问题，只会觉得你昨晚没深睡。
    """
    doc = _fold_sleep([("core", 250), ("deep", 70)])
    assert all(v > 0 for v in doc["minutes"].values()), doc["minutes"]


def test_the_trend_reads_the_total_through_the_real_query_entry_point():
    """⚠️ 这条**必须走 kit.get_trend**，不能直接调 read_trend 并把 shape 传进去。

    第一版就是那么写的，然后故障注入（把 queries 里的 shape 改回 None）
    照样全绿 —— 因为那条测试测的是我能调到的纯函数，而生产路径上真正
    决定 shape 的那一行根本没被走到。

    要防的 bug 是：manifest 路径回落到 history.SHAPE（按 iOS 上报键建索引的
    老表，和 manifest 是两套词表），于是趋势读到空 —— 不报错，只是"没有数据"。
    """
    from datetime import date, datetime, timezone
    from perceptkit.kit import PerceptionKit
    from perceptkit.conformance import InMemoryStorage
    from perceptkit.contracts.records import DailyAggregate

    storage = InMemoryStorage()
    for d in range(21, 28):
        storage.put_aggregate(DailyAggregate(
            subject_id="u1", signal="health_sleep", local_date=date(2026, 8, d),
            aggregation_kind="daily", aggregation_version=1,
            typed_aggregate={"minutes": {"core": 250, "deep": 70, "rem": 110},
                             "duration_minutes": {"total": 430}},
            updated_at=datetime(2026, 8, d, 9, tzinfo=timezone.utc)))

    got = PerceptionKit(storage=storage).get_trend(
        subject_id="u1", signal="health_sleep", field="duration_minutes",
        start=date(2026, 8, 21), end=date(2026, 8, 27))
    assert got["current"] == 430, got


def test_a_short_sleep_streak_still_fires_on_the_new_shape():
    """「连续三晚睡不足六小时」读的就是 duration_minutes 的当天总数。
    新形状没被 scheduled 认出来的话，它取到 None → 当那天没数据 →
    规则一条都不触发，静默不响。"""
    from perceptkit.processing.scheduled import _daily_value
    doc = _fold_sleep([("core", 200), ("deep", 60)])
    assert _daily_value(doc, "duration_minutes", "duration_sum_by_state") == 260.0
