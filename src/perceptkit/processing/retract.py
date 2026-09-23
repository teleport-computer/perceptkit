"""来源撤回的处理：记下来、当前值重选、受影响那天重算。

三个动作**必须在同一个事务里**。分开提交会出现"撤回记下了，但当前值还显示着
被删的数值"，而下一轮不会去修 —— 它以为上一轮成功了。

## 当前值重选，和 ``unavailable`` 正好相反

    unavailable   传感器暂时读不到 → **保留**上一个可靠值当 last_known
                  agent 能说"你上次是 70kg，现在读不到了"

    撤回          用户把那条记录删了 → **重选**下一条仍然有效的
                  agent 不该再知道那个数值。那天的趋势有个缺口，
                  不是"那天是 70kg"

所以不能复用 ``availability`` 那条路 —— 那条路的既有行为会把被删的数值
当成 last_known 继续显示，正是撤回最不该发生的事。

## 重选挑哪一条

同一维度上，剩下的观测里 ``occurred_at`` 最新的那条 ``observed``。
一条都不剩就写成"没有有效样本"，**不是**留着旧值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable, Sequence

from ..contracts.records import CurrentProjection
from ..contracts.retraction import Retraction
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort


#: 当前值写入撞车时重读重判几次。和 ingest 那条路同一个数量级 ——
#: 撤回和一次正常上报同时到达是完全正常的。
_MAX_CAS_RETRIES = 3


@dataclass
class RetractionOutcome:
    """这一轮做了什么。

    ``affected_days`` 是 ``(subject, signal, day)`` 三元组，**不是计数** ——
    调用方要拿它去重算那几天的聚合。只给数字的话它知道"有三天要重算"
    却不知道是哪三天。
    """

    recorded: int = 0
    reselected: int = 0
    #: 当前值连续写入竞争失败的次数。**不算成功。**
    contended: int = 0
    affected_days: set[tuple[str, str, date]] = field(default_factory=set)
    #: 撤回记下了，但信号不在 manifest 里，没法重选。
    unknown_signals: list[str] = field(default_factory=list)


def apply_retractions(
    storage: StoragePort,
    retractions: Sequence[Retraction],
    *,
    signals: dict[str, SignalDefinition],
    now: datetime,
    on_affected_day: Callable[[str, str, date], None] | None = None,
) -> RetractionOutcome:
    """记下撤回，重选当前值，报出受影响的日期。

    **受影响日期是返回值的一部分，不只是个计数。** 调用方要拿它去
    ``recompute_aggregates`` —— 只给个数字的话，它知道"有三天要重算"却
    不知道是哪三天，于是要么全量重算、要么什么都不做。早先就是后者：
    撤回记下了，而已经存着的日聚合原封不动。
    """
    outcome = RetractionOutcome()

    with storage.transaction():
        for r in retractions:
            # 🔴 已经记过**不等于收尾做完了**。
            #
            #    原来是 `记过了 → continue`：而第一次很可能正是崩在后面的
            #    重算那一步 —— 撤回提交了、聚合还是被删掉的那个数字，重试
            #    进来直接跳过，于是永远补不回来（外部审查 F3）。
            #    下面这些（重选当前值、报受影响日期、重算）都是幂等的，
            #    重做一遍没有副作用；跳过才有。
            outcome.recorded += 1 if storage.record_retraction(r) else 0
            sig = signals.get(r.signal)
            if sig is None:
                # 信号不在 manifest 里：撤回照样记下（它是事实），
                # 但没法重选 —— 不知道这个信号的当前值长什么样。
                outcome.unknown_signals.append(r.signal)
                continue
            for day in _affected_days(storage, r):
                outcome.affected_days.add((r.subject_id, r.signal, day))
                if on_affected_day is not None:
                    # 🔴 **在同一个事务里重算。** 放到事务外面的话，撤回提交了、
                    #    重算崩了，两边就再也对不上，而下一轮以为上一轮成功了。
                    on_affected_day(r.subject_id, r.signal, day)
            state = _reselect_current(storage, r, sig, now=now)
            if state == "reselected":
                outcome.reselected += 1
            elif state == "contended":
                outcome.contended += 1

    return outcome


def _affected_days(storage: StoragePort, r: Retraction) -> set[date]:
    """被撤回的那条事实落在哪几天。

    一条事实通常只落一天，但跨午夜的睡眠/运动会被切成两天 ——
    只重算一天会留下另一半错的。

    ``current_only`` 的信号没有观测，返回空集：它们本来也没有日聚合。
    """
    if not hasattr(storage, "list_observations"):
        return set()
    page = _all_observations(storage, r.subject_id, r.signal)
    return {o.effective_local_date for o in page
            if (o.source, o.source_event_id) == (r.source, r.source_event_id)}


def _all_observations(storage: StoragePort, subject_id: str, signal: str) -> list:
    rows, cursor = storage.list_observations(
        subject_id=subject_id, signal=signal, cursor=None, limit=500)
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=subject_id, signal=signal, cursor=cursor, limit=500)
        page.extend(more)
    return page


def _reselect_current(storage: StoragePort, r: Retraction,
                      sig: SignalDefinition, *, now: datetime) -> str:
    """把当前值重选成下一条仍然有效的。返回做了什么。

    靠 ``CurrentProjection`` 自己带的 ``(source, source_event_id)`` 定位 ——
    **不反查观测**：``current_only`` 的信号压根不写观测，反查得到空，
    于是撤回记下了、当前值纹丝不动。
    """
    for _ in range(_MAX_CAS_RETRIES):
        existing = storage.get_current(
            subject_id=r.subject_id, signals=[r.signal]).get(r.signal) or ()
        hit = [c for c in existing
               if (c.source, c.source_event_id) == (r.source, r.source_event_id)]
        if not hit:
            return "untouched"              # 被撤回的那条不是当前值

        page = (_all_observations(storage, r.subject_id, r.signal)
                if hasattr(storage, "list_observations") else [])
        retracted = {(x.source, x.source_event_id)
                     for x in storage.list_retractions(
                         subject_id=r.subject_id, signal=r.signal)}

        lost = False
        for current in hit:
            pool = [o for o in page
                    if o.availability == "observed"
                    and (o.source, o.source_event_id) not in retracted
                    and sig.dimension_key_for(o.typed_value) == current.dimension_key]
            pool.sort(key=lambda o: (o.occurred_at, o.observation_id))
            winner = pool[-1] if pool else None
            ok = storage.compare_and_put_current(
                CurrentProjection(
                    subject_id=r.subject_id,
                    signal=r.signal,
                    dimension_key=current.dimension_key,
                    # 🔴 一条都不剩时写 None，**不是**留着被撤回的那个值。
                    # 留着的话 agent 会继续说出一个用户已经删掉的数字。
                    typed_value=winner.typed_value if winner else None,
                    availability="observed" if winner else "no_data",
                    observed_at=winner.occurred_at if winner else r.observed_at,
                    received_at=now,
                    expires_at=None,
                    source_observation_id=winner.observation_id if winner else None,
                    source=winner.source if winner else None,
                    source_event_id=winner.source_event_id if winner else None,
                    source_revision=winner.source_revision if winner else None,
                    version=current.version + 1,
                    content_digest=current.content_digest,
                ),
                expected_version=current.version,
            )
            if not ok:
                # 有人在我们读之后写了。**不能报告成功** —— 早先忽略这个
                # 返回值，于是并发下会出现"撤回说重选完了、当前值还是旧的"。
                lost = True
                break
        if not lost:
            return "reselected"
    return "contended"


__all__ = ["apply_retractions", "RetractionOutcome"]


def drop_retracted(storage: StoragePort, rows: list, *,
                    subject_id: str, signal: str) -> list:
    """去掉来源已经撤回的那些观测。

    **不是把观测删掉，是折聚合时不算它。** 观测留着，"那天曾经有条记录、
    后来被来源删了"才答得出来；抹掉的话那天只是凭空少一块，没人说得清为什么。

    宿主没实现撤回端口时原样返回 —— 那等于"从不撤回"，是安全的那一边：
    宁可多留一条已经被删的旧数据，也不要因为端口缺失把好数据当成撤回删掉。
    """
    ids = {o.source_event_id for o in rows if o.source_event_id}
    if not ids:
        return rows
    lookup = getattr(storage, "list_retractions", None)
    if lookup is None:
        return rows
    # 🔴 (source, id) 成对比，不能只比 id。同一个 subject 下 iOS 和 Google
    # 完全可能用同一个 source_event_id —— 只比 id 的话，撤回 iOS 那条会
    # 把 Google 那条一起从聚合里踢掉。
    retracted = {(r.source, r.source_event_id) for r in lookup(
        subject_id=subject_id, signal=signal, source_event_ids=sorted(ids))}
    if not retracted:
        return rows
    return [o for o in rows
            if (o.source, o.source_event_id) not in retracted]


def is_retracted(storage: StoragePort, *, subject_id: str, signal: str,
                 source: str, source_event_id: str | None) -> bool:
    """这条事实是不是已经被来源撤回过了。

    用在**收数据的入口**：撤回先到、原样本后到是常态（重传、乱序、补传）。
    不查的话这条后到的样本会被当成一条新事实，当前值和日聚合又变回那个
    已经被用户删掉的数 —— 用户在健康 app 里删了，过一会儿它自己回来了
    （外部审查 F2）。
    """
    if not source_event_id:
        return False
    lookup = getattr(storage, "list_retractions", None)
    if lookup is None:
        return False
    return any(
        x.source == source and x.source_event_id == source_event_id
        for x in lookup(subject_id=subject_id, signal=signal,
                        source_event_ids=[source_event_id])
    )
