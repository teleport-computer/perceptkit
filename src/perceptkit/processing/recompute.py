"""按新算法版本重算日聚合。

产品规范 §12「聚合算法升级 | 使用新 aggregation_version 重算，不静默改写语义」。
先前只做了一半：换版本号、按版本过滤（所以不会两种口径混着 fold），
**但没有任何东西真的把历史重算出来** —— 升级之后旧日子永远停在旧版本，
新旧数据一起查就是一段一段口径不同的曲线。

## 这件事唯一真正危险的地方

重算是**从明细重新折出来**的。而明细有保留期，聚合可以永久 ——
典型形态就是"明细 1 年、聚合永久"。所以：

    2023 年的那一天    明细早就清理了 → 能读到的只有零星几条,甚至一条没有
                       照样折一遍 → 得到一个"当天走了 200 步"的永久统计
                       → 数字错了一个数量级,没有任何地方报错,而且旧值已被覆盖

**所以默认拒绝重算明细可能已经不完整的日子**，而不是尽力而为。
真要重算那些日子，调用方必须显式说"我知道明细不全，仍然要"
（``allow_incomplete=True``），并且结果里会标出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Mapping

from ..contracts.records import DailyAggregate
from ..manifest.types import PERMANENT, SignalDefinition
from ..ports.storage import StoragePort
from . import aggregate as _aggregate


@dataclass
class RecomputeOutcome:
    """重算了哪些天、拒绝了哪些天、为什么拒绝。"""

    rebuilt: list[date] = field(default_factory=list)
    #: ``(那一天, 为什么没算)``
    skipped: list[tuple[date, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.skipped


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def details_may_be_incomplete(
    sig: SignalDefinition, day: date, *, today: date,
) -> bool:
    """这一天的明细有没有可能已经被保留期清掉了。

    **宁可误判成"可能不全"**：误判的代价是拒绝重算一天（可恢复），
    反过来的代价是写下一个错了一个数量级的永久统计（不可恢复）。
    """
    if sig.history_retention_days == PERMANENT:
        return False
    if not sig.stores_history:
        return True                     # 根本不存明细，无从重算
    cutoff = today - timedelta(days=sig.history_retention_days)
    return day < cutoff


def _revision_key(raw: object) -> tuple[int, str]:
    """修订号的比较键。数字按数字比，其余按字符串比，两者不混。

    ``"10"`` 和 ``10`` 是不同的东西：全按字符串比的话 ``"10" < "9"``，
    第 10 版会被第 9 版盖掉。所以数字排一档、字符串排另一档，
    并且**数字档永远小于字符串档**——不给两个不可比的值编一个假的顺序。
    """
    if raw is None:
        return (0, "")
    text = str(raw)
    try:
        return (1, f"{int(text):020d}")
    except ValueError:
        return (2, text)


def _canonical(rows: list, sig: SignalDefinition) -> list:
    """同一件源事实的多个修订，只留最新那一版。

    ``source_revision`` 原来只用在当前值上（高版本替换低版本），**历史和聚合
    完全没用它**。于是修订过的那天会同时折进错值和改正值：

        体重 70.5kg（revision 1）→ 用户在健康 app 里改成 68.5（revision 2）
        重算那天 → 两条都在 → 平均值 69.5，一个从来没发生过的数字

    而且这个错**只有重算时才现形**：当前值那条路是对的，所以"改过之后当前
    显示对了"会让人以为整条链路都对了。

    只对按 ``source_event_id`` 定身份的信号生效 —— 别的信号一条观测就是
    一件独立的事，本来就不该合并。
    """
    if sig.identity_strategy != "source_event_id":
        return rows
    groups: dict[str, list] = {}
    for o in rows:
        if o.source_event_id is not None:       # 没有源身份的，各算各的
            groups.setdefault(o.source_event_id, []).append(o)

    winners = set()
    for key, members in groups.items():
        # **一组里全都没有修订号 = 没有"哪版更新"的信息，一条都不合并。**
        # 合并的话就等于替这些观测编一个"后来的覆盖先来的"的顺序，而那正好
        # 是 `cumulative` 现在**不**采用的规则（它取 max）—— 重算和增量折叠
        # 会给出两个不同的数，同一份数据看你从哪条路读。
        # 修订语义该不该改成"最新的赢"是另一件事，得先定，不能从这里溜进去。
        if all(o.source_revision is None for o in members):
            winners.update(id(o) for o in members)
            continue
        best = max(members, key=lambda o: (_revision_key(o.source_revision),
                                           o.occurred_at, o.observation_id))
        winners.add(id(best))
    return [o for o in rows if o.source_event_id is None or id(o) in winners]


def recompute_day(
    storage: StoragePort,
    sig: SignalDefinition,
    *,
    subject_id: str,
    day: date,
    version: int,
    updated_at: datetime,
) -> DailyAggregate:
    """把某一天的明细重新折成一份聚合文档。**不写库**，由调用方决定写不写。"""
    # 刻意**不按 occurred_at 开时间窗**：一条观测归到哪一天由
    # effective_local_date 决定，跨午夜的睡眠整段归到醒来那天，
    # 按 occurred_at 截窗口会把它切掉一半。宁可多读再过滤。
    rows, cursor = storage.list_observations(
        subject_id=subject_id, signal=sig.key, cursor=None, limit=500,
    )
    # 明细可能不止一页。重算必须读全 —— 只读第一页就是在算一份残缺统计。
    page = list(rows)
    while cursor:
        more, cursor = storage.list_observations(
            subject_id=subject_id, signal=sig.key, cursor=cursor, limit=500,
        )
        page.extend(more)

    same_day = [o for o in page if o.effective_local_date == day]
    same_day = _canonical(same_day, sig)
    same_day = [o for o in same_day if o.availability == "observed"]
    same_day.sort(key=lambda o: (o.occurred_at, o.observation_id))

    doc: dict = {}
    for obs in same_day:
        doc = _aggregate.fold_into_day(
            doc, sig, obs.typed_value or {}, ts=_epoch(obs.occurred_at),
        )
    return DailyAggregate(
        subject_id=subject_id,
        signal=sig.key,
        local_date=day,
        aggregation_kind="daily",
        aggregation_version=version,
        typed_aggregate=doc,
        timezone_attribution=same_day[0].timezone if same_day else None,
        source_coverage={
            "observations": len(same_day),
            # 标出来这份统计是重算的，不是随观测一条条折出来的。
            # 排查"这个数字怎么变了"时，第一件事就是看它是不是被重算过。
            "recomputed": True,
        },
        updated_at=updated_at,
    )


def recompute_range(
    storage: StoragePort,
    signals: Mapping[str, SignalDefinition],
    *,
    subject_id: str,
    signal: str,
    start_date: date,
    end_date: date,
    version: int,
    now: datetime,
    allow_incomplete: bool = False,
) -> RecomputeOutcome:
    """按 ``version`` 重算一段日期的聚合。

    ``now`` 由调用方传 —— 这个包不读时钟。它只用来判断"这一天的明细
    是不是已经过了保留期"。

    ``allow_incomplete=False``（默认）时，明细可能已经不全的日子**不算**，
    并在 ``skipped`` 里说明原因。
    """
    outcome = RecomputeOutcome()
    sig = signals.get(signal)
    if sig is None:
        outcome.skipped.append((start_date, f"manifest 里没有 {signal}"))
        return outcome
    if not sig.stores_history:
        outcome.skipped.append((start_date, f"{signal} 不存明细，无从重算"))
        return outcome

    today = now.date()
    day = start_date
    while day <= end_date:
        if not allow_incomplete and details_may_be_incomplete(sig, day, today=today):
            outcome.skipped.append((
                day,
                f"{day} 早于明细保留期（{sig.history_retention_days} 天），"
                "明细可能已被清理。拿残缺明细重算会写下一个数量级都不对的统计，"
                "而且旧值会被覆盖、不可恢复。确实要算就传 allow_incomplete=True",
            ))
            day += timedelta(days=1)
            continue
        storage.put_aggregate(recompute_day(
            storage, sig, subject_id=subject_id, day=day,
            version=version, updated_at=now,
        ))
        outcome.rebuilt.append(day)
        day += timedelta(days=1)
    return outcome


__all__ = [
    "RecomputeOutcome", "details_may_be_incomplete",
    "recompute_day", "recompute_range",
]
