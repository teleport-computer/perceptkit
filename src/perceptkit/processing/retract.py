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

from datetime import date, datetime
from typing import Iterable, Sequence

from ..contracts.records import CurrentProjection
from ..contracts.retraction import Retraction
from ..manifest.types import SignalDefinition
from ..ports.storage import StoragePort


def apply_retractions(
    storage: StoragePort,
    retractions: Sequence[Retraction],
    *,
    signals: dict[str, SignalDefinition],
    now: datetime,
) -> dict[str, int]:
    """记下撤回，重选当前值，返回各做了多少。

    受影响日期的重算由调用方接着做 —— 它要按 manifest 的聚合版本走
    ``recompute_range``，那条路已经会排除被撤回的观测。
    """
    recorded = 0
    reselected = 0
    affected: set[tuple[str, str, date]] = set()

    with storage.transaction():
        for r in retractions:
            if not storage.record_retraction(r):
                continue                     # 重传，已经记过
            recorded += 1
            sig = signals.get(r.signal)
            if sig is None:
                # 信号不在 manifest 里：撤回照样记下（它是事实），
                # 但没法重选 —— 不知道这个信号的当前值长什么样。
                continue
            for day in _affected_days(storage, r):
                affected.add((r.subject_id, r.signal, day))
            if _reselect_current(storage, r, sig, now=now):
                reselected += 1

    return {"recorded": recorded, "reselected": reselected,
            "affected_days": len(affected)}


def _affected_days(storage: StoragePort, r: Retraction) -> Iterable[date]:
    """被撤回的那条事实落在哪几天。

    一条事实通常只落一天，但跨午夜的睡眠/运动会被切成两天 ——
    只重算一天会留下另一半错的。
    """
    rows, cursor = storage.list_observations(
        subject_id=r.subject_id, signal=r.signal, cursor=None, limit=500)
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=r.subject_id, signal=r.signal, cursor=cursor, limit=500)
        page.extend(more)
    return {o.effective_local_date for o in page
            if o.source_event_id == r.source_event_id}


def _reselect_current(storage: StoragePort, r: Retraction,
                      sig: SignalDefinition, *, now: datetime) -> bool:
    """把当前值重选成下一条仍然有效的。一条都不剩就写成没有有效样本。"""
    existing = storage.get_current(
        subject_id=r.subject_id, signals=[r.signal]).get(r.signal) or ()
    hit = [c for c in existing if c.source_observation_id
           and _is_from(storage, c, r)]
    if not hit:
        return False                          # 被撤回的那条不是当前值

    rows, cursor = storage.list_observations(
        subject_id=r.subject_id, signal=r.signal, cursor=None, limit=500)
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=r.subject_id, signal=r.signal, cursor=cursor, limit=500)
        page.extend(more)

    retracted = {x.source_event_id for x in storage.list_retractions(
        subject_id=r.subject_id, signal=r.signal)}
    changed = False
    for current in hit:
        pool = [o for o in page
                if o.availability == "observed"
                and o.source_event_id not in retracted
                and sig.dimension_key_for(o.typed_value) == current.dimension_key]
        pool.sort(key=lambda o: (o.occurred_at, o.observation_id))
        winner = pool[-1] if pool else None
        storage.compare_and_put_current(
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
                source_revision=winner.source_revision if winner else None,
                version=current.version + 1,
                # 观测上没有 content_digest（那是投递身份算出来的，不落在
                # 观测记录上）。重选不改内容语义，沿用原来那个 —— 它只被
                # "同身份异内容报冲突"那条路读，而重选不是新内容到达。
                content_digest=current.content_digest,
            ),
            expected_version=current.version,
        )
        changed = True
    return changed


def _is_from(storage: StoragePort, current: CurrentProjection,
             r: Retraction) -> bool:
    """这条当前值是不是来自被撤回的那条事实。"""
    rows, cursor = storage.list_observations(
        subject_id=r.subject_id, signal=r.signal, cursor=None, limit=500)
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=r.subject_id, signal=r.signal, cursor=cursor, limit=500)
        page.extend(more)
    for o in page:
        if o.observation_id == current.source_observation_id:
            return o.source_event_id == r.source_event_id
    return False


__all__ = ["apply_retractions"]
