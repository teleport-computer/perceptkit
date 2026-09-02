"""一条测量该算哪一天。

★ 为什么需要这个模块（设计文档修订 D）：标准里的 effective_time_frame 只描述
  「事实发生于某个点或某段区间」，它不替产品决定「算哪一天」。睡眠 23:00–07:00
  按直觉是「第二天的睡眠」，而在公司待的时长跨午夜时必须切成两天 —— 这是两条
  不同的规则，不能一刀切。

★ 时区：一律用时间自带的 offset。缺 offset 直接报错，不猜 —— 静默按 UTC 或
  按用户当前时区重解释，会让历史数据在用户出国时集体漂一天。

★ 零 I/O、不读时钟。
"""
from __future__ import annotations

import datetime as _dt
import zoneinfo as _zoneinfo

from ..contracts import _time

INSTANT = "instant"                        # 单点：按其自带 offset 的本地日期
EPISODE_END = "episode_end"                # 区间：整体归结束（醒来）那天
SPLIT_AT_MIDNIGHT = "split_at_midnight"    # 可加总时长：按本地午夜切分
SOURCE_LOCAL_DATE = "source_local_date"    # 周期事件：用来源记录的本地日期，不重解释

ATTRIBUTION: dict[str, str] = {
    "health_sleep": EPISODE_END,
    "health_workout": EPISODE_END,
    "health_body": INSTANT,
    "health_vitals": INSTANT,
    "health_metabolic": INSTANT,
    "health_activity": INSTANT,
    "health_mood": INSTANT,
    "health_cycle": SOURCE_LOCAL_DATE,
    "location_signal": SPLIT_AT_MIDNIGHT,
    "playback": SPLIT_AT_MIDNIGHT,
    "motion_state": SPLIT_AT_MIDNIGHT,
    "focus": SPLIT_AT_MIDNIGHT,
    "audio_route": SPLIT_AT_MIDNIGHT,
    # calendar_next_event / reminders：条目自带日期(来自日历/提醒事项存储)，
    # 按用户「当前」时区重解释会在他出国时把一场会议错移到另一天 ——
    # 正是这个模块存在的目的要防的那类漂移，所以用来源本地日期,不重算。
    "calendar_next_event": SOURCE_LOCAL_DATE,
    "reminders": SOURCE_LOCAL_DATE,
    # weather 现在的 SHAPE 是 NUMERIC_DIST（history.py），是单点测量，
    # 跟体重/心率同类 —— 归属规则是 INSTANT。
    # ⚠️ Codex code_review 2026-08-23 抓到：早先按"weather 即将改成仅当前+预报、
    # 不再产生 rollup"的未来态把这条声明成了"故意缺席"，但 SHAPE/record_daily
    # 从未真的改过去，导致四张声明表互相矛盾（history 说存、retention/attribution
    # 说不存）。真要把 weather 改成不存历史时，这一行、retention.RETENTION_DAYS
    # 里的 weather 条目、history.SHAPE 里的 weather 条目，三处必须在同一批
    # 一起删，不许只删一处。
    "weather": INSTANT,
}


def _aware(raw: str) -> _dt.datetime:
    """解析成带 offset 的时刻。没有 offset 就报错 —— 不猜。

    🔴 **走 ``contracts._time`` 那一个解析器，不许自己调 ``fromisoformat``。**
    3.10 的 ``fromisoformat`` 只认三位或六位小数秒，真实 producer 发的是
    ``23:59:59.96+08:00`` 这种两位的；自己调就是"3.12 上全绿、3.10 上
    整条上报被拒"。这个包声明 ``requires-python>=3.10``，那就得真的能跑。
    """
    try:
        return _time.parse_timestamp(raw, field="时间")
    except _time.TimestampError as exc:
        raise ValueError(str(exc)) from exc


def _utc_naive(dtobj: _dt.datetime) -> _dt.datetime:
    """把一个 aware datetime 精确转成"UTC 等价的裸 datetime"，用于比较/相减。

    见 ``split_across_midnight`` 里的坑注释：两个 aware datetime 若共享同一个
    ``tzinfo`` 对象，CPython 的 `<`/`-` 会走一条忽略 offset 变化的捷径，对
    ``ZoneInfo`` 这种 offset 会随日期变的时区算错。显式转成裸 UTC datetime
    绕开这条捷径；用 timedelta 精确运算，不经过 float epoch，避免精度损失。
    """
    return dtobj.replace(tzinfo=None) - dtobj.utcoffset()


