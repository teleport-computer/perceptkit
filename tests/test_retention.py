"""保留期与保质期。

⚠️ **2026-09-03 起，公开的 ``retention_days()`` / ``stores_history()``
从 manifest 查，不再读本模块顶上那张 ``RETENTION_DAYS``。**
外部审查复现了两者七条全对不上（`focus_state` 抛 KeyError、`audio_route`
返回 90 而 manifest 说 7）—— 接入方调公开 API 会拿到错的结论且不报错。

所以这个文件里断言"公开 API 返回什么"的部分，现在断言的是 manifest；
断言"那张历史表长什么样"的部分，改成直接读 ``RETENTION_DAYS``，
说明它只是一份历史记录。一致性由 ``test_retention_single_truth.py`` 盯着。
"""
from __future__ import annotations


import pytest


import perceptkit.algorithms.history as history
import perceptkit.retention as retention
from perceptkit.manifest.types import PERMANENT


def test_health_signals_are_kept_forever():
    """健康数据的明细永久保存 —— 趋势本身就是价值。"""
    for signal in ("health_body", "health_sleep", "health_vitals", "health_activity",
                   "health_workout", "health_metabolic", "health_mood", "health_cycle"):
        assert retention.retention_days(signal) == PERMANENT, signal


def test_none_means_the_opposite_in_the_two_vocabularies():
    """🔴 ``None`` 在旧表和新 API 里意思**正好相反**。

        旧表      None = 永久保存
        新 API    None = 不进历史表；-1（PERMANENT）才是永久

    照旧表的文档去理解新 API，会把「永久保存」读成「不存历史」——
    然后按 0 天清理掉。这条钉住这个碰撞，免得有人把两者当同一个约定。
    """
    assert retention.KEEP_FOREVER is None            # 旧表的"永久"
    assert PERMANENT == -1                           # manifest 的"永久"
    assert retention.retention_days("health_body") == PERMANENT
    # 不进历史表的**抛错**，不返回 None —— 否则照旧词表读的人会把
    # 「根本不存历史」当成「永久保留」，而这两个意思正好相反。
    with pytest.raises(KeyError):
        retention.retention_days("battery")
    assert retention.stores_history("battery") is False


def test_the_old_declaration_table_is_only_a_historical_record():
    """那张表还在，但不再是任何查询的依据。

    留着是因为它里面 weather 那段注释记着一次真实的四表打架，值得留。
    """
    assert retention.RETENTION_DAYS["weather"] == 90       # 旧表这么写
    assert retention.retention_days("weather") == 7        # manifest 才算数


def test_states_that_feed_long_term_habits_keep_a_year():
    """产品 2026-09-02 定的：明细一年、聚合永久。"""
    for signal in ("focus_state", "motion_state", "music_playback"):
        assert retention.retention_days(signal) == 365, signal


def test_short_lived_states_are_swept_within_a_week():
    for signal in ("audio_route", "weather"):
        assert retention.retention_days(signal) == 7, signal


def test_calendar_and_reminders_are_mirrors_not_signals():
    """日历和提醒不再是"有保留期的信号"，它们是**来源镜像**。

    镜像存的是「来源现在有哪些条目」，来源删了本地就删 —— 没有"存多久"
    这个问题。旧表里那两条 60 天是改成镜像之前的写法。
    """
    for old in ("calendar_next_event", "reminders"):
        with pytest.raises(KeyError):
            retention.retention_days(old)
        assert retention.stores_history(old) is False, old


def test_every_historized_signal_answers_with_a_number_or_permanent():
    from perceptkit.manifest import MINIMAL_SIGNALS
    for key, sig in MINIMAL_SIGNALS.items():
        if not sig.stores_history:
            continue
        got = retention.retention_days(key)
        assert got == PERMANENT or got > 0, f"{key} 的保留期是 {got}"


def test_measured_at_ttl_is_longer_than_the_upload_based_one():
    # 改判测量时间后若沿用旧值，不常测的指标会永远是 null
    from perceptkit.catalog import SIGNALS
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
