"""叫醒判据 —— 纯函数，不碰 DB、不碰时钟。

★ 语义：should_wake 回答的是「值不值得戳一下 agent」，
  不是「该不该说话」。返回值里不许出现任何跟「说什么」有关的东西。

★ 用词：这套叫 PERCEPTION_WAKE_SOURCES（感知叫醒源），刻意不叫 wake_kind ——
  宿主自己的运行时可能已经有含义不同的 wake_kind 概念（例如投递通道选择、
  或去重分类）。这套和那些不可互传，详见 wake.py 的注释。

原测试文件里还有三条宿主专用的守卫：一条比对宿主另外两套 wake_kind 常量
不撞名（引用了宿主内部模块名），一条扫宿主仓库的源码目录检查未接线名字
有没有被提前引用，外加一条直接读宿主某个内部模块的源码核对信号名单。
三条都要求本包知道宿主的内部结构，迁移时未带过来——宿主侧接线这套判据时
应在自己仓库里保留等价的漂移守卫。
"""
from __future__ import annotations

import pytest


import perceptkit.algorithms.wake as wake


def test_disabled_source_never_wakes():
    ok, reason = wake.should_wake(
        "photo", enabled_sources=("arrival",), last_wake_ts=0.0, now=1000.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "source_disabled"


def test_debounce_blocks_a_second_wake_inside_the_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "debounced"


def test_wake_passes_outside_the_debounce_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1100.0, debounce_sec=60.0
    )
    assert ok is True
    assert reason == "arrival"


def test_first_ever_wake_has_no_previous_timestamp():
    ok, _ = wake.should_wake(
        "unlock", enabled_sources=wake.PERCEPTION_WAKE_SOURCES, last_wake_ts=None, now=1.0,
        debounce_sec=60.0
    )
    assert ok is True


def test_motion_is_not_a_significant_change():
    # 基线语义：motion 变得太频繁，故意不作为叫醒源。
    assert wake.is_significant_change("motion_state", "still", "walking") is False


def test_place_label_change_is_never_significant():
    # place_label 在 NOT_WAKE_WORTHY_SIGNALS 里——即使值真的变了（home -> office），
    # 也不算「值得注意的变化」。
    assert wake.is_significant_change("place_label", "home", "office") is False


def test_same_value_is_never_significant():
    assert wake.is_significant_change("location_signal", "office", "office") is False


# ---------------------------------------------------------------------------
# 直接钉住 is_wake_worthy_signal 的契约：这是真正接线时会被调用的那个函数。
# ---------------------------------------------------------------------------
# 这条测试的存在理由：曾经计划过一版 catalog 驱动的判据（白名单——只有
# catalog.SIGNALS 里的 key 才算 wake-worthy）。catalog.SIGNALS 装的是设备上报
# 字段名，和下面这七个真实叫醒信号的名字交集为空，所以那版实现会让每一个真实
# durable wake 信号都被判成「不算」——会静默停发全部叫醒事件，而当时已有的
# 回归测试全绿，没有一个测试直接对着 is_wake_worthy_signal 断言过这七个信号
# 该返回 True。这里把「五项拒绝 + 七个真实 wake 信号默认允许 + 未知名字默认
# 允许」的契约钉死，防止同类回归再次骗过测试套件。
@pytest.mark.parametrize(
    "signal",
    sorted(wake.NOT_WAKE_WORTHY_SIGNALS),
)
def test_deny_listed_signals_are_never_wake_worthy(signal: str):
    assert wake.is_wake_worthy_signal(signal) is False


@pytest.mark.parametrize(
    "signal",
    [
        # 三个 anchor
        "connectivity_anchor",
        "wifi_anchor",
        "bluetooth_anchor",
        # 其余真实 durable wake 信号
        "unlock_after_absence",
        "screen_phash",
        "photo_added",
        "broadcast_state",
    ],
)
def test_durable_wake_signals_default_allow(signal: str):
    assert wake.is_wake_worthy_signal(signal) is True


def test_unknown_signal_name_defaults_to_allow():
    # 默认允许 + 明确拒绝名单，不是白名单——没见过的名字一律放行。
    assert wake.is_wake_worthy_signal("some_future_signal_nobody_added_yet") is True
