"""标准化 —— 从"producer 发来的东西"到"可以落库的事实"。

这一步做四件事，每件都是后面所有环节的前提：

    按 manifest 校验    类型、单位、枚举、区间、可空性
    定时区              没有时区就没法算归属到哪一天
    算归属日期          按信号声明的策略（瞬时 / 区间结束 / 跨午夜切分 / 上游给）
    算去重身份          按信号声明的策略（上游 id / 确定性摘要 / 单例）

**校验失败不抛异常，返回问题清单。** 一批上报里可能有十条观测，其中两条
字段有问题 —— 因为这两条把另外八条一起丢掉，是最容易让人骂街的设计。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from .. import attribution
from ..contracts._time import to_iso
from ..contracts.context import IngestContext
from ..contracts.observation import Observation
from ..contracts.records import StoredObservation
from ..manifest.types import FieldDefinition, SignalDefinition


@dataclass(frozen=True)
class NormalizedObservation:
    """标准化的结果：一条可落库的观测，外加两种身份。

    **两种身份必须分开，混用会造成两类静默错误。**

        fact_key         这是【哪一条事实】。同一个 HealthKit 样本、同一个
                         电量槽位，永远是同一个 fact_key —— 修订它的时候
                         靠这个找到旧记录。
        identity_digest  这是【哪一次投递】。fact_key + 版本 + 内容。
                         去重问的是这个。

    混用的后果（都发生过，是这两个字段被拆开的原因）：

        用 fact_key 去重 → 电量第二次上报被当成重传丢掉，current 冻结在
                          第一次；同一个样本从 revision 1 改到 2 也被当成
                          重传，用户在健康 App 里的纠错永远生效不了。
        用 identity 找旧记录 → 每次内容一变就成了"新事实"，修订无从谈起。
    """

    stored: StoredObservation
    #: 这是哪一次投递。去重问它。明细按保留期删掉之后，
    #: 这是唯一还能回答"这条处理过没有"的东西。
    identity_digest: str
    #: 这是哪一条事实。修订同一条事实时靠它找到旧记录。
    fact_key: str
    #: 内容摘要。用来分辨"同一时刻的重传"和"同一时刻的不同内容"。
    content_digest: str
    #: 跨午夜的区间会摊到多天：``[(本地日期, 分钟数), ...]``。其余为空。
    day_slices: tuple[tuple[str, float], ...] = ()


def _canonical(value: Any) -> str:
    """稳定的内容序列化 —— 字典顺序不能影响摘要，否则同一份数据会算出两个键。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def _digest(*parts: str) -> str:
    """带长度前缀的拼接再哈希。

    不加长度前缀的话 ``("ab","c")`` 和 ``("a","bc")`` 会撞成同一个键 ——
    这类碰撞不会报错，只会让两条无关的观测被当成重复。
    """
    joined = "\x1f".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "string": (str,),
    "enum": (str,),
    "timestamp": (str, datetime),
    "array": (list, tuple),
    "object": (dict,),
}


def _check_field(fd: FieldDefinition, raw: Any, where: str) -> list[str]:
    if raw is None:
        return [] if fd.nullable else [f"{where}: 不可为空"]

    expected = _PY_TYPES.get(fd.value_type, ())
    # bool 是 int 的子类，不拦一下的话 True 会被当成合法的 integer。
    if fd.value_type in ("integer", "number") and isinstance(raw, bool):
        return [f"{where}: 期望 {fd.value_type}，收到 boolean"]
    if expected and not isinstance(raw, expected):
        return [f"{where}: 期望 {fd.value_type}，收到 {type(raw).__name__}"]

    problems: list[str] = []
    if fd.enum and raw not in fd.enum:
        problems.append(f"{where}: {raw!r} 不在 {list(fd.enum)}")
    if fd.valid_range and isinstance(raw, (int, float)):
        low, high = fd.valid_range
        if low is not None and raw < low:
            problems.append(f"{where}: {raw} 小于下限 {low}")
        if high is not None and raw > high:
            problems.append(f"{where}: {raw} 大于上限 {high}")
    return problems


def validate_value(sig: SignalDefinition, value: Mapping[str, Any] | None) -> list[str]:
    """按 manifest 校验一条观测的 payload。"""
    if value is None:
        return []
    known = sig.field_map()
    problems: list[str] = []
    for key, raw in value.items():
        fd = known.get(key)
        if fd is None:
            # 不认识的字段忽略而不是报错：让 producer 可以先发新字段、
            # 宿主后升级。代价是拼错的字段名不会报错，由下面的必填检查兜。
            continue
        problems += _check_field(fd, raw, f"{sig.key}.{key}")
    for key, fd in known.items():
        if not fd.nullable and key not in value:
            problems.append(f"{sig.key}.{key}: 必填字段缺失")
    return problems


