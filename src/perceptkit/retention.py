"""保留期（存多久才删）与保质期（多久之后不再采信）的声明表。

★ 本模块只声明，不含任何删除逻辑 —— 真正的清理任务要接定时器，属于消费方。

★ 保留期的判据：这条数据在 N 个月后，还会改变 agent 对这个人的理解吗。

★ 保质期这一块只列「改判测量时间之后需要改的」。改判之前它判的是
  「距这次上报多久」，改判之后判「这条数据多久前测的」—— 沿用旧值会把功能
  杀死：体重 24 小时 = 除非今天刚称过否则永远 null。
"""
from __future__ import annotations

KEEP_FOREVER = None

_DAY = 86400.0

# signal -> 保留天数；None = 永久；不在表里 = 不进历史表
RETENTION_DAYS: dict[str, int | None] = {
    # 永久：趋势本身就是价值
    "health_body": KEEP_FOREVER,
    "health_sleep": KEEP_FOREVER,
    "health_vitals": KEEP_FOREVER,
    "health_activity": KEEP_FOREVER,
    "health_workout": KEEP_FOREVER,
    "health_metabolic": KEEP_FOREVER,
    "health_mood": KEEP_FOREVER,
    "health_cycle": KEEP_FOREVER,
    "location_signal": KEEP_FOREVER,
    # 一年：年度口味有价值，再久没人问
    "playback": 365,
    # 90 天：瞬时状态，回看价值掉得快
    "motion_state": 90,
    "focus": 90,
    "audio_route": 90,
    # weather 现在的 SHAPE 是 NUMERIC_DIST（history.py），仍在产生 rollup，
    # 所以现在必须有真实保留期 —— 跟瞬时状态同档。
    # ⚠️ Codex code_review 2026-08-23 抓到：早先按"weather 即将改成仅当前+预报、
    # 不再存历史"的未来态把这条声明成了 KEEP_FOREVER 之外/None，
    # 但 SHAPE/history.record_daily 从未真的改过去，导致四张声明表互相矛盾。
    # 真要把 weather 改成不存历史时，这一行、attribution.ATTRIBUTION 里的
    # weather 条目、history.SHAPE 里的 weather 条目，三处必须在同一批一起删，
    # 不许只删一处。
    "weather": 90,
    # 60 天：采集窗口是前后 14 天，够覆盖「未来的会 → 过去的会 → 再留一个月回看」
    "calendar_next_event": 60,
    "reminders": 60,
}

# 改判「测量时间」之后的保质期。只列与 catalog 现值不同的；
# 「现在测现在传」的信号（位置/运动/专注/音频/播放）不需要改。
MEASURED_AT_TTL_SEC: dict[str, float] = {
    "health_body": 90 * _DAY,        # 三个月内称过就还算数
    "health_metabolic": 30 * _DAY,   # 一个月内测过就还算数
    "health_cycle": 60 * _DAY,       # 两个月内有记录就还算数
    "health_vitals": 7 * _DAY,       # 一周内测过就还算数
}


# ⚠️ 「永久保存」不等于「不可删除」（Codex 评审修订 K）。
#    以下生命周期动作必须由消费方实现，本表只是保留期，不是删除策略的全部：
#      · 账号删除时清空
#      · 用户主动清空
#      · 健康权限关闭后：禁用读取与 wake（不是继续用存量）
#      · 第三方撤权
#      · 来源侧删除 / 纠正如何传播到我们的汇总
#    本模块不实现这些 —— 它零 I/O。放在这里是为了让读到保留期的人
#    不会把「永久」误解成「不可删」。
LIFECYCLE_NOTE = "retention != undeletable; see design doc 修订 K"


def stores_history(signal: str) -> bool:
    """这个信号进不进历史表。"""
    return signal in RETENTION_DAYS


def retention_days(signal: str) -> int | None:
    """保留天数；``KEEP_FOREVER``（None）表示永久。

    不进历史表的信号调用这个是调用方的错，直接抛 —— 静默返回 None 会被
    误当成「永久」，那是最坏的一种默认值。
    """
    if signal not in RETENTION_DAYS:
        raise KeyError(f"signal {signal!r} 不进历史表，先用 stores_history() 判断")
    return RETENTION_DAYS[signal]
