"""一次观测的三种状态 —— 协议层唯一的可用性词表。

    observed      拿到了可靠的值。``0`` / ``False`` / ``[]`` 都是合法的值,
                  不需要单独一个"观测到零"的状态。
    no_data       查询成功了,但在指定范围内没有样本。
                  典型:昨晚没戴表 —— 不等于"睡了 0 分钟"。
    unavailable   来源当前给不出这项数据(没授权 / 不支持 / 来源报错)。

**为什么是三个而不是四个。** 这个包早期用的是四态(多一个 ``observed_zero``),
问题在于"零"是值域里的一个普通取值,不是一种观测结果:把它提到状态位上,
每个消费方都得记住"observed 和 observed_zero 都算观测到了",迟早有人漏一个。
零值归 ``observed``,状态位就只回答一个问题:**这次到底有没有拿到数**。

``LEGACY_STATES`` 保留旧词表到新词表的映射,让还在用四态的宿主平滑过渡;
新代码一律用这里的三个常量。

**``stale`` 不在这里。** 它不是上报状态,是读取 current 时由
``occurred_at + current_ttl`` 推导出来的结果 —— 属于查询层,不属于上报协议。
"""
from __future__ import annotations

#: 拿到了可靠的值(``0`` / ``False`` / ``[]`` 都算)。
OBSERVED = "observed"

#: 查询成功但范围内没有样本。不参与数值趋势,不当 0 用。
NO_DATA = "no_data"

#: 来源当前无法提供该数据。不覆盖最后一次可靠值。
UNAVAILABLE = "unavailable"

#: 协议层允许的全部状态。
AVAILABILITY_STATES: frozenset[str] = frozenset({OBSERVED, NO_DATA, UNAVAILABLE})

#: 旧四态 -> 新三态。``observed_zero`` 并进 ``observed``,``no_observation``
#: 改名 ``no_data``(语义不变,只是名字更直白)。
LEGACY_STATES: dict[str, str] = {
    "observed": OBSERVED,
    "observed_zero": OBSERVED,
    "no_observation": NO_DATA,
    "no_data": NO_DATA,
    "unavailable": UNAVAILABLE,
}

#: ``unavailable`` 可选的粗粒度原因。刻意不穷举操作系统的每种权限状态 ——
#: 那些属于 adapter 的诊断信息,不该进 agent 的上下文。
UNAVAILABLE_REASONS: frozenset[str] = frozenset({
    "permission_denied",
    "not_supported",
    "source_error",
})


def normalize(state: str) -> str:
    """把任意(含旧词表的)状态归一成三态之一。

    未知状态一律当 ``unavailable`` —— 宁可让 agent 觉得"这项现在没有",
    也不要把一个看不懂的状态当成有效观测混进趋势。
    """
    return LEGACY_STATES.get(state, UNAVAILABLE)


def updates_current(state: str) -> bool:
    """这个状态该不该更新 current 的数值。

    只有 ``observed`` 会。``no_data`` 只更新 coverage/诊断,``unavailable``
    只标记"当前不可用",两者都不许覆盖最后一次可靠值。
    """
    return normalize(state) == OBSERVED


def enters_trend(state: str) -> bool:
    """这个状态该不该进数值趋势和 streak 计算。

    只有 ``observed`` 会。把 ``no_data`` 当 0 塞进趋势,是这类系统最常见的
    错误 —— 十四天里两天没戴表,平均值会被两个 0 直接拉垮。
    """
    return normalize(state) == OBSERVED
