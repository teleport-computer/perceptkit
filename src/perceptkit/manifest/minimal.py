"""最小可执行 manifest —— 五个信号，把整条管线走通一遍。

**为什么先做五个而不是二十个。** 管线的正确性（幂等、乱序、TTL、聚合重算、
规则求值、投递可靠性）和信号数量无关；而字段细节还有一堆没和产品方对齐的问题
（见 ``OPEN-QUESTIONS.md``）。先用五个把管线跑通，剩下十五个只是往表里填格子。

选这五个是因为它们**恰好覆盖四种存储形态和三种身份策略**：

    battery            current_only          · singleton
    presence_recovery  current_only          · source_event_id  · occurrence 事件
    steps              timeline + aggregate  · source_event_id  · threshold 事件
    location_city      timeline + aggregate  · deterministic    · 变化才追加
    focus_state        timeline + aggregate  · deterministic    · 时长聚合

**和产品规范的三处有意出入**，都在各自的 ``note`` 里写明了原因，
并且都进了 ``OPEN-QUESTIONS.md`` 等确认 —— 不是默默改掉。
"""
from __future__ import annotations

from .types import PERMANENT, FieldDefinition, SignalDefinition

# ---------------------------------------------------------------------------
# battery —— 最简单的一种：只留当前值
# ---------------------------------------------------------------------------

BATTERY = SignalDefinition(
    key="battery",
    label="电量",
    schema_version=1,
    capability="device",
    storage_mode="current_only",
    current_ttl_sec=600.0,
    identity_strategy="singleton",
    attribution_strategy="instant",
    history_retention_days=0,
    fields=(
        FieldDefinition(
            key="level_ratio",
            value_type="number",
            unit="ratio",
            privacy_class="public",
            nullable=False,
            valid_range=(0.0, 1.0),
            comparison_strategy="threshold_crossing",
            wake_eligible=False,
            query_visibility="always",
        ),
        FieldDefinition(
            key="is_charging",
            value_type="boolean",
            privacy_class="public",
            nullable=False,
            comparison_strategy="state_change",
            query_visibility="always",
        ),
        FieldDefinition(
            key="is_low_power_mode_enabled",
            value_type="boolean",
            privacy_class="public",
            comparison_strategy="state_change",
            query_visibility="always",
        ),
    ),
)


# ---------------------------------------------------------------------------
# presence_recovery —— occurrence 型事件
# ---------------------------------------------------------------------------

PRESENCE_RECOVERY = SignalDefinition(
    key="presence_recovery",
    label="久别之后重新在场",
    schema_version=1,
    capability="device",
    storage_mode="current_only",
    # 不按普通 TTL 失效：查询时返回"多久之前"，由调用方判断还算不算新鲜。
    current_ttl_sec=0.0,
    identity_strategy="source_event_id",
    attribution_strategy="instant",
    history_retention_days=0,
    source_profile="device_occurrence",
    note=(
        "产品规范叫 device_unlock，这里改名 presence_recovery —— iOS 拿不到硬件"
        "解锁事件（precise_unlock 恒为 null），能给的只是「app 自己进后台到回前台"
        "的间隔」。沿用 unlock 这个名字会让模型解释成「用户刚解锁手机」，和产品方"
        "自己撤回 device_boot 是同一类错误。见 OPEN-QUESTIONS B10。"
    ),
    fields=(
        FieldDefinition(
            key="recovered_at",
            value_type="timestamp",
            privacy_class="personal",
            nullable=False,
            # occurrence:这条观测到达本身就是事件,没有前后值可比,
            # 靠 source_event_id 去重。
            comparison_strategy="occurrence",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            key="absence_seconds",
            value_type="number",
            unit="seconds",
            privacy_class="personal",
            valid_range=(0.0, None),
            comparison_strategy="threshold_crossing",
            query_visibility="always",
        ),
        FieldDefinition(
            # 这个值是估的，不是测的。字段本身带上这个事实，比写在文档里可靠 ——
            # 读到值的人不一定读过文档。
            key="absence_quality",
            value_type="enum",
            privacy_class="public",
            enum=("measured", "estimated"),
            nullable=False,
            query_visibility="always",
        ),
    ),
)


