"""Manifest 的自动检查 —— 让"忘了声明"变成一条红色的测试。

以前一个字段的属性散在七个地方，**漏掉其中一处不会有任何东西变红**：
新加的健康字段忘了声明 retention，历史就永远不会被清理；忘了声明 comparator，
它的变化就永远触发不了事件；resolver 名字拼错，就是一个永远不会被发现的空指针
——现状里就有 10 个这样的名字，声明了但没有任何实现。

这里的四条检查对应产品规范 §8 的四条要求。它们不判风格、不判命名，
只判**结构上有没有缺口**，误伤为零。

用法（在宿主的测试里）::

    problems = validate_manifest(MY_SIGNALS, available_normalizers=MY_NORMALIZERS)
    assert not problems, "\\n".join(problems)
"""
from __future__ import annotations

from typing import Iterable, Mapping

from .types import (
    AGGREGATION_STRATEGIES,
    TREND_MODELS,
    ATTRIBUTION_STRATEGIES,
    COMPARISON_STRATEGIES,
    IDENTITY_STRATEGIES,
    PERMANENT,
    PRIVACY_CLASSES,
    QUERY_VISIBILITY,
    SOURCE_PROFILES,
    STORAGE_MODES,
    VALUE_TYPES,
    SignalDefinition,
)

#: 需要单位的类型。布尔、枚举、字符串、对象没有量纲。
_NUMERIC_TYPES = frozenset({"integer", "number"})

#: 会产生日聚合的存储形态。
_AGGREGATING_MODES = frozenset({"current_timeline_aggregate", "current_short_timeline"})


def check_types_and_units(signals: Mapping[str, SignalDefinition]) -> list[str]:
    """① 每个字段都要有合法类型；数值型必须有单位。

    没有单位的数字在跨宿主传递时**必然**被解释错 —— 体重是公斤还是磅、
    时长是秒还是分钟，接收方只能猜，而猜错不会报错。
    """
    problems: list[str] = []
    for key, sig in signals.items():
        if not sig.fields:
            problems.append(f"{key}: 一个字段都没声明")
        for f in sig.fields:
            if f.value_type not in VALUE_TYPES:
                problems.append(
                    f"{key}.{f.key}: value_type={f.value_type!r} 不在 {sorted(VALUE_TYPES)}"
                )
            if f.value_type in _NUMERIC_TYPES and not f.unit:
                problems.append(
                    f"{key}.{f.key}: 数值字段必须声明 unit"
                    f"（没有单位的数字跨宿主必然被解释错）"
                )
            if f.value_type == "enum" and not f.enum:
                problems.append(f"{key}.{f.key}: enum 类型必须列出合法取值")
            if f.privacy_class not in PRIVACY_CLASSES:
                problems.append(
                    f"{key}.{f.key}: privacy_class={f.privacy_class!r} "
                    f"不在 {sorted(PRIVACY_CLASSES)}"
                )
            if f.query_visibility not in QUERY_VISIBILITY:
                problems.append(
                    f"{key}.{f.key}: query_visibility={f.query_visibility!r} "
                    f"不在 {sorted(QUERY_VISIBILITY)}"
                )
    return problems


def check_history_has_retention(signals: Mapping[str, SignalDefinition]) -> list[str]:
    """② 会存历史的信号必须声明保留期，会聚合的字段必须声明聚合方式。

    忘了声明 retention，历史就无限增长且永远不会被清理 —— 而且不会有任何
    症状，直到某天库满了。
    """
    problems: list[str] = []
    for key, sig in signals.items():
        if sig.storage_mode in _AGGREGATING_MODES and not sig.stores_history:
            problems.append(
                f"{key}: storage_mode={sig.storage_mode} 会产生历史，"
                f"但 history_retention_days=0（等于声明不存历史，自相矛盾）"
            )
        if sig.storage_mode == "current_only" and sig.stores_history:
            problems.append(
                f"{key}: storage_mode=current_only 却声明了保留期 "
                f"{sig.history_retention_days}，两者只能留一个"
            )
        if sig.stores_history:
            aggregating = [f for f in sig.fields if f.aggregation_strategy != "none"]
            if not aggregating:
                problems.append(
                    f"{key}: 声明了要存历史，但没有任何字段声明 aggregation_strategy"
                    f"（那存下来的明细没有任何东西会去读它）"
                )
        if sig.history_retention_days < -1:
            problems.append(
                f"{key}: history_retention_days={sig.history_retention_days} 非法"
                f"（-1 表示永久，0 表示不存，正数表示天数）"
            )
    return problems


