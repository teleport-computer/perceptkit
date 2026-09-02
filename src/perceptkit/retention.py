"""保留期（存多久才删）与保质期（多久之后不再采信）。

★ **接定时器仍然是宿主的事**，这里没有任何调度。但「删什么、留什么、
  为什么跳过」是**规则**，规则只该有一份 —— 早先这里连规则都不给，
  于是每个宿主自己照 manifest 重新推导一遍，而这条路上的每个坑
  （两个保留期要分开、永久的要跳过、去重身份不能跟着明细删）
  错了都不报错，只是安静地少数据或多数据。
  ``plan_retention`` 给规则，``PerceptionKit.run_retention`` 走端口执行。

★ 保留期的判据：这条数据在 N 个月后，还会改变 agent 对这个人的理解吗。

★ 保质期这一块只列「改判测量时间之后需要改的」。改判之前它判的是
  「距这次上报多久」，改判之后判「这条数据多久前测的」—— 沿用旧值会把功能
  杀死：体重 24 小时 = 除非今天刚称过否则永远 null。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field as _field
from typing import TYPE_CHECKING, Mapping as _Mapping

from .manifest.types import PERMANENT

if TYPE_CHECKING:                       # 仅为类型标注，运行时不引入依赖
    from .manifest.types import SignalDefinition

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


@dataclass(frozen=True)
class RetentionAction:
    """一个信号、一类数据、一条截止线。"""

    signal: str
    #: ``"observations"`` 或 ``"aggregates"``。
    kind: str
    #: 早于这个时间点的删掉。
    before: _dt.date


@dataclass(frozen=True)
class SkippedSignal:
    """故意不清的一个信号，以及为什么。

    ``code`` 是稳定的机器可读标识，``detail`` 是给人看的一句话。
    **宿主的运维界面该用 code 自己渲染** —— 一个库不该替宿主决定报告用什么
    语言。这里的 detail 是中文（这个包的注释都是中文），直接印进一个英文
    运维报告里就成了半中半英。
    """

    signal: str
    code: str
    detail: str

    def __iter__(self):
        """还能按 ``(signal, detail)` 解包 —— 老调用方不用一次全改。"""
        return iter((self.signal, self.detail))


#: 稳定的跳过原因。宿主按这个渲染自己的文案。
SKIP_NO_HISTORY = "no_history"
SKIP_DETAILS_PERMANENT = "details_permanent"
SKIP_DETAILS_UNDECLARED = "details_undeclared"
SKIP_AGGREGATES_PERMANENT = "aggregates_permanent"
SKIP_AGGREGATES_UNDECLARED = "aggregates_undeclared"


@dataclass
class RetentionPlan:
    """要删什么，以及**故意不删什么、为什么**。

    跳过的理由必须列出来 —— 一份只说"删了 0 条"的报告，和一份说
    "这些信号是永久保存的所以跳过"的报告，看起来一样，但前者读不出
    「是没到期，还是规则写错了」。
    """

    actions: list[RetentionAction] = _field(default_factory=list)
    skipped: list[SkippedSignal] = _field(default_factory=list)


def plan_retention(
    signals: _Mapping[str, "SignalDefinition"], *, now: _dt.datetime,
) -> RetentionPlan:
    """按 manifest 算出这一轮该删什么。**纯函数，零 I/O，不读时钟。**

    规则一共四条，每条都对应一个"错了不报错"的坑：

        明细和聚合是**两个**保留期   典型形态是「明细 1 年、聚合永久」。
                                     不分开写就会继承明细的天数，于是
                                     「8月1日新增了 5 张照片」一年后被扫掉，
                                     而那是一件发生过的事实。
        PERMANENT 的一律跳过         判定放这里，不指望每个宿主记得。
        没声明保留期的**跳过，不默认** 猜一个数字就是拿真实数据去赌。
        去重身份**不在这里删**       明细没了之后，它是「重放的上报会不会
                                     把永久聚合数两遍」之间唯一的东西，
                                     而那个错是不可逆的。
    """
    plan = RetentionPlan()
    for key in sorted(signals):
        sig = signals[key]
        if not sig.stores_history:
            plan.skipped.append(SkippedSignal(key, SKIP_NO_HISTORY, "不进历史表，没东西可清"))
            continue

        if sig.history_retention_days == PERMANENT:
            plan.skipped.append(SkippedSignal(key, SKIP_DETAILS_PERMANENT, "明细永久保存"))
        elif sig.history_retention_days is None:
            plan.skipped.append(SkippedSignal(key, SKIP_DETAILS_UNDECLARED, "没声明明细保留期 —— 跳过，不替它猜一个"))
        else:
            plan.actions.append(RetentionAction(
                key, "observations",
                (now - _dt.timedelta(days=sig.history_retention_days)).date()))

        agg_days = sig.effective_aggregate_retention_days
        if agg_days == PERMANENT:
            plan.skipped.append(SkippedSignal(key, SKIP_AGGREGATES_PERMANENT, "日聚合永久保存"))
        elif agg_days is None:
            plan.skipped.append(SkippedSignal(key, SKIP_AGGREGATES_UNDECLARED, "没声明聚合保留期 —— 跳过，不替它猜一个"))
        else:
            plan.actions.append(RetentionAction(
                key, "aggregates",
                (now - _dt.timedelta(days=agg_days)).date()))
    return plan


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
