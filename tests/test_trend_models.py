"""漂移型趋势：不存在「平时水平」，看方向和速率。

存在理由：体重一年 80→60，用波动型（跟中位数比）会得出
「你比平时轻 10 公斤」——而这个人从来没有一个叫「平时」的体重。
"""
from __future__ import annotations


import perceptkit.algorithms.trend_models as tm


def _rows(pairs):
    return [{"date": d, "doc": {"weight_kg": v}} for d, v in pairs]


def test_weight_is_declared_drifting():
    assert tm.TREND_MODEL["health_body"] == tm.DRIFTING


def test_sleep_is_declared_fluctuating():
    assert tm.TREND_MODEL["health_sleep"] == tm.FLUCTUATING


def test_cycle_is_declared_cyclical():
    assert tm.TREND_MODEL["health_cycle"] == tm.CYCLICAL


def test_metabolic_is_query_only():
    # 血糖/血压缺餐食、体位、运动上下文，不能做同质趋势比较 —> 查得到，不叫醒
    assert "health_metabolic" in tm.QUERY_ONLY


def test_bmi_and_height_do_not_participate():
    # BMI 是体重的派生量，一起叫醒 = 同一件事叫两次；成人身高是常量
    assert "bmi" in tm.DERIVED_OR_CONSTANT_FIELDS
    assert "height_cm" in tm.DERIVED_OR_CONSTANT_FIELDS


def test_current_heart_rate_is_query_only_despite_its_signal_being_wake_eligible():
    # health_vitals 整体是 FLUCTUATING、不在 QUERY_ONLY 里 —— 只看"有模型 +
    # 不在 QUERY_ONLY"这条粗规则会误判当前心率可以叫醒；但它每次心跳都在变，
    # 跟血糖血压一样缺采样协议，必须靠字段级例外单独拦。
    assert tm.wake_eligible("health_vitals", "current_heart_rate") is False
    # 同一个信号里的静息心率、HRV 不受影响，仍然可以叫醒
    assert tm.wake_eligible("health_vitals", "resting_heart_rate") is True
    assert tm.wake_eligible("health_vitals", "hrv_sdnn_ms") is True
    # 信号级 QUERY_ONLY（血糖/血压）照样整体拦
    assert tm.wake_eligible("health_metabolic", "glucose_mg_dl") is False
    # 派生/常量字段跟信号无关，照样拦
    assert tm.wake_eligible("health_body", "bmi") is False


def test_year_long_weight_loss_reports_total_and_rate():
    # 12 个月 80 -> 60
    rows = _rows([(f"2025-{m:02d}-01", 80.0 - (20.0 * (m - 1) / 11)) for m in range(1, 13)])
    out = tm.read_drift(rows, "health_body", "weight_kg")
    assert out["model"] == tm.DRIFTING
    assert out["first"]["value"] == 80.0
    assert out["last"]["value"] == 60.0
    assert out["total_delta"] == -20.0
    # 11 个月跨度掉 20 -> 约 -1.8/月
    assert -2.0 < out["per_month"] < -1.5


def test_flat_series_reports_zero_drift():
    rows = _rows([("2025-01-01", 68.0), ("2025-06-01", 68.0), ("2025-12-01", 68.0)])
    out = tm.read_drift(rows, "health_body", "weight_kg")
    assert out["total_delta"] == 0.0
    assert out["per_month"] == 0.0
    assert out["accelerating"] is False


def test_recent_acceleration_is_detected():
    # 前 10 个月几乎不动，最后 2 个月猛掉
    pairs = [(f"2025-{m:02d}-01", 80.0) for m in range(1, 11)]
    pairs += [("2025-11-01", 77.0), ("2025-12-01", 74.0)]
    out = tm.read_drift(_rows(pairs), "health_body", "weight_kg")
    assert out["accelerating"] is True


def test_single_point_cannot_drift():
    out = tm.read_drift(_rows([("2025-01-01", 68.0)]), "health_body", "weight_kg")
    assert out["per_month"] is None
    assert out["n"] == 1


def test_empty_rows_are_safe():
    out = tm.read_drift([], "health_body", "weight_kg")
    assert out["n"] == 0
    assert out["total_delta"] is None


def test_regular_cycle_reports_typical_interval():
    out = tm.read_cycles(["2025-06-02", "2025-06-30", "2025-07-29", "2025-08-28"],
                         today="2025-09-05")
    assert out["model"] == tm.CYCLICAL
    assert out["intervals"] == [28, 29, 30]
    assert out["typical_interval"] == 29        # 中位数
    assert out["days_since_last"] == 8
    assert out["overdue_by"] == 0               # 还没到，不算推迟


def test_overdue_is_measured_against_typical_interval():
    out = tm.read_cycles(["2025-06-02", "2025-06-30", "2025-07-29", "2025-08-28"],
                         today="2025-10-02")
    assert out["days_since_last"] == 35
    assert out["overdue_by"] == 6               # 35 - 29


def test_single_event_has_no_interval():
    out = tm.read_cycles(["2025-08-28"], today="2025-09-05")
    assert out["intervals"] == []
    assert out["typical_interval"] is None
    assert out["overdue_by"] is None            # 没有既往间隔，无从判断推迟
    assert out["days_since_last"] == 8


def test_empty_events_are_safe():
    out = tm.read_cycles([], today="2025-09-05")
    assert out["n"] == 0
    assert out["days_since_last"] is None


def test_unsorted_and_duplicate_dates_are_normalized():
    out = tm.read_cycles(["2025-07-29", "2025-06-02", "2025-06-30", "2025-06-30"],
                         today="2025-08-01")
    assert out["intervals"] == [28, 29]
