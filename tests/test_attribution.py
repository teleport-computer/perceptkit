"""一条测量该算哪一天 —— effective_time_frame 只说事实何时发生，不替产品决定归日。

设计文档修订 D：睡眠 episode 归醒来那天（23:00–07:00 算 8/20）；
可加总时长按本地午夜切分；单点按其发生时区的本地日期。
时间必须自带 offset —— 缺时区时不许静默按 UTC 或当前用户时区重解释。
"""
from __future__ import annotations


import pytest


import perceptkit.attribution as attr


def test_sleep_is_attributed_to_the_wake_up_day():
    # 8/19 23:00 睡到 8/20 07:00 —— 直觉上这是「8/20 那天的睡眠」
    assert attr.attribute_episode("2026-08-19T23:00:00+08:00",
                                  "2026-08-20T07:00:00+08:00") == "2026-08-20"


def test_a_nap_inside_one_day_stays_on_that_day():
    assert attr.attribute_episode("2026-08-20T13:00:00+08:00",
                                  "2026-08-20T14:30:00+08:00") == "2026-08-20"


def test_instant_uses_its_own_offset_not_ours():
    # 同一个瞬间，在 +08:00 是 8 月 20 日，在 -05:00 还是 8 月 19 日。
    # 归日必须按事件自带的 offset，不能按服务器或用户当前时区重解释。
    assert attr.attribute_instant("2026-08-20T07:30:00+08:00") == "2026-08-20"
    assert attr.attribute_instant("2026-08-19T18:30:00-05:00") == "2026-08-19"


def test_missing_offset_is_rejected_not_guessed():
    with pytest.raises(ValueError):
        attr.attribute_instant("2026-08-20T07:30:00")


def test_episode_with_end_before_start_is_rejected():
    with pytest.raises(ValueError):
        attr.attribute_episode("2026-08-20T07:00:00+08:00",
                               "2026-08-19T23:00:00+08:00")


def test_duration_is_split_at_local_midnight():
    # 22:00 到次日 02:00 = 前一天 120 分钟 + 后一天 120 分钟
    parts = attr.split_across_midnight("2026-08-19T22:00:00+08:00",
                                       "2026-08-20T02:00:00+08:00")
    assert parts == [("2026-08-19", 120.0), ("2026-08-20", 120.0)]


def test_duration_inside_one_day_is_one_part():
    parts = attr.split_across_midnight("2026-08-20T09:00:00+08:00",
                                       "2026-08-20T10:30:00+08:00")
    assert parts == [("2026-08-20", 90.0)]


def test_duration_spanning_three_days_yields_three_parts():
    parts = attr.split_across_midnight("2026-08-19T23:00:00+08:00",
                                       "2026-08-21T01:00:00+08:00")
    assert [d for d, _ in parts] == ["2026-08-19", "2026-08-20", "2026-08-21"]
    assert parts[1][1] == 1440.0          # 完整的中间一天
    assert sum(m for _, m in parts) == pytest.approx(26 * 60.0)


def test_zero_length_duration_is_empty():
    assert attr.split_across_midnight("2026-08-20T09:00:00+08:00",
                                      "2026-08-20T09:00:00+08:00") == []


# --- ③ DST：不传 tz 时用固定 offset（老实说明局限），传 tz 时按真实换日规则 ---

def test_without_tz_a_dst_fallback_day_is_wrongly_hard_coded_to_1440():
    # 2026-11-01 是 America/New_York 的秋季回退日（25 小时）。不传 tz 时函数
    # 只知道 start 自带的固定 offset，不知道当地钟表那天真的跳了一小时 ——
    # 这段测试钉住"不传 tz 就是这个已知局限"，而不是声称它是对的。
    parts = attr.split_across_midnight("2026-11-01T00:00:00-04:00",
                                       "2026-11-02T00:00:00-05:00")
    assert parts == [("2026-11-01", 1440.0), ("2026-11-02", 60.0)]  # 已知：错的


def test_with_tz_a_dst_fallback_day_gets_its_real_1500_minutes():
    parts = attr.split_across_midnight("2026-11-01T00:00:00-04:00",
                                       "2026-11-02T00:00:00-05:00",
                                       tz="America/New_York")
    assert parts == [("2026-11-01", 1500.0)]   # 25 小时，一段吃满


def test_with_tz_a_dst_spring_forward_day_gets_its_real_1380_minutes():
    # 2026-03-08 是 America/New_York 的春季提前日（23 小时）。
    parts = attr.split_across_midnight("2026-03-07T00:00:00-05:00",
                                       "2026-03-09T00:00:00-04:00",
                                       tz="America/New_York")
    assert parts == [("2026-03-07", 1440.0), ("2026-03-08", 1380.0)]
    assert sum(m for _, m in parts) == pytest.approx(47 * 60.0)   # 2 天少 1 小时


def test_with_tz_span_crossing_a_transition_sums_to_the_true_wall_clock_delta():
    parts = attr.split_across_midnight("2026-10-30T12:00:00-04:00",
                                       "2026-11-02T12:00:00-05:00",
                                       tz="America/New_York")
    total = sum(m for _, m in parts)
    assert total == pytest.approx(3 * 24 * 60.0 + 60.0)  # 3 天整 + 秋季回退多出的 1 小时


# --- ⑥ 各段不单独四舍五入，加总必须等于真实总时长 ---

def test_sub_second_span_across_midnight_sums_exactly():
    # 跨午夜 0.08 秒：若每段各自四舍五入到 3 位小数会变成 0.001+0.001=0.002，
    # 真实值是 0.08/60 = 0.0013333...
    parts = attr.split_across_midnight("2026-08-19T23:59:59.96+08:00",
                                       "2026-08-20T00:00:00.04+08:00")
    assert sum(m for _, m in parts) == pytest.approx(0.08 / 60.0, abs=1e-12)


def test_odd_second_span_across_midnight_sums_exactly():
    parts = attr.split_across_midnight("2026-08-19T23:59:57+08:00",
                                       "2026-08-20T00:00:04+08:00")
    assert sum(m for _, m in parts) == pytest.approx(7.0 / 60.0, abs=1e-12)


def test_attribution_rules_are_declared_per_signal():
    assert attr.ATTRIBUTION["health_sleep"] == attr.EPISODE_END
    assert attr.ATTRIBUTION["health_body"] == attr.INSTANT
    assert attr.ATTRIBUTION["location_signal"] == attr.SPLIT_AT_MIDNIGHT
    assert attr.ATTRIBUTION["health_cycle"] == attr.SOURCE_LOCAL_DATE


def test_attribution_keys_cover_every_historized_signal():
    # 双向契约：漏一个就意味着某个有 rollup 的信号没人知道该归哪天。
    # 不再对 weather 特殊放行 —— 它的 SHAPE 现在确实是 NUMERIC_DIST、仍在
    # 产生 rollup，ATTRIBUTION 里必须有它的 INSTANT 条目（见 attr.ATTRIBUTION
    # 里 weather 那条的注释：早先按未来态提前声明"故意缺席"，导致声明表
    # 互相矛盾，Codex code_review 2026-08-23 抓到）。
    # 用集合差而非硬编码具体信号名 —— 这样以后新加的信号漏配也会被抓到。
    from perceptkit import history

    missing = set(history.SHAPE) - set(attr.ATTRIBUTION)
    assert not missing, f"这些信号进历史表却没定归属规则：{missing}"


def test_weather_is_instant_matching_its_numeric_dist_shape():
    assert attr.ATTRIBUTION["weather"] == attr.INSTANT
