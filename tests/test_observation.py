"""观测四态 —— 「没戴表」和「真的没睡」必须分开。

这是设计文档修订 A：HealthKit 没返回睡眠样本，可能是没戴表、没授权、
没同步、查询窗口不对，不能据此断言用户没睡。把这四种情况压成一个「没数据」，
下游要么把它当成零值（编造健康事实），要么把它当成连续（周一三五被当成连续三天）。
"""
from __future__ import annotations


import perceptkit.algorithms.observation as obs


def test_a_real_value_is_observed():
    assert obs.classify(420.0) == obs.OBSERVED


def test_source_reported_zero_is_its_own_state():
    # 步数/活动量会真的出现 0；睡眠几乎不会。两者语义不同，不能都叫「没数据」。
    assert obs.classify(0.0, source_reported_zero=True) == obs.OBSERVED_ZERO


def test_zero_without_the_flag_is_just_an_observed_value():
    # 没有来源背书时，0 只是一个普通数值，不能自作主张升格成 observed_zero
    assert obs.classify(0.0) == obs.OBSERVED


def test_query_succeeded_but_no_sample():
    assert obs.classify(None) == obs.NO_OBSERVATION


def test_unavailable_beats_everything():
    # 未授权/失败/未同步：即使碰巧带了个值也不能采信
    assert obs.classify(None, available=False) == obs.UNAVAILABLE
    assert obs.classify(420.0, available=False) == obs.UNAVAILABLE


def test_only_observed_states_may_enter_a_numeric_trend():
    assert obs.is_trend_eligible(obs.OBSERVED) is True
    assert obs.is_trend_eligible(obs.OBSERVED_ZERO) is True
    assert obs.is_trend_eligible(obs.NO_OBSERVATION) is False
    assert obs.is_trend_eligible(obs.UNAVAILABLE) is False


def test_missing_observations_break_a_streak():
    # 「连续五天睡眠偏低」不能跨过一个没数据的日子
    assert obs.breaks_streak(obs.NO_OBSERVATION) is True
    assert obs.breaks_streak(obs.UNAVAILABLE) is True
    assert obs.breaks_streak(obs.OBSERVED) is False
    assert obs.breaks_streak(obs.OBSERVED_ZERO) is False


def test_trend_eligible_set_matches_the_predicate():
    for state in (obs.OBSERVED, obs.OBSERVED_ZERO, obs.NO_OBSERVATION, obs.UNAVAILABLE):
        assert obs.is_trend_eligible(state) == (state in obs.TREND_ELIGIBLE), state


def test_unknown_state_is_not_eligible_and_breaks_streaks():
    # 失败要往安全那边倒：没见过的状态一律不进趋势、一律打断
    assert obs.is_trend_eligible("something_new") is False
    assert obs.breaks_streak("something_new") is True
