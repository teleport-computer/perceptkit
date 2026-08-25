"""「连续 N 天偏离」到底怎么算。

★ 四个坑（设计文档修订 G，第④条 2026-08-23 review 补）：
  ① 按「最近 N 行」算 —— 历史读法会丢掉空日，于是周一三五被排在一起，
     看起来像连续三天。必须按日历日期判相邻。
  ② 把「没数据」当成「正常」或「继续」—— 没戴表的那天既不能算偏离，
     也不能让连续性跨过去。
  ③ 异常持续超过冷却期就再叫一次 —— 同一个 episode 会被反复播报。
     所以是 edge-trigger：只在从正常跨入异常那一刻叫。
  ④ 信任调用方「一定是按日期升序传入」—— 一旦上游（比如读历史表那层）的
     排序出了问题，会安静地数错，而不是报错或归零。所以 ``current_streak``
     的输入契约是顺序无关的：自己先排序再走，不假设调用方给对了。

★ 零 I/O、不读时钟：日期与状态都由调用方给。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import datetime as _dt
from typing import NamedTuple

from .observation import breaks_streak


class Trigger(NamedTuple):
    """``should_trigger`` 的返回值。

    ★ 为什么加 ``next_firing``（Codex code_review 2026-08-23 抓到）：
      旧签名只返回 ``(fire, reason)``，``already_firing`` 这个锁存状态完全
      交给调用方自己维护——但调用方唯一能看到的信号就是这个返回值，如果
      不把"这段异常有没有结束"算在这里，调用方要么手写一套重复的判断
      （容易和这里的日历/edge-trigger 逻辑漂移），要么干脆一直传
      ``already_firing=True`` 直到手动清掉，锁存了就再也打不开——
      「恢复过再复发」永远叫不出第二次。
      现在锁存的开合由 ``current_streak`` 算出来，调用方只需要把
      ``next_firing`` 原样存起来、下次调用原样传回来。
    """
    fire: bool
    reason: str
    next_firing: bool


def _date(raw) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _sort_key(row) -> tuple[bool, _dt.date]:
    """结构损坏的行（非 dict、日期解析不了）统一排到最后，让它们在倒序
    遍历时第一个被撞见 —— 与「结构损坏即不可信」的降级方式保持一致。"""
    d = _date(row.get("date")) if isinstance(row, Mapping) else None
    return (d is None, d or _dt.date.min)


def current_streak(days: Sequence[Mapping]) -> int:
    """从最后一天往回数，连续的「有观测且偏离」有几天。

    ``days``：``[{"date", "state", "abnormal"}]``，顺序无关 —— 不依赖调用方
    保证升序，函数自己先按日期排序再从最近一天往回走。结构损坏的行（非
    dict、日期解析不了）排到最后，一旦撞见就安全降级、返回 0，而不是抛错
    或凭空报出一个看似合理实则算错的数字。
    日历上不相邻、观测缺失、或不偏离，三者任一都终止计数。
    """
    ordered = sorted(list(days or ()), key=_sort_key)
    count = 0
    expected: _dt.date | None = None
    for row in reversed(ordered):
        if not isinstance(row, Mapping):
            break
        d = _date(row.get("date"))
        if d is None:
            break
        if expected is not None and d != expected:
            break                       # 日历上断开了
        if breaks_streak(str(row.get("state") or "")):
            break                       # 没观测：不算偏离，也不许跨过去
        if not row.get("abnormal"):
            break                       # 回到正常
        count += 1
        expected = d - _dt.timedelta(days=1)
    return count


def should_trigger(
    days: Sequence[Mapping],
    *,
    min_days: int,
    already_firing: bool,
) -> Trigger:
    """要不要为这段连续偏离发一次事件，以及调用方下次该存的锁存状态。

    ``already_firing``：调用方记录的「这一段异常是否已经叫过」。它就是
    hysteresis —— 只有恢复过（连续中断）之后再次达标，才算新事件。
    返回的 ``reason`` 是给日志和回执用的机器可读串，不是给模型看的措辞。

    ``already_firing=True`` 时不会无条件一直锁着：会用 ``current_streak``
    检查这段异常是不是已经结束（回到 0）——已经结束就把 ``next_firing``
    复位成 ``False``，这样调用方下次传回来就能重新触发，而不是永久锁死。
    复位本身不叫醒（那一刻还没有新的连续异常），真正的下一次叫醒要等
    新一段异常重新攒够 ``min_days``。
    """
    streak = current_streak(days)
    if already_firing:
        if streak == 0:
            return Trigger(False, "recovered", False)
        return Trigger(False, "already_firing", True)
    if streak < max(1, int(min_days)):
        return Trigger(False, "streak_too_short", False)
    return Trigger(True, "streak_reached", True)
