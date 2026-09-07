

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


# ---------------------------------------------------------------------------
# 累计型允许向下修订（外部复核 §2.7）
#
# 一味取 max 的后果是**永远保留已知错误的最大值**：同一天重新查一次
# HealthKit 得到更小的数，那更小的才是权威结果，而旧实现会把它丢掉。
# ---------------------------------------------------------------------------

def _fold_energy(steps):
    from perceptkit.processing.aggregate import fold_into_day
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    sig = MINIMAL_SIGNALS["health_activity"]
    doc = None
    for value, ts, rev, epoch in steps:
        doc = fold_into_day(doc, sig, {"active_energy_kcal": value},
                            ts=ts, revision=rev, counter_epoch=epoch)
    return doc["active_energy_kcal"]["total"]


def test_a_newer_revision_may_correct_the_number_downwards():
    """🔴 这是这条的全部理由：来源把当天能量从 500 改成 300，
    库里必须是 300。取 max 的话永远停在那个已知是错的 500。"""
    assert _fold_energy([(500, 100.0, 1, None), (300, 100.0, 2, None)]) == 300


def test_a_late_arriving_older_observation_does_not_overwrite_the_newer_one():
    """迟到的旧观测不是修订。让它覆盖，当天的数会随网络抖动来回跳。"""
    assert _fold_energy([(500, 200.0, 2, None), (300, 100.0, 1, None)]) == 500


def test_a_counter_reset_starts_over_instead_of_looking_like_a_wrong_value():
    """换设备 / 重装之后计数器归零。**重置不是修订** —— 混在一起的话
    「重置到 30」会被单调假设当成错值吃掉，当天永远停在 500。"""
    assert _fold_energy([(500, 100.0, 1, "e1"), (30, 200.0, 1, "e2")]) == 30


def test_a_producer_that_says_nothing_still_gets_the_monotonic_assumption():
    """来源不带版本也不带纪元时，取 max 是单调计数器唯一安全的猜法。
    老客户端因此不受影响 —— 这条改动对它们是零行为变化。"""
    assert _fold_energy([(500, None, None, None), (300, None, None, None)]) == 500


def test_the_provenance_is_kept_so_the_next_observation_can_be_judged():
    """判先后要靠上一条留下的来历。不留的话每次都退回取 max，
    等于这条修复只在第二条观测上生效一次。"""
    from perceptkit.processing.aggregate import fold_into_day
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    sig = MINIMAL_SIGNALS["health_activity"]
    doc = fold_into_day(None, sig, {"active_energy_kcal": 500},
                        ts=100.0, revision=1, counter_epoch="e1")
    cell = doc["active_energy_kcal"]
    assert cell["_rev"] == 1 and cell["_at"] == 100.0 and cell["_epoch"] == "e1"


# ---------------------------------------------------------------------------
# Workout 三投影（外部复核 §2.6）
#
# 「日聚合应对不重复的 Workout 求和，不能取 max 后把结果解释成今日总时长」。
# ---------------------------------------------------------------------------

def _fold_workouts(sessions):
    from perceptkit.processing.aggregate import fold_into_day
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    sig = MINIMAL_SIGNALS["health_workout"]
    doc = None
    for mins, kcal, dist in sessions:
        doc = fold_into_day(doc, sig, {
            "workout_type": "running", "duration_minutes": mins,
            "active_energy_kcal": kcal, "distance_m": dist,
        }, ts=1787824800.0)
    return doc


def test_two_workouts_add_up_instead_of_keeping_the_longest():
    """🔴 跑 30 分钟又跑 45 分钟，今天是 75 分钟。

    取 max 会答 45 —— 一个错的数字，不报错。这正是 daily_total（服务
    同一个计数器的反复上报）和 daily_sum（服务各自独立的事件）的区别。
    """
    doc = _fold_workouts([(30, 200, 5000), (45, 320, 8000)])
    assert doc["duration_minutes"]["total"] == 75


def test_energy_and_distance_are_summed_too_not_left_unaggregated():
    """这两个此前压根没声明聚合 ——「今天一共消耗多少、跑了多远」答不出来。"""
    doc = _fold_workouts([(30, 200, 5000), (45, 320, 8000)])
    assert doc["active_energy_kcal"]["total"] == 520
    assert doc["distance_m"]["total"] == 13000


def test_the_count_is_kept_so_two_trips_can_be_told_from_one_long_one():
    """知道"今天 75 分钟"和知道"分两趟"是两件事。"""
    assert _fold_workouts([(30, 200, 5000), (45, 320, 8000)])["duration_minutes"]["count"] == 2


def test_the_timeline_projection_still_lists_the_sessions():
    """三投影：Timeline / Current / DailyAggregate。这条盯第一个。"""
    assert _fold_workouts([(30, 200, 5000)])["events"]


def test_two_observations_at_the_same_instant_do_not_lose_one():
    """⚠️ 这条是给累计型那批改动配的回归：一度把「时刻相同」判成了
    「更旧的迟到观测」，于是同一时刻到达的第二条被静默丢掉。
    两次运动报同一个 occurred_at 时真会发生。"""
    doc = _fold_workouts([(30, 200, 5000), (45, 320, 8000)])
    assert doc["duration_minutes"]["count"] == 2, "同一时刻的第二条被丢了"
