"""perceptkit —— 主动感知内核:纯函数、零 I/O,与宿主环境无关。

这个包只做判断,不做执行:

  · 有没有发生什么值得留意的事(glance,只出 bool,不泄露具体数值)
  · 这次变化值不值得叫醒一次 agent(wake)
  · 一条测量到底是"有观测到零值"还是"压根没测到"(observation 四态)
  · 一条测量该记在本地日历的哪一天(attribution)
  · "连续 N 天"怎么断续、只在跨入异常时触发一次(streaks)
  · 按天汇总出什么趋势 —— 波动 / 漂移 / 周期性,三种判读方式(history / trend_models)
  · agent 能看哪些字段、该不该给(fields)
  · 该怎么把以上这些讲给模型(prompts)
  · 每类信号的历史该留多久、一条测量的去重键怎么造(retention / identity)

**wake ≠ 该开口了。** 戳醒之后继续睡 / 只看一眼 / 开口说话是三个平行选项,
这个包不参与那个决定,也不产出任何"该说话了"式的措辞。

不在这里的(由调用方提供):

  数据采集 · 存储 · 加解密 · 账号身份与鉴权 · 定时器 / 调度 ·
  真正调模型 · 决定 agent 最终该说什么话

硬指标:**本包只依赖标准库**,不 import 任何宿主模块、不碰网络、不碰数据库、
不碰文件系统。一旦这条破了,"内核可独立发布 / 可被任意宿主嵌入"就都不成立
——见 ``tests/test_purity.py``(AST 扫描)与 ``tests/test_no_host_leakage.py``。

详见 ``README.md``。
"""
from __future__ import annotations

from .attribution import attribute_episode, attribute_instant, split_across_midnight
from .catalog import CAPABILITIES, SIGNALS
from .fields import AGENT_PERCEPTION_SIGNALS, project_signal
from .glance import build_perception_glance
from .history import is_historized
from .identity import MissingIdentity, measurement_key
from .observation import (
    NO_OBSERVATION,
    OBSERVED,
    OBSERVED_ZERO,
    UNAVAILABLE,
    classify,
    is_trend_eligible,
)
from .retention import retention_days, stores_history
from .streaks import current_streak, should_trigger
from .trend_models import model_for, wake_eligible
from .wake import is_wake_worthy_signal, is_significant_change, should_wake

__all__ = [
    "attribute_episode",
    "attribute_instant",
    "split_across_midnight",
    "CAPABILITIES",
    "SIGNALS",
    "AGENT_PERCEPTION_SIGNALS",
    "project_signal",
    "build_perception_glance",
    "is_historized",
    "MissingIdentity",
    "measurement_key",
    "NO_OBSERVATION",
    "OBSERVED",
    "OBSERVED_ZERO",
    "UNAVAILABLE",
    "classify",
    "is_trend_eligible",
    "retention_days",
    "stores_history",
    "current_streak",
    "should_trigger",
    "model_for",
    "wake_eligible",
    "is_wake_worthy_signal",
    "is_significant_change",
    "should_wake",
]
