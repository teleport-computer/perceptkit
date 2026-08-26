"""「连续 N 天偏离」的可执行定义。

设计文档修订 G：
· 窗口按日历日期，不是「最近 N 行」—— 否则周一三五会被当成连续三天
· 缺日 / unavailable 日打断连续性
· 只在从正常跨入异常时 edge-trigger；异常持续不重复叫（hysteresis）
"""
from __future__ import annotations


import perceptkit.observation as obs
import perceptkit.streaks as streaks


def _day(date, abnormal, state=obs.OBSERVED):
    return {"date": date, "state": state, "abnormal": abnormal}


def test_three_consecutive_abnormal_days():
    days = [_day("2026-08-18", True), _day("2026-08-19", True), _day("2026-08-20", True)]
    assert streaks.current_streak(days) == 3


def test_a_normal_day_resets_the_streak():
    days = [_day("2026-08-18", True), _day("2026-08-19", False), _day("2026-08-20", True)]
    assert streaks.current_streak(days) == 1


def test_a_calendar_gap_breaks_the_streak():
    # 周一三五各偏低，中间两天根本没有行 —— 不是连续三天
    days = [_day("2026-08-17", True), _day("2026-08-19", True), _day("2026-08-21", True)]
    assert streaks.current_streak(days) == 1


def test_a_missing_observation_breaks_the_streak():
    days = [
        _day("2026-08-18", True),
        _day("2026-08-19", False, state=obs.NO_OBSERVATION),
        _day("2026-08-20", True),
    ]
    assert streaks.current_streak(days) == 1


def test_unavailable_day_also_breaks_the_streak():
    days = [
        _day("2026-08-18", True),
        _day("2026-08-19", False, state=obs.UNAVAILABLE),
        _day("2026-08-20", True),
    ]
    assert streaks.current_streak(days) == 1


def test_empty_history_has_no_streak():
    assert streaks.current_streak([]) == 0


def test_triggers_when_the_streak_first_reaches_the_threshold():
    days = [_day("2026-08-18", True), _day("2026-08-19", True), _day("2026-08-20", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=False)
    assert trig.fire is True
    assert trig.reason == "streak_reached"
    assert trig.next_firing is True


def test_does_not_trigger_below_the_threshold():
    days = [_day("2026-08-19", True), _day("2026-08-20", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=False)
    assert trig.fire is False
    assert trig.reason == "streak_too_short"
    assert trig.next_firing is False


def test_does_not_retrigger_while_the_same_episode_continues():
    # 异常持续到第 5 天，不能因为「又满足条件了」再叫一次
    days = [_day(f"2026-08-{d}", True) for d in range(16, 21)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=True)
    assert trig.fire is False
    assert trig.reason == "already_firing"
    assert trig.next_firing is True   # 异常还没结束，锁继续扣着


def test_can_fire_again_after_recovering_and_relapsing():
    # 恢复过（正常日打断），再次偏离满 3 天 —— 这是新事件；
    # already_firing 用上一次调用返回的 next_firing 接力，不手工摆 False
    # （旧版本的这条测试手工传 already_firing=False，等于没测到锁存复位
    # 这条真正的行为，Codex code_review 2026-08-23 指出这一点。）
    fired_days = [_day(f"2026-08-{d}", True) for d in (12, 13, 14)]
    trig = streaks.should_trigger(fired_days, min_days=3, already_firing=False)
    assert trig.fire is True and trig.reason == "streak_reached"
    firing = trig.next_firing
    assert firing is True

    recovered_days = fired_days + [_day("2026-08-15", False)]
    trig = streaks.should_trigger(recovered_days, min_days=3, already_firing=firing)
    assert trig.fire is False
    assert trig.next_firing is False        # 恢复过 -> 锁复位
    firing = trig.next_firing

    relapse_days = recovered_days + [
        _day("2026-08-16", True), _day("2026-08-17", True), _day("2026-08-18", True),
    ]
    trig = streaks.should_trigger(relapse_days, min_days=3, already_firing=firing)
    assert trig.fire is True
    assert trig.reason == "streak_reached"


def test_full_cycle_threads_the_latch_state_through_consecutive_calls():
    # fire -> recover -> relapse -> fire again，每一步都用上一次的 next_firing
    # 接力，而不是像旧测试那样手工摆某一步的 already_firing。这才真正验证了
    # "锁存会不会自己打开"，而不是"手动把锁摆开之后会不会再叫"。
    firing = False

    days = [_day("2026-09-01", True), _day("2026-09-02", True), _day("2026-09-03", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=firing)
    assert (trig.fire, trig.reason) == (True, "streak_reached")
    firing = trig.next_firing
    assert firing is True

    # 正常日打断 -> 这段异常结束，即使还没显式恢复调用方的锁，函数自己也
    # 该看出 current_streak 已经归零
    days = days + [_day("2026-09-04", False)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=firing)
    assert trig.fire is False
    assert trig.reason == "recovered"
    firing = trig.next_firing
    assert firing is False

    # 异常持续到第 4 天但还没到没满 min_days，不该叫
    days = days + [_day("2026-09-05", True), _day("2026-09-06", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=firing)
    assert trig.fire is False and trig.reason == "streak_too_short"
    firing = trig.next_firing
    assert firing is False

    # 复发满 3 天 -> 新事件，第二次叫
    days = days + [_day("2026-09-07", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=firing)
    assert (trig.fire, trig.reason) == (True, "streak_reached")
    firing = trig.next_firing
    assert firing is True

    # 异常继续到第 5 天 —— 同一段 episode，不许再叫第三次
    days = days + [_day("2026-09-08", True)]
    trig = streaks.should_trigger(days, min_days=3, already_firing=firing)
    assert trig.fire is False
    assert trig.reason == "already_firing"
    assert trig.next_firing is True


def test_current_streak_is_order_independent_for_consecutive_days():
    # 同样三个日历连续的异常日，只是顺序被打乱 —— 不该依赖调用方保证升序
    days = [_day("2026-08-20", True), _day("2026-08-18", True), _day("2026-08-19", True)]
    assert streaks.current_streak(days) == 3


def test_out_of_order_input_with_a_real_gap_still_returns_one():
    # 乱序，且真的有日历缺口（8-18 缺失）—— 排序不能把缺口抹掉
    days = [_day("2026-08-21", True), _day("2026-08-17", True), _day("2026-08-19", True)]
    assert streaks.current_streak(days) == 1


def test_unparseable_date_rows_degrade_safely_without_raising():
    days = [_day("2026-08-19", True), {"date": "not-a-date", "state": obs.OBSERVED, "abnormal": True}]
    assert streaks.current_streak(days) == 0
