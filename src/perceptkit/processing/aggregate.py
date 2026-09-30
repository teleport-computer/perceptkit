"""聚合分派 —— manifest 说用哪种算法,这里去调。

**算法只有一份。** 这里不重新实现任何聚合,只是把 manifest 按【字段】声明的
``aggregation_strategy`` 路由到 ``history`` 里那批已经测过的 merger。
旧的按信号查表那条路(``history.record_daily``)原样保留,两条路共用同一批算法,
不会漂移。
"""
from __future__ import annotations

from typing import Any, Mapping

from ..algorithms import history
from ..manifest.types import SignalDefinition

#: manifest 的策略名 -> history 的 shape 名。
#: ``daily_total`` 走 CUMULATIVE(日内单调累加,当天代表值取 max=总数)。
_STRATEGY_TO_SHAPE: dict[str, str] = {
    "daily_total": history.CUMULATIVE,
    "cumulative": history.CUMULATIVE,
    # 每次事件贡献一份、当天求和。**不是** daily_total：那个取 max，
    # 用来数「打开了几次」会永远得到 1。
    "occurrence_count": history.OCCURRENCE_COUNT,
    "numeric_dist": history.NUMERIC_DIST,
    "main_of_day": history.MAIN_OF_DAY,
    "duration_by_state": history.DURATION_BY_STATE,
    # 时长由观测直接给出、按状态分桶求和（睡眠）。挂在**时长字段**上，
    # 不是挂在状态字段上 —— 真正被加总的是分钟，状态只是桶的键。
    "duration_sum_by_state": history.DURATION_SUM_BY_STATE,
    "daily_sum": history.DAILY_SUM,
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
    revision: Any = None,
    counter_epoch: str | None = None,
) -> dict[str, Any]:
    """把一条观测折进它那天的聚合文档。

    一个信号可以有多个字段各自聚合(比如同时要"每日总数"和"按状态分时长"),
    所以按字段逐个折,而不是整条 payload 一次性丢给某一个 merger。

    ``ts`` 是发生时刻的 epoch 秒。``duration_by_state`` 这类算法靠相邻两条
    观测的时间差累计时长 —— **没有 ts 就只能记状态、算不出时长**。
    """
    doc = dict(prev_doc or {})
    # 跨字段的策略需要状态标签那一格。manifest 里状态字段自己不声明聚合，
    # 所以从 dimension_fields 取。current_policy=none 的信号（睡眠）仍有
    # 聚合维度；声明了维度并不表示它必须发布 Current。
    bucket_field = sig.dimension_fields[0] if sig.dimension_fields else None
    for field_key, shape in aggregating_fields(sig):
        if field_key not in values:
            continue
        if shape == history.DURATION_SUM_BY_STATE:
            # **唯一的跨字段策略**：状态标签和时长分在两个字段里，这条必须
            # 同时看到两格。下面那条「一次只喂一个字段」的纪律仍然管着其余
            # 所有策略 —— 这里是显式开的一个口子，不是把纪律取消了。
            doc = history.apply_shape(
                shape, doc,
                {bucket_field: values.get(bucket_field),
                 field_key: values[field_key]},
                signal=sig.key, state_field=bucket_field,
                duration_field=field_key, ts=ts,
            )
            continue
        doc = history.apply_shape(
            # **只喂这一个字段。** merger 会把收到的 mapping 里的每个字段都
            # 按自己那套算法写一遍 —— 整条 payload 递进去,一个字段声明的
            # 算法就会写到所有字段头上。两个后果都真发生过:
            #
            #   声明 none 的字段被凭空聚合  weather 只有 temperature_c 声明了
            #                              numeric_dist,结果 uv_index、湿度、
            #                              体感温度全被写了 min/max/sum/count。
            #   同信号两种算法互相覆盖      health_vitals 同时有 numeric_dist
            #                              (静息心率) 和 main_of_day (vo2_max):
            #                              后者把字段写成裸数字,当天第二条上报
            #                              进来时前者读 cell.get("min") 直接崩,
            #                              **每个用户每天第二次上报都会炸**。
            shape, doc, {field_key: values[field_key]},
            signal=sig.key,
            # duration_by_state 需要知道哪个字段是状态标签。manifest 按字段
            # 声明,所以这里能直接给出来,不用像旧路径那样按信号查表。
            state_field=field_key if shape == history.DURATION_BY_STATE else None,
            ts=ts,
            # 累计型要判「这次是修订、迟到、还是计数器重置」，光有值判不了。
            # 这两格是**上下文**（和 ts 同一性质），不是被聚合的字段 ——
            # 「一次只喂一个字段」那条纪律没被破。
            revision=revision, counter_epoch=counter_epoch,
        )
    return doc


__all__ = ["aggregating_fields", "fold_into_day", "_STRATEGY_TO_SHAPE"]
