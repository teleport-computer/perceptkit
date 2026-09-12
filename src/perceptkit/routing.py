"""上报字段走到哪儿去 —— 三套词表之间唯一的那张路由图。

这个库里同一个指标有三个名字、归三个表管：

    上报契约   catalog.SIGNALS          键是 iOS 的上报键，客户端一次报一整包
    存储声明   manifest.MINIMAL_SIGNALS 键是拆完的单指标信号，字段名是归一之后的
    能力门禁   catalog.CAPABILITIES     分「报告闸（管整包能不能报）」和
                                        「查询档（管单个指标能不能查）」

**这三者之间的对应关系此前没有任何一处写下来。** 它以三份互相不知道的
手抄表存在：示例适配器里一份拆包表、宿主自己的接线里一份、能力表里靠
「信号 key 恰好等于能力 key」的默契连着第三份。改一处另外两处不会红。

2026-09-06 就是这么塌的：把健康能力拆成报告闸和查询档，拆了存储侧、
没拆上报侧，宿主照老规矩「查这个信号的能力、看它能不能被查询」拿到的是
报告闸 —— 于是心率、血氧、体重、血糖**一个都到不了 agent**。数据照收
照存、接口照样 200、测试全绿，六天没人发现。

所以这里只写**连不起来的那几条**（改过名的三个字段），其余全部按同名推导。
手写的越少，能漂的越少。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .catalog import CAPABILITIES, SIGNALS, Capability, Signal

#: 上报字段名 ≠ 存储字段名的几个 —— 归一的时候改了名。
#:
#: 体脂：上报是百分数（37.5），落库是比值（0.375），所以叫 ratio 不叫 pct。
#: 血压：落库带单位后缀，免得后人看见一个裸数字去猜是 mmHg 还是 kPa。
#:
#: ⚠️ 这三条是**唯一**需要手写的东西。加字段时别往这里加 —— 同名的自动连上，
#: 只有真改了名才写进来。
FIELD_RENAMES: Mapping[str, str] = {
    "body_fat_pct": "body_fat_ratio",
    "blood_pressure_systolic": "blood_pressure_systolic_mmhg",
    "blood_pressure_diastolic": "blood_pressure_diastolic_mmhg",
}

#: 只写进存储、从不给 agent 看的字段。它们没有查询档，也**不该**有。
#:
#: counter_epoch_id 是累计型计数器的「这是第几轮」标记，用来区分
#: 「来源把当天数字改小了」和「计数器清零重来了」。对 agent 毫无意义，
#: 说出来只会让它以为这是个可以聊的指标。
INTERNAL_ONLY_FIELDS: frozenset[str] = frozenset({"counter_epoch_id"})


@dataclass(frozen=True)
class FieldRoute:
    """一个上报字段的完整去向。"""

    report_key: str           # iOS 上报键（整包）
    report_field: str         # 上报字段名
    report_capability: str    # 报告闸：这一包能不能报
    #: 落到哪个信号（manifest 的 key）。**None = manifest 没声明它的去向**。
    #:
    #: 别拿 None 当"落在同名信号里"。上报名和存储名是两套词表，电量上报
    #: 叫 battery_level、存储叫 level_ratio；靠同名去猜只会编出一个不存在的
    #: 落点，而这正是这张路由图要消灭的东西。目前只有健康那几包把这条关系
    #: 显式写全了，其余信号的存储侧对应关系还没建模 —— 那就如实说没有。
    storage_signal: str | None
    storage_field: str | None  # 归一之后的字段名；storage_signal 为 None 时同样是 None
    query_capability: str     # 查询档：这个指标能不能被查
    ttl_sec: float            # 上报侧的新鲜度（老路 perception_state 用的那个）

    @property
    def is_split_off(self) -> bool:
        """这个字段是不是被拆到了整包之外的独立信号里。"""
        return self.storage_signal is not None and self.storage_signal != self.report_key


def _storage_signal_for(
    field: str,
    report_key: str,
    manifest: Mapping[str, object],
    split_sources: Mapping[tuple[str, str], str],
) -> str | None:
    """这个（归一后的）字段落到哪个信号里。

    先查拆分登记表，再看整包信号自己声明了没有。**不按同名去猜** ——
    active_energy_kcal 同时属于 health_activity（全天活动能量）和
    health_workout（单次运动消耗），同名不同义，猜一次就把全天活动量
    路由到运动的查询档上去了（第一版真这么错过，是守卫抓出来的）。
    """
    split = split_sources.get((report_key, field))
    if split is not None:
        return split
    own = manifest.get(report_key)
    if own is not None and any(f.key == field for f in own.fields):
        return report_key
    return None


def pack_gates(routes: Mapping[str, FieldRoute] | None = None) -> set[str]:
    """哪些报告闸底下挂着**不止一个**查询档 —— 也就是"一次报一整包"的那些。

    这是「报告闸 / 查询档」区分的可计算定义，不靠名字约定：一个闸底下的
    字段如果各归各的查询档，它就是个包闸，它自己不该再标 query_tool。
    """
    routes = ROUTES if routes is None else routes
    by_gate: dict[str, set[str]] = {}
    for r in routes.values():
        by_gate.setdefault(r.report_capability, set()).add(r.query_capability)
    return {gate for gate, caps in by_gate.items() if len(caps) > 1}


def build_routes(
    *,
    signals: Mapping[str, Signal] | None = None,
    capabilities: Mapping[str, Capability] | None = None,
    manifest: Mapping[str, object] | None = None,
    split_sources: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, FieldRoute]:
    """算出每个上报字段的去向，键是上报字段名。

    三张表都可以换成宿主自己的 —— 这是个可插拔库，默认目录只是默认值。
    """
    signals = SIGNALS if signals is None else signals
    capabilities = CAPABILITIES if capabilities is None else capabilities
    if manifest is None:
        from .manifest.minimal import MINIMAL_SIGNALS

        manifest = MINIMAL_SIGNALS
    if split_sources is None:
        from .manifest.minimal import SPLIT_SOURCES

        split_sources = SPLIT_SOURCES

    routes: dict[str, FieldRoute] = {}
    for sig in signals.values():
        for name in sig.outputs:
            if name == "user_state":
                continue
            storage_field = FIELD_RENAMES.get(name, name)
            storage_signal = _storage_signal_for(
                storage_field, sig.input, manifest, split_sources
            )
            if storage_signal is None:
                # manifest 里没有这个字段 —— 它只走老路的扁平快照，
                # 查询档就是上报闸本身（天气、日程、电量这些都是这样）。
                storage_field = None
                query_capability = sig.capability
            else:
                owner = manifest.get(storage_signal)
                query_capability = getattr(owner, "capability", sig.capability)
            if query_capability not in capabilities:
                raise ValueError(
                    f"{sig.input}.{name} 的查询档 {query_capability!r} 没有声明"
                )
            routes[name] = FieldRoute(
                report_key=sig.input,
                report_field=name,
                report_capability=sig.capability,
                storage_signal=storage_signal,
                storage_field=storage_field,
                query_capability=query_capability,
                ttl_sec=sig.ttl_sec,
            )
    return routes


#: 默认路由图（按默认目录 + 最小 manifest 算）。
ROUTES: dict[str, FieldRoute] = build_routes()


def snapshot_fields(
    *,
    include_query_tools: bool,
    routes: Mapping[str, FieldRoute] | None = None,
    capabilities: Mapping[str, Capability] | None = None,
) -> dict[str, float]:
    """这次快照该出现哪些上报字段，各自的过期秒数是多少。

    ``include_query_tools=False`` 是便宜的唤醒快照，只出常驻上下文字段；
    ``True`` 额外带上 agent 可按需查询的字段。

    TTL 用的是**上报侧**的新鲜度（老路 perception_state 那一套），不是
    manifest 的 ``current_ttl_sec``。两者回答的不是同一个问题：前者是
    「这一包多久没上报就当它过期」，后者是「这个值多久之后不能再叫当前值」。
    要后者就去读 manifest，别拿这个函数的返回值当它用。
    """
    routes = ROUTES if routes is None else routes
    capabilities = CAPABILITIES if capabilities is None else capabilities

    wanted: dict[str, float] = {}
    for route in routes.values():
        cap = capabilities.get(route.query_capability)
        if not cap:
            continue
        if not (cap.context_field or (include_query_tools and cap.query_tool)):
            continue
        prev = wanted.get(route.report_field)
        wanted[route.report_field] = (
            route.ttl_sec if prev is None else min(prev, route.ttl_sec)
        )
    return wanted


def query_capability_for(field: str, *, routes: Mapping[str, FieldRoute] | None = None) -> str | None:
    """门禁**查询**这个上报字段的能力 key。未知字段返回 None。"""
    routes = ROUTES if routes is None else routes
    route = routes.get(field)
    return route.query_capability if route else None


__all__ = [
    "FIELD_RENAMES",
    "pack_gates",
    "INTERNAL_ONLY_FIELDS",
    "FieldRoute",
    "ROUTES",
    "build_routes",
    "query_capability_for",
    "snapshot_fields",
]