# ---------------------------------------------------------------------------
# steps —— 累计量 + 阈值事件
# ---------------------------------------------------------------------------

STEPS = SignalDefinition(
    key="steps",
    label="步数",
    schema_version=1,
    capability="health_vitals",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=3600.0,
    identity_strategy="source_event_id",
    attribution_strategy="source_local_date",
    history_retention_days=PERMANENT,
    source_profile="health_sample",
    fields=(
        FieldDefinition(
            key="step_count",
            value_type="integer",
            unit="count",
            privacy_class="sensitive",
            nullable=False,
            valid_range=(0, None),
            aggregation_strategy="daily_total",
            # 必须是"跨过"而不是"大于等于" —— 后者会让 3001、3010、3100
            # 每次上报都重复触发。
            comparison_strategy="threshold_crossing",
            wake_eligible=True,
            query_visibility="on_demand",
        ),
    ),
)


# ---------------------------------------------------------------------------
# location_city —— 城市级位置
# ---------------------------------------------------------------------------

LOCATION_CITY = SignalDefinition(
    key="location_city",
    label="所在城市",
    schema_version=1,
    capability="location",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=900.0,
    # iOS 不给稳定的位置事件 id，用 (signal, occurred_at, 值摘要) 造确定性键。
    identity_strategy="deterministic_digest",
    attribution_strategy="instant",
    history_retention_days=PERMANENT,
    source_profile="location",
    note=(
        "只有城市级。精细位置（home / work / 某个房间）是另一个信号"
        "（proximity_anchor），不能混进同一个字段 —— 两个时期都叫 home "
        "就看不出搬过家。"
    ),
    fields=(
        FieldDefinition(
            key="locality",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="exact",
            aggregation_strategy="duration_by_state",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            key="country_code",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="exact",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            # 声明出来，是为了让"它永远不该被持久化、永远不该给 agent"
            # 成为一条可被测试检查的规则，而不是一句口头约定。
            key="coordinate",
            value_type="object",
            privacy_class="restricted",
            query_visibility="never",
            # 不挂 normalizer：把坐标粗化成城市要查地理编码，那是 I/O，
            # kit 不做。实际链路里 iOS 在端上就解析完并丢弃了坐标，
            # 这个字段是留给其他 producer 的（协议要通用），
            # 声明它是为了让"永不持久化、永不给 agent"成为可测试的规则。
        ),
    ),
)


# ---------------------------------------------------------------------------
# focus_state —— 状态时长聚合
# ---------------------------------------------------------------------------

FOCUS_STATE = SignalDefinition(
    key="focus_state",
    label="专注模式",
    schema_version=1,
    capability="focus",
    storage_mode="current_timeline_aggregate",
    # 产品规范给的是 300s。实测 iOS 后台保活上报间隔正好也是 300s、
    # 进程被挂起后更长 —— TTL 等于上报间隔，意味着用户只要不在前台，
    # 这个值几乎永远是 stale。取 3 倍。见 OPEN-QUESTIONS B12。
    current_ttl_sec=900.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="split_at_midnight",
    history_retention_days=PERMANENT,
    note=(
        "替掉产品规范阶段二里的 proximity_anchor：那个信号的 bluetooth 类型 iOS "
        "给不了（只有音频路由这个子集），enter/leave 边缘也没有。focus_state "
        "覆盖同一种存储形态，且采集能力确凿。见 OPEN-QUESTIONS B14/B17。"
    ),
    fields=(
        FieldDefinition(
            key="is_active",
            value_type="boolean",
            privacy_class="personal",
            nullable=False,
            aggregation_strategy="duration_by_state",
            comparison_strategy="state_change",
            wake_eligible=False,
            query_visibility="always",
        ),
    ),
)


#: 最小 manifest 的全部内容。批 8 往里加剩下的信号。
MINIMAL_SIGNALS: dict[str, SignalDefinition] = {
    s.key: s for s in (BATTERY, PRESENCE_RECOVERY, STEPS, LOCATION_CITY, FOCUS_STATE)
}


__all__ = [
    "BATTERY", "PRESENCE_RECOVERY", "STEPS", "LOCATION_CITY", "FOCUS_STATE",
    "MINIMAL_SIGNALS",
]