def sanitize_value(
    sig: SignalDefinition, value: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """落库前把不该存的字段去掉。返回 ``(干净的 payload, 被丢掉了什么)``。

    丢两类：

        privacy_class="restricted"  manifest 明说永不持久化(精确坐标、BSSID)。
                                    **在写入边界丢,不是在读取边界过滤** ——
                                    靠读取点自觉过滤,漏一个点就是泄漏,
                                    而且数据已经在库里了,泄漏是既成事实。
        manifest 里没声明的字段      不报错(让 producer 可以先发新字段),
                                    但也不进 canonical value —— 否则任何
                                    未声明字段都能被存下来并查出去。

    被丢掉的字段名会记在 warning 里，方便排查"我发了它怎么查不到"。
    """
    if value is None:
        return None, []
    known = sig.field_map()
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, raw in value.items():
        fd = known.get(key)
        if fd is None:
            dropped.append(f"{sig.key}.{key}（manifest 未声明）")
            continue
        if fd.privacy_class == "restricted":
            dropped.append(f"{sig.key}.{key}（privacy_class=restricted，永不持久化）")
            continue
        clean[key] = raw
    return clean, dropped


# ---------------------------------------------------------------------------
# 时区与归属日期
# ---------------------------------------------------------------------------

def resolve_timezone(
    obs: Observation, *, fallback: str | None = None,
) -> tuple[str | None, str | None]:
    """定这条观测该用哪个时区，返回 ``(时区名, 说明来源的标记)``。

    优先级：观测自带 > 宿主给的兜底 > 无。

    **``occurred_at`` 的偏移不算时区。** 偏移只够算"这条算哪一天"，不够处理
    夏令时 —— 纽约的 ``-04:00`` 和 ``-05:00`` 是同一个时区在不同季节，
    光看偏移分不出来，切换那天（那天有 25 小时）就会算错。

    兜底策略本身还没和产品方对齐（见 OPEN-QUESTIONS B2），所以由调用方传进来，
    不在这里写死。
    """
    if obs.timezone:
        return obs.timezone, "observation"
    if fallback:
        return fallback, "host_fallback"
    return None, None


def effective_date(
    obs: Observation, sig: SignalDefinition, *, timezone_name: str | None,
) -> tuple[date, tuple[tuple[str, float], ...], list[str]]:
    """算这条观测归到哪一天，以及（跨午夜时）怎么摊。"""
    problems: list[str] = []
    value = obs.value or {}
    strategy = sig.attribution_strategy
    iso = to_iso(obs.occurred_at)

    def _parse(day: str) -> date:
        return date.fromisoformat(day)

    if strategy == "source_local_date":
        raw = value.get("local_date")
        if isinstance(raw, str):
            try:
                return _parse(raw), (), problems
            except ValueError:
                problems.append(f"{sig.key}: local_date={raw!r} 不是 YYYY-MM-DD")
        # 上游说好了给本地日期却没给 —— 退回按发生时刻算，并记一笔，
        # 不静默换算法。
        problems.append(f"{sig.key}: 声明了 source_local_date 但 payload 里没有 local_date")
        return _parse(attribution.attribute_instant(iso)), (), problems

    if strategy in ("episode_end", "split_at_midnight"):
        start, end = value.get("start_at"), value.get("end_at")
        if not isinstance(start, str) or not isinstance(end, str):
            problems.append(
                f"{sig.key}: {strategy} 需要 start_at / end_at，"
                f"退回按 occurred_at 归属"
            )
            return _parse(attribution.attribute_instant(iso)), (), problems
        if strategy == "episode_end":
            return _parse(attribution.attribute_episode(start, end)), (), problems
        slices = tuple(attribution.split_across_midnight(start, end, tz=timezone_name))
        # 归属日取结束那天；跨午夜的分摊由聚合层按 slices 处理。
        return _parse(slices[-1][0]) if slices else _parse(
            attribution.attribute_episode(start, end)
        ), slices, problems

    return _parse(attribution.attribute_instant(iso)), (), problems


# ---------------------------------------------------------------------------
# 去重身份
# ---------------------------------------------------------------------------

def identity_for(
    obs: Observation, sig: SignalDefinition, ctx: IngestContext, *,
    source: str, content_digest: str,
) -> tuple[str, str, list[str]]:
    """算两种身份，返回 ``(投递身份, 事实身份, 问题清单)``。

    见 :class:`NormalizedObservation` 的文档 —— 这两个混用会造成两类静默错误。
    """
    problems: list[str] = []
    strategy = sig.identity_strategy

    if strategy == "source_event_id" and not obs.source_event_id:
        # 声明了要用上游 id 却没给。**退回确定性摘要而不是拒收** ——
        # 拒收会让一整类信号（音乐、照片，见 FACTS.md）一条都进不来。
        problems.append(
            f"{sig.key}: 声明了 source_event_id 但没给，退回确定性摘要"
            f"（去重强度下降：挡不住「同一事实换个时间戳重发」）"
        )
        strategy = "deterministic_digest"

    if strategy == "source_event_id":
        fact = _digest(ctx.subject_id, source, sig.key, obs.source_event_id or "")
    elif strategy == "singleton":
        # 每个 subject+signal 只有一条事实，后来的是它的新版本。
        fact = _digest(ctx.subject_id, source, sig.key)
    else:
        # 没有上游身份：同一时刻同一内容才算同一条事实。
        fact = _digest(ctx.subject_id, source, sig.key, to_iso(obs.occurred_at))

    # 投递身份 = 事实 + 这一版的时刻、版本、内容。
    # 少了任何一项，都会有一类"新数据"被误判成重传而静默丢掉。
    delivery = _digest(
        fact, to_iso(obs.occurred_at),
        "" if obs.source_revision is None else str(obs.source_revision),
        content_digest,
    )
    return delivery, fact, problems


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizeResult:
    normalized: tuple[NormalizedObservation, ...]
    #: 被拒的观测：``(在这批里的下标, 问题清单)``。其余照常处理。
    rejected: tuple[tuple[int, tuple[str, ...]], ...]
    #: 处理了但有话要说的（退回了备用策略之类）。不影响落库。
    warnings: tuple[str, ...]


def normalize_observations(
    observations: tuple[Observation, ...],
    *,
    context: IngestContext,
    signals: Mapping[str, SignalDefinition],
    source: str,
    timezone_fallback: str | None = None,
    observation_id_for: Any = None,
) -> NormalizeResult:
    """把一批 wire 观测标准化成可落库的形态。

    ``observation_id_for`` 是宿主提供的 id 生成器（kit 不读时钟也不生成随机数
    —— 那会让重放和测试都做不了）。不给时用去重身份当 id。
    """
    out: list[NormalizedObservation] = []
    rejected: list[tuple[int, tuple[str, ...]]] = []
    warnings: list[str] = []

    for index, obs in enumerate(observations):
        problems: list[str] = []

        sig = signals.get(obs.signal)
        if sig is None:
            rejected.append((index, (f"{obs.signal}: manifest 里没有这个信号",)))
            continue
        if not context.allows(obs.signal):
            # 用户关掉了这项权限，设备却还在发。挡在这里，不能等写库才发现。
            rejected.append((index, (f"{obs.signal}: 这个连接没有被授权写它",)))
            continue
        if obs.signal_schema_version != sig.schema_version:
            warnings.append(
                f"{obs.signal}: payload 版本 {obs.signal_schema_version} != "
                f"manifest 版本 {sig.schema_version}"
            )

        problems += validate_value(sig, obs.value)
        if problems:
            rejected.append((index, tuple(problems)))
            continue

        # 落库边界过滤：受限字段和未声明字段到此为止，不进 canonical value。
        clean, dropped = sanitize_value(sig, obs.value)
        if dropped:
            warnings.append(f"{obs.signal}: 丢弃了 {', '.join(dropped)}")

        tz_name, _ = resolve_timezone(obs, fallback=timezone_fallback)
        if tz_name is None:
            warnings.append(
                f"{obs.signal}: 没有时区，按 occurred_at 的偏移归属日期"
                f"（夏令时切换当天可能算错）"
            )
        day, slices, day_problems = effective_date(obs, sig, timezone_name=tz_name)
        warnings += day_problems

        content = _digest(_canonical(clean), obs.availability)
        identity, fact_key, id_problems = identity_for(
            obs, sig, context, source=source, content_digest=content,
        )
        warnings += id_problems
        obs_id = (observation_id_for(obs) if callable(observation_id_for) else identity)

        out.append(NormalizedObservation(
            stored=StoredObservation(
                observation_id=obs_id,
                subject_id=context.subject_id,
                signal=obs.signal,
                signal_schema_version=obs.signal_schema_version,
                source=source,
                occurred_at=obs.occurred_at,
                received_at=context.received_at,
                availability=obs.availability,
                effective_local_date=day,
                typed_value=clean,
                timezone=tz_name,
                source_event_id=obs.source_event_id,
                source_revision=obs.source_revision,
            ),
            identity_digest=identity,
            fact_key=fact_key,
            content_digest=content,
            day_slices=slices,
        ))

    return NormalizeResult(tuple(out), tuple(rejected), tuple(warnings))


__all__ = [
    "NormalizedObservation", "NormalizeResult", "normalize_observations",
    "validate_value", "sanitize_value", "resolve_timezone", "effective_date",
    "identity_for",
]