def _local(dtobj: _dt.datetime, tz: str | None) -> _dt.datetime:
    """换算到观测声明的那个时区。没声明就用时间戳自带的 offset。

    ★ **这两件事不是一回事，混用会把数据归错天。**

    时间戳自带的是一个**固定 offset**；``tz`` 是一整套带换日规则的时区。
    producer 完全可以（而且常常）用 UTC 发 ``occurred_at``、把 IANA 时区另放
    在观测的 ``timezone`` 字段里 —— 那时按 offset 算出来的是 UTC 日期，不是
    用户的本地日期。

    实例（这就是它被写出来的原因）：上海用户早上 7 点，``occurred_at`` 是
    ``T-1 23:00+00:00``。按 offset 算 = 前一天。**每天本地 00:00–08:00 的数据
    全部落到前一天**，"昨天走了多少步""昨晚睡了几小时"跟着一起偏。
    """
    if not tz:
        return dtobj
    try:
        return dtobj.astimezone(_zoneinfo.ZoneInfo(tz))
    except Exception:                              # noqa: BLE001
        # 时区名不认识时按 offset 算 —— 这是降级，不是等价物，
        # 但比整条观测拒收好：调用方已经在别处对时区名做了校验。
        return dtobj


def attribute_instant(when: str, *, tz: str | None = None) -> str:
    """单点测量：归到它发生时、**当地**的那一天。

    ``tz`` 是观测声明的 IANA 时区。不传时退回时间戳自带的 offset —— 见
    ``_local`` 里说明这两者为什么不等价。
    """
    return _local(_aware(when), tz).date().isoformat()


def attribute_episode(start: str, end: str, *, tz: str | None = None) -> str:
    """区间事件（睡眠、一次运动）：整体归结束那天的**当地**日期。"""
    s, e = _aware(start), _aware(end)
    if e < s:
        raise ValueError(f"区间结束早于开始：{start!r} -> {end!r}")
    return _local(e, tz).date().isoformat()


def split_across_midnight(start: str, end: str, *, tz: str | None = None) -> list[tuple[str, float]]:
    """可加总的时长：按本地午夜切开，返回 ``[(本地日期, 分钟数), ...]``。

    ``tz``（可选）：传入 IANA 时区名（如 ``"America/New_York"``）时，用
    ``zoneinfo.ZoneInfo`` 按该时区的真实换日规则计算本地午夜 —— 跨夏令时
    切换的那一天会正确算出 23 小时（春季提前）或 25 小时（秋季回退），
    不会被硬当成 1440 分钟。

    不传 ``tz`` 时（默认）：本地午夜用 ``start`` 自带的 offset 推算 —— 这是
    一个**固定** offset，不是一整套带换日规则的时区。★ 老实说明局限：跨夏
    令时切换的那一天，本函数并不知道当地钟表在那天真的跳了一小时，切分点
    仍按「每天 1440 分钟」机械推进，那一天算出来的分钟数会是错的（例如秋季
    回退的 25 小时天，仍会被切成 24×60）。要正确处理夏令时切换，必须显式
    传入 ``tz``。

    各段分钟数不做单独四舍五入 —— 调用方需要展示或落库时再自己 round，
    否则多段各自舍入会破坏「各段之和 = 总时长」这个不变式（例如跨午夜的
    0.08 秒会被两段各自舍成 0.001，加总 0.002，而真实值是 0.001333...）。
    """
    s, e = _aware(start), _aware(end)
    if e < s:
        raise ValueError(f"区间结束早于开始：{start!r} -> {end!r}")
    if e == s:
        return []

    zone = _zoneinfo.ZoneInfo(tz) if tz else None
    if zone is not None:
        s = s.astimezone(zone)
        e = e.astimezone(zone)

    # ★ 坑（真实调试过，别删）：下面比较/相减都拿 `_utc_naive()` 转换过的值，
    #   不直接对两个 aware datetime 做 `<` / `-`。原因是 CPython 对"两个 aware
    #   datetime 共享同一个 tzinfo 对象"有一条捷径：直接比较墙上时间、完全不
    #   重新问 tzinfo 要 offset —— 对固定 offset 的 tzinfo 这条捷径没问题
    #   （offset 反正不变），但 ZoneInfo 的 offset 会随日期变（换季），同一个
    #   ZoneInfo 对象在夏令时切换前后 offset 不同，这条捷径会把跨切换的那一段
    #   整整算错一小时。`_utc_naive()` 每次都显式调用 `.utcoffset()` 拿当下
    #   那个墙上时间对应的真实 offset，不吃这条捷径；且转换本身是 timedelta
    #   精确运算（不经过 float epoch），不会像 `.timestamp()` 那样在秒级精度
    #   之外引入浮点误差。
    e_utc = _utc_naive(e)
    out: list[tuple[str, float]] = []
    cursor = s
    cursor_utc = _utc_naive(cursor)
    while cursor_utc < e_utc:
        next_midnight = _dt.datetime.combine(
            cursor.date() + _dt.timedelta(days=1),
            _dt.time(0, 0),
            tzinfo=cursor.tzinfo,
        )
        next_midnight_utc = _utc_naive(next_midnight)
        if next_midnight_utc < e_utc:
            chunk_end, chunk_end_utc = next_midnight, next_midnight_utc
        else:
            chunk_end, chunk_end_utc = e, e_utc
        minutes = (chunk_end_utc - cursor_utc).total_seconds() / 60.0
        out.append((cursor.date().isoformat(), minutes))
        cursor, cursor_utc = chunk_end, chunk_end_utc
    return out