def check_named_implementations_exist(
    signals: Mapping[str, SignalDefinition],
    *,
    available_normalizers: Iterable[str] = (),
) -> list[str]:
    """③ manifest 里提到的每个名字都要能解析到实现。

    这一条抓的是**空指针式的声明**：写了 ``normalizer="health_vitals"``
    但没有任何地方实现它。这类错误不会抛异常，只会让那个字段静默地不被标准化。

    ``available_normalizers`` 由调用方传入 —— 有些 normalizer 天然属于宿主
    （比如把坐标粗化成城市要查地理编码，那是 I/O，kit 不做）。
    """
    known = frozenset(available_normalizers)
    problems: list[str] = []
    for key, sig in signals.items():
        if sig.storage_mode not in STORAGE_MODES:
            problems.append(
                f"{key}: storage_mode={sig.storage_mode!r} 不在 {sorted(STORAGE_MODES)}"
            )
        if sig.identity_strategy not in IDENTITY_STRATEGIES:
            problems.append(
                f"{key}: identity_strategy={sig.identity_strategy!r} "
                f"不在 {sorted(IDENTITY_STRATEGIES)}"
            )
        if sig.attribution_strategy not in ATTRIBUTION_STRATEGIES:
            problems.append(
                f"{key}: attribution_strategy={sig.attribution_strategy!r} "
                f"不在 {sorted(ATTRIBUTION_STRATEGIES)}"
            )
        agg_days = sig.aggregate_retention_days
        if agg_days is not None:
            if agg_days == 0 and sig.stores_history:
                problems.append(
                    f"{key}: 存明细却声明聚合保留 0 天。"
                    "明细进了聚合表却当天就被删，那张表永远是空的"
                )
            elif (agg_days != PERMANENT
                  and sig.history_retention_days == PERMANENT):
                problems.append(
                    f"{key}: 明细永久保存但聚合只留 {agg_days} 天。"
                    "反了 —— 聚合是压缩过的、体量小的那一半，"
                    "留明细不留聚合等于既花了存储又丢了长期趋势"
                )
            elif (agg_days != PERMANENT and sig.history_retention_days != PERMANENT
                  and agg_days < sig.history_retention_days):
                problems.append(
                    f"{key}: 聚合留 {agg_days} 天比明细的 "
                    f"{sig.history_retention_days} 天还短。"
                    "日统计会先于它依据的明细消失，历史上会出现一段"
                    "有明细却查不到统计的窗口"
                )

        if sig.source_profile is not None and sig.source_profile not in SOURCE_PROFILES:
            problems.append(
                f"{key}: source_profile={sig.source_profile!r} "
                f"不在 {sorted(SOURCE_PROFILES)}"
            )
        for f in sig.fields:
            if f.aggregation_strategy not in AGGREGATION_STRATEGIES:
                problems.append(
                    f"{key}.{f.key}: aggregation_strategy={f.aggregation_strategy!r} "
                    f"不在 {sorted(AGGREGATION_STRATEGIES)}"
                )
            if f.comparison_strategy not in COMPARISON_STRATEGIES:
                problems.append(
                    f"{key}.{f.key}: comparison_strategy={f.comparison_strategy!r} "
                    f"不在 {sorted(COMPARISON_STRATEGIES)}"
                )
            if f.trend_model not in TREND_MODELS:
                problems.append(
                    f"{key}.{f.key}: trend_model={f.trend_model!r} "
                    f"不在 {sorted(TREND_MODELS)}"
                )
            if (f.value_type in ("integer", "number")
                    and f.aggregation_strategy != "none"
                    and f.trend_model == "none"):
                problems.append(
                    f"{key}.{f.key}: 数值字段会进日聚合却没声明 trend_model"
                    f"（趋势查询只能瞎猜用哪种算法,而三种结论完全不同）"
                )
            if f.accepted_units:
                from .units import can_convert
                if not f.unit:
                    problems.append(
                        f"{key}.{f.key}: 声明了 accepted_units 却没有标准单位"
                    )
                else:
                    for u in f.accepted_units:
                        if not can_convert(u, f.unit):
                            problems.append(
                                f"{key}.{f.key}: 声明接受 {u!r} 但没有 "
                                f"{u!r} -> {f.unit!r} 的换算实现"
                            )
            if f.max_relative_jump is not None and f.max_relative_jump <= 0:
                problems.append(
                    f"{key}.{f.key}: max_relative_jump 必须为正数"
                )
            if f.normalizer is not None and f.normalizer not in known:
                problems.append(
                    f"{key}.{f.key}: normalizer={f.normalizer!r} 没有对应实现"
                    f"（声明了名字却没人实现 = 这个字段静默地不被标准化）"
                )
    return problems


def check_wake_eligible_fields_have_comparators(
    signals: Mapping[str, SignalDefinition],
) -> list[str]:
    """④ 能触发唤醒的字段必须说清楚"怎么算变了"。

    没有 comparator 的 wake_eligible 字段永远不会触发 —— 它看起来配好了，
    实际是死的。这是最典型的"上线了但功能没生效"。
    """
    problems: list[str] = []
    for key, sig in signals.items():
        for f in sig.fields:
            if f.wake_eligible and f.comparison_strategy == "none":
                problems.append(
                    f"{key}.{f.key}: wake_eligible=True 但 comparison_strategy=none"
                    f"（永远不会触发，看起来配好了实际是死的）"
                )
            if f.wake_eligible and f.query_visibility == "never":
                problems.append(
                    f"{key}.{f.key}: 既然 agent 永远看不到它，"
                    f"用它去唤醒 agent 说不通"
                )
    return problems


def validate_manifest(
    signals: Mapping[str, SignalDefinition],
    *,
    available_normalizers: Iterable[str] = (),
) -> list[str]:
    """跑全部四条检查，返回问题清单（空 = 通过）。

    返回列表而不是抛异常：一次看到全部缺口，比逐个修再重跑快得多。
    """
    problems: list[str] = []
    for key, sig in signals.items():
        if key != sig.key:
            problems.append(f"{key}: 字典的键和 SignalDefinition.key={sig.key!r} 对不上")
        seen: set[str] = set()
        for f in sig.fields:
            if f.key in seen:
                problems.append(f"{key}.{f.key}: 字段重复声明")
            seen.add(f.key)
    problems += check_types_and_units(signals)
    problems += check_history_has_retention(signals)
    problems += check_named_implementations_exist(
        signals, available_normalizers=available_normalizers
    )
    problems += check_wake_eligible_fields_have_comparators(signals)
    return problems


__all__ = [
    "check_types_and_units",
    "check_history_has_retention",
    "check_named_implementations_exist",
    "check_wake_eligible_fields_have_comparators",
    "validate_manifest",
]
