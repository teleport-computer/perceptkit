"""Manifest 的类型 —— 每个信号、每个字段的完整声明。

**为什么要有这一层。** 在这之前,一个字段的属性散在七个互不关联的地方:
catalog 声明输入和 TTL、fields 决定 agent 能不能看、history 决定怎么聚合、
retention 决定留多久、attribution 决定算哪天、trend_models 决定用哪种趋势、
prompts 决定怎么讲给模型。加一个字段要同时改七处,**漏一处就是静默出错**
—— 没有任何测试会因为"你忘了给新字段声明 retention"而变红。

manifest 把这七处并成一处,并让四条自动检查有了施力点(见 ``checks.py``)。

manifest 只声明**属性**,不实现算法。``normalizer`` / ``aggregation_strategy``
这类字段存的是名字,具体实现由 kit 或宿主注册 —— 但名字必须能解析到实现,
否则就是一个永远不会被发现的空指针(现状:catalog 里有 10 个 resolver 名字
没有任何实现)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 值域
# ---------------------------------------------------------------------------

#: 字段的类型。刻意只有这几种 —— 协议要能被非 Python 的 producer 实现。
VALUE_TYPES: frozenset[str] = frozenset({
    "integer", "number", "boolean", "string", "enum", "timestamp", "array", "object",
})

#: 隐私级别。决定这个字段能不能进日志、能不能持久化、能不能给 agent 看。
#: kit 不做加解密 —— 这只是给宿主的处理提示。
PRIVACY_CLASSES: frozenset[str] = frozenset({
    "public",      # 无所谓,如电量
    "personal",    # 用户的日常事实,如城市、专注状态
    "sensitive",   # 健康、日历内容、照片属性
    "restricted",  # 默认不持久化、不给 agent,如精确坐标、BSSID
})

#: 四种存储形态。每个信号必须明确属于其中一种,不能"看情况"。
STORAGE_MODES: frozenset[str] = frozenset({
    "current_only",              # 只留最近可信值。电量、屏幕变化
    "current_timeline_aggregate",  # 最新 + 变化明细 + 日聚合。位置、专注、健康
    "current_short_timeline",    # 最新 + 有限期明细。Wi-Fi、天气、音频路由
    "source_mirror",             # 跟随外部可变集合。日历、提醒
})

#: 日聚合的算法。名字要能解析到实现。
AGGREGATION_STRATEGIES: frozenset[str] = frozenset({
    "none", "daily_total", "occurrence_count", "numeric_dist",
    "duration_by_state", "event_list", "tally", "main_of_day", "cumulative",
})

#: 怎么判"这个字段该触发了"。事件规则和 wake 判断都读它。
#: 注意 ``occurrence`` 不是在比较 —— 它表示"这条观测到达本身就是事件"
#: （解锁、新增照片这类）,没有前后值可比,靠 source_event_id 去重。
COMPARISON_STRATEGIES: frozenset[str] = frozenset({
    "none", "exact", "numeric_delta", "threshold_crossing", "state_change",
    "occurrence",
})

#: 一条观测的去重身份怎么来。
IDENTITY_STRATEGIES: frozenset[str] = frozenset({
    # 上游给稳定 id(HealthKit sample、日历事件)。最可靠。
    "source_event_id",
    # 上游给不了 id,用 (signal, occurred_at, 值摘要) 造一个确定性的键。
    # 音乐、照片现在只能走这条 —— 见 FACTS.md ③ 和 3.1。
    "deterministic_digest",
    # 每个 subject+signal 只有一条,后来的直接覆盖。电量这类纯快照。
    "singleton",
})

#: 一条观测算哪一天。
ATTRIBUTION_STRATEGIES: frozenset[str] = frozenset({
    "instant",            # 瞬时值,按发生时刻的本地日期
    "episode_end",        # 区间,按结束时刻(睡眠算醒来那天)
    "split_at_midnight",  # 区间,跨午夜按本地日切开分摊
    "source_local_date",  # 上游直接给了本地日期,原样用
})

#: 这个字段的历史该用哪种趋势读法。三种算法结论完全不同,选错就是错的:
#:   fluctuating 有"平时水平",偏离才是信号(睡眠时长、步数)
#:   drifting    没有平时水平,方向和速率才是信号(体重)
#:   cyclical    看间隔不看数值高低(经期)
TREND_MODELS: frozenset[str] = frozenset({
    "none", "fluctuating", "drifting", "cyclical",
})

#: agent 能不能看到这个字段。
QUERY_VISIBILITY: frozenset[str] = frozenset({
    "always",     # 直接进上下文
    "on_demand",  # agent 主动查才给
    "never",      # 只在 kit 内部用,永不出给模型(如原始坐标)
})

#: 需要额外语义的来源。不是所有信号都要背 revision / cursor / tombstone。
SOURCE_PROFILES: frozenset[str] = frozenset({
    "health_sample", "calendar_sync", "location", "device_occurrence", "proximity_anchor",
})

#: 保留期用这个值表示"永久"。用 ``None`` 会和"忘了声明"混淆。
PERMANENT = -1


# ---------------------------------------------------------------------------
# 声明
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDefinition:
    """一个字段的完整声明。"""

    key: str
    value_type: str
    privacy_class: str
    #: **标准**单位。无量纲的(布尔、枚举、标签)写 ``None``,但**数值型必须有** ——
    #: 没有单位的数字在跨宿主传递时必然被解释错。
    unit: str | None = None
    #: producer 还可能发来哪些单位。收到这些一律换算成 ``unit``,
    #: 原始单位留作 metadata。不在这个名单里的单位**拒收** ——
    #: 一个磅的数字当公斤存进去,比拒收难查得多(没有报错,只有一个悄悄
    #: 错掉一半的体重)。
    accepted_units: tuple[str, ...] = ()
    #: 相邻两次测量的相对变化上限。超过就报 conflict(**不拒收**,交给宿主决定)。
    #: 这是唯一能挡住"单位标错"的一道:体重一次掉一半,不管单位对不对都不正常。
    #: 值域校验拦不住它 —— 31.8 kg 完全合法。
    #: 会误伤(用户真换了体重计),所以只报冲突。``None`` = 不检查。
    max_relative_jump: float | None = None
    nullable: bool = True
    #: 数值型的合法区间 ``(min, max)``,任一端可为 ``None``。
    valid_range: tuple[float | None, float | None] | None = None
    #: 枚举型的合法取值。
    enum: tuple[str, ...] | None = None
    aggregation_strategy: str = "none"
    comparison_strategy: str = "none"
    #: 这个字段的变化能不能触发唤醒。
    wake_eligible: bool = False
    query_visibility: str = "on_demand"
    trend_model: str = "none"
    #: 标准化函数的名字。``None`` = 原样存。
    normalizer: str | None = None
    #: 这个字段为什么长这样 —— 和规范不一致的地方、平台限制、放弃的替代方案。
    #: **写进数据结构而不是注释**：读 manifest 的人(和 dump 出来的文档)一定
    #: 看得到,注释只有读源码的人看得到。信号级有同名字段,这里补上字段级。
    note: str | None = None


@dataclass(frozen=True)
class SignalDefinition:
    """一个信号的完整声明。"""

    key: str
    label: str
    schema_version: int
    #: 权限门。用户关掉这个 capability,整个信号停止读取和唤醒。
    capability: str
    storage_mode: str
    #: 超过这个秒数就不能再叫"当前值"。仍可作为带 ``as_of`` 的 last known 返回。
    current_ttl_sec: float
    identity_strategy: str
    attribution_strategy: str
    fields: tuple[FieldDefinition, ...]
    #: 哪几个字段把这个信号的当前值**分成并列的多条**。
    #:
    #: 空（默认）= 一个信号一条当前值，新的覆盖旧的。绝大多数信号就该这样：
    #: 电量、天气、运动状态，同一时刻只有一个答案。
    #:
    #: 非空 = 每个取值组合各留一条。锚点是这么来的：同时连着家里和公司两个
    #: Wi-Fi，「当前连着哪些锚点」有两个答案，覆盖式写入只会剩最后一个。
    #: 更糟的是用户搬家、新旧网络都叫 "home" —— 按名字看是同一个，按
    #: ``anchor_id`` 看是两个，合并之后历史再也分不开哪段是哪个家。
    dimension_fields: tuple[str, ...] = ()
    #: **明细**（逐条观测）保留多少天。``PERMANENT`` = 永久;``0`` = 不存历史。
    history_retention_days: int = 0
    #: **聚合**（日统计）保留多少天。默认跟着明细走 —— 但两者常常不该一样。
    #:
    #: 明细是聚合的几十倍体量,而能回答的问题正好反过来:
    #: 「上周三下午你专注了多久」时间越久越没人问,
    #: 「你今年专注时间比去年长了吗」时间越久越值钱。
    #: 所以典型形态是**明细短、聚合永久** —— 省的全在明细上,
    #: 多花的不到 2%,长期趋势保住了。
    #:
    #: ``None`` = 跟明细一样。
    #:
    #: ⚠️ 明细过期而聚合永久时,**去重记录必须比明细活得久**,
    #: 否则旧数据重放会把永久聚合的数字加两遍且无法回滚。
    aggregate_retention_days: int | None = None
    source_profile: str | None = None
    #: 这条声明为什么长这样 —— 特别是和产品规范有出入的地方。
    #: 写进数据结构而不是注释,是为了让它跟着 manifest 一起被读到。
    note: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def field_map(self) -> dict[str, FieldDefinition]:
        return {f.key: f for f in self.fields}

    @property
    def stores_history(self) -> bool:
        return self.history_retention_days != 0

    @property
    def keeps_history_forever(self) -> bool:
        return self.history_retention_days == PERMANENT

    def dimension_key_for(self, value: Mapping[str, Any] | None) -> str:
        """这条观测落在哪一条当前值上。

        没声明 ``dimension_fields`` 就用信号名 —— 一个信号一条，和以前一样。
        声明了就按那几个字段的取值拼出来；取不到值的按空串参与，**不能退回
        信号名**：那会让「拿不到 anchor_id 的那条」去覆盖掉一条真的锚点。
        """
        if not self.dimension_fields:
            return self.key
        parts = [str((value or {}).get(f) or "") for f in self.dimension_fields]
        return self.key + "\x1f" + "\x1f".join(parts)

    @property
    def effective_aggregate_retention_days(self) -> int:
        """聚合实际留多久。没单独声明就跟明细一样。"""
        return (self.history_retention_days if self.aggregate_retention_days is None
                else self.aggregate_retention_days)

    @property
    def keeps_aggregates_forever(self) -> bool:
        return self.effective_aggregate_retention_days == PERMANENT


__all__ = [
    "VALUE_TYPES", "PRIVACY_CLASSES", "STORAGE_MODES",
    "AGGREGATION_STRATEGIES", "COMPARISON_STRATEGIES",
    "IDENTITY_STRATEGIES", "ATTRIBUTION_STRATEGIES",
    "QUERY_VISIBILITY", "TREND_MODELS", "SOURCE_PROFILES", "PERMANENT",
    "FieldDefinition", "SignalDefinition",
]
