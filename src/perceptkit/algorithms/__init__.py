"""纯计算 —— 给定输入算出结果，不碰存储、不碰网络、不认识任何宿主。

产品规范 §18 要求这一层单独存在：「不能再把 contract、算法、存储、
宿主 runtime 接线混成一层」。先前这些模块散在包的顶层，和 ``kit.py``
（装配和接线）平级，正是那句话说的情况。

**分出来不只是好看。** 这一层是唯一可以放心大改的地方 —— 它没有副作用、
没有顺序依赖，一个函数改错了测试当场红，而不会在某个宿主的生产环境里
变成一条静默错掉的记录。

    attribution    一条观测算哪一天（跨午夜、跨时区、夏令时）
    glance         把感知事实压成一组布尔，给不需要细节的调用方
    history        日聚合的各种形状与合并
    identity       上游给不了稳定 id 时怎么造一个确定性的
    observation    三态判断（测到 / 没测到 / 不能测）
    streaks        连续 N 天
    trend_models   三种趋势模型的数学
    wake           值不值得戳醒（**不回答该不该开口**）

**刻意留在顶层、没搬进来的四个**：``catalog`` / ``fields`` / ``retention``
是声明表不是算法（而且正在被 manifest 取代），``prompts`` 归属还没定
（见给产品方的回复 §三）。把它们塞进 algorithms/ 只会让这个词失去意义。
"""
from __future__ import annotations

from . import (  # noqa: F401
    attribution,
    glance,
    history,
    identity,
    observation,
    streaks,
    trend_models,
    wake,
)

__all__ = [
    "attribution", "glance", "history", "identity",
    "observation", "streaks", "trend_models", "wake",
]
