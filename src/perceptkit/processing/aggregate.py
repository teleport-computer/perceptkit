"""聚合分派 —— manifest 说用哪种算法,这里去调。

**算法只有一份。** 这里不重新实现任何聚合,只是把 manifest 按【字段】声明的
``aggregation_strategy`` 路由到 ``history`` 里那批已经测过的 merger。
旧的按信号查表那条路(``history.record_daily``)原样保留,两条路共用同一批算法,
不会漂移。
"""
from __future__ import annotations

from typing import Any, Mapping

from .. import history
from ..manifest.types import SignalDefinition

#: manifest 的策略名 -> history 的 shape 名。
#: ``daily_total`` 走 CUMULATIVE(日内单调累加,当天代表值取 max=总数)。
_STRATEGY_TO_SHAPE: dict[str, str] = {
    "daily_total": history.CUMULATIVE,
    "cumulative": history.CUMULATIVE,
    "numeric_dist": history.NUMERIC_DIST,
    "main_of_day": history.MAIN_OF_DAY,
    "duration_by_state": history.DURATION_BY_STATE,
    "event_list": history.EVENT_LIST,
    "tally": history.TALLY,
}


def aggregating_fields(sig: SignalDefinition) -> list[tuple[str, str]]:
    """这个信号有哪些字段要聚合,各用哪种 shape。返回 ``[(字段名, shape), ...]``。"""
    out: list[tuple[str, str]] = []
    for f in sig.fields:
        shape = _STRATEGY_TO_SHAPE.get(f.aggregation_strategy)
        if shape is not None:
            out.append((f.key, shape))
    return out


def fold_into_day(
    prev_doc: Mapping[str, Any] | None,
    sig: SignalDefinition,
    values: Mapping[str, Any],
    *,
    ts: float | None = None,
) -> dict[str, Any]:
    """把一条观测折进它那天的聚合文档。

    一个信号可以有多个字段各自聚合(比如同时要"每日总数"和"按状态分时长"),
    所以按字段逐个折,而不是整条 payload 一次性丢给某一个 merger。

    ``ts`` 是发生时刻的 epoch 秒。``duration_by_state`` 这类算法靠相邻两条
    观测的时间差累计时长 —— **没有 ts 就只能记状态、算不出时长**。
    """
    doc = dict(prev_doc or {})
    for field_key, shape in aggregating_fields(sig):
        if field_key not in values:
            continue
        doc = history.apply_shape(
            shape, doc, values,
            signal=sig.key,
            # duration_by_state 需要知道哪个字段是状态标签。manifest 按字段
            # 声明,所以这里能直接给出来,不用像旧路径那样按信号查表。
            state_field=field_key if shape == history.DURATION_BY_STATE else None,
            ts=ts,
        )
    return doc


__all__ = ["aggregating_fields", "fold_into_day", "_STRATEGY_TO_SHAPE"]
