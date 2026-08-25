"""「这次上报算不算一件事」「值不值得戳一下 agent」的纯判据。

★ wake ≠ 该开口了。这里只回答要不要戳；戳醒之后 agent 继续睡 / 只看一眼 /
  开口说话，是三个平行选项，内核不参与。``should_wake`` 的第二个返回值是
  **给日志和回执用的机器可读原因**，不是给模型看的措辞，更不是「该说什么」。

★ 零 I/O：不查库、不看时钟、不发 metrics。时间由调用方传进来
  （``now`` / ``last_wake_ts`` / ``observed`` / ``previous_seen``），
  这样才可测、才能被任意宿主复用。

★ 判据搬过来，机制一行不动：事务、行锁、指纹比对这类「怎么保证不重复触发」的
  机制，全部留在宿主那一侧。内核只回答「算不算 / 值不值得」，不管怎么落地。
"""
from __future__ import annotations

from collections.abc import Sequence

# ---------------------------------------------------------------------------
# 感知叫醒源
# ---------------------------------------------------------------------------
# 🔴 **刻意不叫 "wake kind"。** 宿主自己的运行时往往已经有别的、含义不同的
#    "wake kind" 概念——比如「这次叫醒走哪条投递通道」，或者「哪几类叫醒要
#    互相防撞去重」。这些都是宿主接线层的关注点，和这里要回答的问题不是一件事：
#
#      宿主·投递通道选择   —— 「这次叫醒该走哪条路径送出去」
#      宿主·防撞分类       —— 「哪几类叫醒要互相防重复」
#      本模块 `PERCEPTION_WAKE_SOURCES` —— 「这次戳是被什么感知到的」
#
#    三者关注的问题不同，字面上即使有重叠的词（比如都用到 "screen_watch"
#    这个名字），含义也不能互换。本模块这套讲的是**感知来源**，不是运行时
#    通道、也不是防撞分类，故用 `PERCEPTION_WAKE_SOURCES` 这个名字，和宿主
#    自己那几套划清界限——接线时不要为了「统一命名」反过来去改宿主已有的
#    契约，那是两件独立的事。
#
# 下面五个是感知来源的语义分类，各自对应「一类可能触发叫醒的信号变化」：
#
#   source        语义
#   ------------  ---------------------------------------------
#   arrival       到达某个持久锚点附近（比如常去的地点）
#   unlock        长时间静默后重新解锁
#   photo         新增照片
#   screen_watch  屏幕内容/场景发生显著变化
#   broadcast     一段广播式状态开始或结束
#
# broadcast 没有独立开关——宿主实现里可能把它挂在 screen_watch 那个总开关下；
# 但它在信号目录里是独立的一个 wake capability（自带独立的 debounce），所以
# 去重要分开算。两件事，都保留。
PERCEPTION_WAKE_SOURCES: tuple[str, ...] = (
    "arrival",
    "unlock",
    "photo",
    "screen_watch",
    "broadcast",
)


# ---------------------------------------------------------------------------
# 「值变了算不算一件事」
# ---------------------------------------------------------------------------
# 这份名单的语义是**默认算数 + 一张明确的否决名单**，不是「白名单里才算」——
# 真正的叫醒源（photo_added / screen_phash / unlock_after_absence / 几个
# anchor 类信号 / broadcast_state）压根不在信号目录（catalog）的 SIGNALS
# 表里（那张表装的是设备上报字段的 key，两套词表交集为空），用白名单会把
# 每一个真实叫醒源都判成「不算」——这是一个真实踩过的坑：换成白名单实现后，
# 会静默停发全部叫醒事件，而当时的回归测试全绿，因为没有一条测试直接对着
# `is_wake_worthy_signal` 断言过真实叫醒信号该返回 True。
#
# motion 的特例：它是可拉取的上下文，但变得太频繁，故意不作为叫醒源。
NOT_WAKE_WORTHY_SIGNALS: frozenset[str] = frozenset({
    "motion_state",
    "battery",
    "now_playing",
    "time",
    "place_label",
})


def is_wake_worthy_signal(signal: str) -> bool:
    """这个信号变了，值不值得发一次叫醒事件（不问值本身变没变）。

    给已经在别处 durable 地判完「变没变」的调用方用——调用方自己负责判断
    「这次上报和上一次相比是否真的不同」（比如做指纹比对），这里只回答
    「即使真的变了，这类信号是否值得为此叫醒一次」。
    """
    return signal not in NOT_WAKE_WORTHY_SIGNALS


def is_significant_change(signal: str, previous, current) -> bool:
    """值变了、且这个信号的变化本身值得注意，才算一件事。

    调用方同时握着新旧两个值时用这个；只握着「变没变」这个结论时用
    ``is_wake_worthy_signal``。两者是同一条判据的两半，不是两套。
    """
    if previous == current:
        return False
    return is_wake_worthy_signal(signal)


# ---------------------------------------------------------------------------
# 「这条上报是不是迟到 / 撞点了」
# ---------------------------------------------------------------------------
# 纯粹的先后判断。调用方通常是在加锁读出上一条记录的时间戳之后调它——
# **锁、事务、指纹比对这些机制都留在宿主**，内核只回答先后关系。
OBSERVATION_STALE = "stale"          # 比上一条还早：迟到的乱序上报
OBSERVATION_SAME_TS = "same_ts"      # 和上一条同一时刻：可能是重复，也可能是撞点冲突
OBSERVATION_NEWER = "newer"          # 比上一条新：正常的下一条


def observation_order(observed, previous_seen) -> str:
    """比较两个时刻，返回 ``stale`` / ``same_ts`` / ``newer`` 之一。

    只用 ``<`` 和 ``==``，对 float 和 tz-aware datetime 都成立；不做任何
    转换，免得把调用方原本的比较语义改掉。
    """
    if observed < previous_seen:
        return OBSERVATION_STALE
    if observed == previous_seen:
        return OBSERVATION_SAME_TS
    return OBSERVATION_NEWER


# ---------------------------------------------------------------------------
# 「值不值得戳一下 agent」
# ---------------------------------------------------------------------------
# ⚠️ 接线提醒：下面这几个原因串是**内核自己的词**（``source_disabled`` /
#    ``debounced``），宿主如果已经有一套用户可见的原因字符串在用（比如按
#    source 分开命名、或写进事件流 / 审计日志），接线时要先决定「统一成一套」
#    还是「维护一张映射表」——这属于用户可见的行为变更，不要在接线时顺手改掉。
def should_wake(
    source: str,
    *,
    enabled_sources: Sequence[str],
    last_wake_ts: float | None,
    now: float,
    debounce_sec: float,
) -> tuple[bool, str]:
    """返回 ``(要不要戳, 原因)``。

    原因是给日志和回执用的机器可读短语，**不是给模型看的**：这里不产出、不暗示
    任何跟「该说什么」有关的东西。戳醒之后 agent 接着睡、只查一个工具、还是开口
    说话，是三个平行且同等合法的结局，内核不参与那个决定。
    """
    if source not in PERCEPTION_WAKE_SOURCES:
        return False, "unknown_source"
    if source not in tuple(enabled_sources or ()):
        return False, "source_disabled"
    if last_wake_ts is not None and (now - last_wake_ts) < debounce_sec:
        return False, "debounced"
    return True, source
