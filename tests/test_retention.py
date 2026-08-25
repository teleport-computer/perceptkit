"""保留期与保质期的声明表。

只声明，不含删除逻辑 —— 清理任务属于接线层，不在本批。
"""
from __future__ import annotations


import pytest


import sensegate.history as history
import sensegate.retention as retention


def test_health_signals_are_kept_forever():
    for signal in ("health_body", "health_sleep", "health_vitals", "health_activity",
                   "health_workout", "health_metabolic", "health_mood", "health_cycle"):
        assert retention.retention_days(signal) is retention.KEEP_FOREVER, signal


def test_location_is_kept_forever():
    # 产品决策：这是私人场景，按价值留，不设过期
    assert retention.retention_days("location_signal") is retention.KEEP_FOREVER


def test_calendar_and_reminders_keep_sixty_days():
    # 采集窗口是前后 14 天：未来的会变成过去的会，再留一个月回看
    assert retention.retention_days("calendar_next_event") == 60
    assert retention.retention_days("reminders") == 60


def test_transient_states_keep_ninety_days():
    for signal in ("motion_state", "focus", "audio_route"):
        assert retention.retention_days(signal) == 90, signal


def test_playback_keeps_one_year():
    assert retention.retention_days("playback") == 365


def test_weather_currently_stores_history_matching_its_shape():
    # weather 的 SHAPE 现在是 NUMERIC_DIST，仍在产生 rollup —— 保留期表必须
    # 如实反映现状，不能提前按"即将改成仅当前+预报"的未来态声明成不存历史
    # （Codex code_review 2026-08-23 抓到四张声明表互相矛盾）。
    assert retention.stores_history("weather") is True
    assert retention.retention_days("weather") == 90


def test_every_historized_signal_has_a_retention_value():
    # 漏一个就意味着那个信号无限增长且没人知道
    missing = [s for s in history.SHAPE if s not in retention.RETENTION_DAYS]
    assert not missing, f"这些信号进历史表却没定保留期：{missing}"


def test_measured_at_ttl_is_longer_than_the_upload_based_one():
    # 改判测量时间后若沿用旧值，不常测的指标会永远是 null
    from sensegate.catalog import SIGNALS
    for signal, ttl in retention.MEASURED_AT_TTL_SEC.items():
        assert ttl > SIGNALS[signal].ttl_sec, signal


# --- 以下为 Codex code_review 补的契约加固：原有断言只防「漏配」，
# 不防「多配」「TTL 表被整个删空」「非历史信号该抛异常却没测」——这几种
# 改动都不会被上面几条测试抓到，回归会悄悄改变已定契约却全绿。


def test_retention_days_keys_exactly_match_historized_signals():
    # 双向：既不许漏（信号进历史表却没保留期），也不许多（保留期表里
    # 有条目但对应信号早就不进历史表了，变成没人再读的死声明）。
    # 不再对 weather 特殊放行 —— 它现在跟其他历史化信号一样，必须严格对齐
    # history.SHAPE（见 retention.RETENTION_DAYS 里 weather 那条的注释）。
    assert set(retention.RETENTION_DAYS) == set(history.SHAPE)


def test_measured_at_ttl_keys_and_values_are_pinned():
    # 遍历字典的断言在字典被删空时会空转过关；这里钉死键集合和精确天数，
    # 防止「改判测量时间」这四个信号的目标值被静默改掉或整体删除。
    assert set(retention.MEASURED_AT_TTL_SEC) == {
        "health_body", "health_metabolic", "health_cycle", "health_vitals",
    }
    _DAY = 86400.0
    assert retention.MEASURED_AT_TTL_SEC["health_body"] == 90 * _DAY
    assert retention.MEASURED_AT_TTL_SEC["health_metabolic"] == 30 * _DAY
    assert retention.MEASURED_AT_TTL_SEC["health_cycle"] == 60 * _DAY
    assert retention.MEASURED_AT_TTL_SEC["health_vitals"] == 7 * _DAY


def test_retention_days_raises_for_non_historized_signal():
    # 静默返回 None 会和 KEEP_FOREVER 撞在一起，被误当成「永久保留」——
    # 这是本模块的核心设计决策，必须有测试钉住，不能只靠 docstring 说明。
    # 用 "battery" 而不是 "weather"：weather 现在确实历史化了（见上面
    # test_weather_currently_stores_history_matching_its_shape），battery 才是
    # history.py 文档里点名的真正 pure-instant、不历史化信号。
    with pytest.raises(KeyError):
        retention.retention_days("battery")
    with pytest.raises(KeyError):
        retention.retention_days("not_a_real_signal")
