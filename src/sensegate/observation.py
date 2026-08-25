"""观测四态 —— 「没测到」和「测到是零」和「不能测」是三件不同的事。

★ 为什么要分（设计文档修订 A）：HealthKit 没返回睡眠样本，可能是没戴表、
  没授权、没同步、查询窗口不对。把这些压成一个「没数据」，下游只有两条错路：
  当成零值 → 编造健康事实（「你昨晚没睡」）；当成不存在 → 连续性判断把
  周一三五当成连续三天。

★ 零 I/O、纯判断。
"""
from __future__ import annotations

OBSERVED = "observed"                 # 有测量值
OBSERVED_ZERO = "observed_zero"       # 来源明确记录为零（步数/活动量会有；睡眠几乎不会）
NO_OBSERVATION = "no_observation"     # 查询成功但无样本 —— 「没戴表」和「没睡」都长这样
UNAVAILABLE = "unavailable"           # 未授权 / 查询失败 / 尚未同步

# 只有这两种可以进数值趋势。其余是 coverage 信息，不是数值。
TREND_ELIGIBLE: frozenset[str] = frozenset({OBSERVED, OBSERVED_ZERO})


def classify(value, *, source_reported_zero: bool = False, available: bool = True) -> str:
    """把一次取数的结果归到四态之一。

    ``available=False`` 优先级最高：拿不到授权时即使带了值也不能采信。
    ``source_reported_zero`` 必须由来源背书 —— 我们不从 ``value == 0``
    自作主张推断，因为多数指标的 0 只是一个普通数值。
    """
    if not available:
        return UNAVAILABLE
    if value is None:
        return NO_OBSERVATION
    if source_reported_zero:
        return OBSERVED_ZERO
    return OBSERVED


def is_trend_eligible(state: str) -> bool:
    """这一天的观测能不能作为一个数值参与趋势。未知状态一律不能。"""
    return state in TREND_ELIGIBLE


def breaks_streak(state: str) -> bool:
    """这一天会不会打断「连续 N 天」。未知状态一律打断（往安全那边倒）。"""
    return state not in TREND_ELIGIBLE
