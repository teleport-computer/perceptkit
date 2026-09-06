"""来源撤回：之前记下的那条事实，来源说它不作数了。

**为什么不是第四个 availability 状态。**

``availability`` 回答的是一个问题：**这次到底有没有拿到数**。
三个取值（observed / no_data / unavailable）都在描述**这一次观测尝试**。

撤回不回答那个问题。它是对**之前某条事实**的陈述：那条还作不作数。
两个正交的轴，硬塞进一个状态位会同时坏掉两头：

    塞进去   `availability.py` 当初刻意避开的坑回来了 —— 每个消费方都得记住
             "observed 之外还有一种非 observed 是要重选当前值的"，迟早有人漏
    不塞     旧宿主看到不认识的状态会 normalize 成 unavailable，
             而 unavailable 的既有行为是**保留上一个可靠值当 last_known** ——
             正好是撤回最不该发生的事：被删掉的数值继续显示

所以撤回走自己的通道。旧宿主没实现就是**从不撤回**（fail-closed），
而不是"撤回了但表现成传感器故障"。

## 撤回和"读不到"的区别，用产品语言说

    unavailable      传感器暂时读不到 → 当前值保留上一次的，标成 last_known
                     agent 能说"你上次是 70kg，现在读不到了"

    撤回             用户在健康 app 里删掉了那条记录 → 当前值重选下一条仍有效的
                     agent **不该**再知道那个数值。那天的趋势有个缺口，
                     而不是"那天是 70kg"

## 谁赢：观察即支配

同一条源事实上，撤回一旦被**某个成功持久化的批次**观察到，就支配它。

刻意**不用时间戳排序**：
    · 来源的删除对象是临时的，事后补不回来
    · 客户端时钟可以被改
    · 增量游标是不透明的，不保证可比较
拿这三样中的任何一个排序，都会出现"先删后传的旧读数把当前值复活"。

撤回时刻只做审计，不参与胜负。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._time import parse_timestamp


@dataclass(frozen=True)
class Retraction:
    """来源撤回了一条已经记下的事实。

    身份用 ``source_event_id`` —— 和当初记下它时用的是同一个，
    否则撤回落不到任何东西上。这也是为什么逐条样本必须带稳定身份：
    没有它，"删掉那条"这句话在本地没有指向。
    """

    subject_id: str
    signal: str
    #: 当初记下这条事实时用的那个身份。
    source_event_id: str
    #: 哪个来源系统撤回的。身份的一部分 —— 不同来源可能撞 id。
    source: str
    #: 什么时候观察到这次撤回。**只做审计，不参与胜负排序。**
    observed_at: datetime

    def __post_init__(self) -> None:
        for name in ("subject_id", "signal", "source_event_id", "source"):
            if not (getattr(self, name) or "").strip():
                raise ValueError(
                    f"{name} 不能为空：撤回落不到具体哪条事实上时，"
                    f"要么什么都不做、要么删错东西，两种都不可逆"
                )
        parse_timestamp(self.observed_at, field="observed_at")


__all__ = ["Retraction"]
