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
            # 每天的步数围绕一个"平时水平"上下浮动,偏离才是信号 ——
            # 不是单调漂移(那是体重),也不是看间隔(那是经期)。
            trend_model="fluctuating",
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


# ---------------------------------------------------------------------------
# 以下是 §5.1「时间、设备与短期环境」逐条对完之后加进来的
# ---------------------------------------------------------------------------

TIME_CONTEXT = SignalDefinition(
    key="time_context",
    label="时间语境",
    schema_version=1,
    capability="time",
    storage_mode="current_timeline_aggregate",
    # 产品规范给的是 300s。时区一年可能才变两次 —— 5 分钟就过期，等于用户
    # 不在前台时我们几乎永远不知道他在哪个时区，而「这条数据算哪一天」恰恰靠它。
    # 改成不失效，由查询层返回「这个信息多久之前的」。（hx 2026-08-28）
    current_ttl_sec=0.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="instant",
    history_retention_days=PERMANENT,
    note=(
        "只记录时区【变化】，本地时间不存历史 —— 有时区 + UTC 时刻就能算出来。"
        "TTL 改成不失效，原因见 current_ttl_sec 上面。"
    ),
    fields=(
        FieldDefinition(
            key="time_zone_id",
            value_type="string",
            privacy_class="personal",
            nullable=False,
            # 必须是 IANA 名字，不能只有偏移：纽约冬天 -05:00、夏天 -04:00
            # 是同一个时区，光看偏移分不出来，夏令时切换那天就会算错。
            comparison_strategy="exact",
            aggregation_strategy="event_list",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            key="utc_offset_seconds",
            value_type="integer",
            unit="seconds",
            privacy_class="personal",
            comparison_strategy="none",
            query_visibility="always",
        ),
        FieldDefinition(
            key="locale",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="none",
            query_visibility="always",
        ),
    ),
)


BROADCAST = SignalDefinition(
    key="broadcast",
    label="屏幕采集会话",
    schema_version=1,
    capability="broadcast",
    storage_mode="current_short_timeline",
    current_ttl_sec=300.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="split_at_midnight",
    history_retention_days=7,
    note=(
        "和 screen_change 是两件事：这个表示【采集会话开着没有】（一天开关几次），"
        "screen_change 表示【画面变了没有】（采集期间每几秒可能一次）。"
    ),
    fields=(
        FieldDefinition(
            key="is_active",
            value_type="boolean",
            privacy_class="personal",
            nullable=False,
            comparison_strategy="state_change",
            aggregation_strategy="duration_by_state",
            wake_eligible=True,
            query_visibility="always",
        ),
    ),
)


SCREEN_CHANGE = SignalDefinition(
    key="screen_change",
    label="画面是否发生变化",
    schema_version=1,
    capability="broadcast",
    storage_mode="current_only",
    current_ttl_sec=60.0,
    identity_strategy="singleton",
    attribution_strategy="instant",
    history_retention_days=0,
    note=(
        "🔴 只存「变了 / 没变」这个布尔。**画面指纹不进这个信号，也不落库** —— "
        "指纹序列存久了，理论上能反推用户屏幕上出现过什么。"
        "比较放在设备端做（设备本地记着上一次的指纹，只上报布尔），"
        "iOS 其他信号的 changed 标志已经是这套机制，screen 这条跟上即可。"
    ),
    fields=(
        FieldDefinition(
            key="changed",
            value_type="boolean",
            privacy_class="personal",
            nullable=False,
            comparison_strategy="occurrence",
            wake_eligible=True,
            query_visibility="always",
        ),
    ),
)


AUDIO_ROUTE = SignalDefinition(
    key="audio_route",
    label="声音从哪个设备出",
    schema_version=1,
    capability="audio_route",
    storage_mode="current_short_timeline",
    current_ttl_sec=600.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="split_at_midnight",
    history_retention_days=7,
    note=(
        "当场景线索用：连车机大概率在开车、戴 AirPods 可能在通勤或想专注。"
        "它也是蓝牙锚点唯一能拿到的残片 —— iOS 不给第三方 app 看系统级蓝牙连接，"
        "只有音频输出设备这一个子集。"
    ),
    fields=(
        FieldDefinition(
            key="output_type",
            value_type="enum",
            privacy_class="personal",
            nullable=False,
            enum=("builtin", "headphones", "car_audio",
                  "bluetooth_a2dp", "bluetooth_hfp", "bluetooth_le", "other"),
            comparison_strategy="state_change",
            aggregation_strategy="duration_by_state",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            key="is_bluetooth",
            value_type="boolean",
            privacy_class="personal",
            comparison_strategy="state_change",
            query_visibility="always",
        ),
        FieldDefinition(
            key="device_label",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
    ),
)


WEATHER = SignalDefinition(
    key="weather",
    label="天气",
    schema_version=1,
    capability="weather",
    storage_mode="current_short_timeline",
    current_ttl_sec=1800.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="instant",
    history_retention_days=7,
    note=(
        "天气是【预报】不是测量 —— valid_at 说明这份数据什么时候有效，"
        "和「我们什么时候收到的」是两件事。"
    ),
    fields=(
        FieldDefinition(
            key="condition",
            value_type="string",
            privacy_class="public",
            comparison_strategy="exact",
            query_visibility="always",
        ),
        FieldDefinition(
            key="temperature_c",
            value_type="number",
            unit="celsius",
            privacy_class="public",
            valid_range=(-90.0, 60.0),
            comparison_strategy="numeric_delta",
            aggregation_strategy="numeric_dist",
            trend_model="fluctuating",
            query_visibility="always",
        ),
        FieldDefinition(
            key="apparent_temperature_c",
            value_type="number", unit="celsius", privacy_class="public",
            valid_range=(-90.0, 70.0), query_visibility="always",
        ),
        FieldDefinition(
            key="humidity_ratio",
            value_type="number", unit="ratio", privacy_class="public",
            valid_range=(0.0, 1.0), query_visibility="always",
        ),
        FieldDefinition(
            key="precipitation_probability",
            value_type="number", unit="ratio", privacy_class="public",
            valid_range=(0.0, 1.0), query_visibility="always",
        ),
        FieldDefinition(
            key="uv_index",
            value_type="number", unit="index", privacy_class="public",
            valid_range=(0.0, 20.0), query_visibility="always",
        ),
        FieldDefinition(
            key="is_daylight",
            value_type="boolean", privacy_class="public",
            comparison_strategy="state_change", query_visibility="always",
        ),
        FieldDefinition(
            key="alerts",
            value_type="array", privacy_class="public",
            comparison_strategy="exact", wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            # 这份预报什么时候有效 —— 和"我们什么时候收到的"不是一回事。
            key="valid_at",
            value_type="timestamp", privacy_class="public",
            query_visibility="always",
        ),
        FieldDefinition(
            key="location_scope",
            value_type="string", privacy_class="personal",
            query_visibility="always",
        ),
    ),
)


# ---------------------------------------------------------------------------
# §5.3「行为、应用与媒体」逐条对完之后加进来的
# ---------------------------------------------------------------------------

MOTION_STATE = SignalDefinition(
    key="motion_state",
    label="活动状态",
    schema_version=1,
    capability="motion",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=900.0,
    identity_strategy="deterministic_digest",
    attribution_strategy="split_at_midnight",
    # 产品规范给的是"永久"。改成明细 1 年 + 聚合永久 —— 明细是聚合的 60 倍体量,
    # 但能答的问题正好反过来:明细答「上周三下午」时间越久越没人问,
    # 聚合答「今年比去年」时间越久越值钱。（hx 2026-08-28）
    history_retention_days=365,
    note=(
        "保留期偏离规范：明细 1 年（规范给「永久」），聚合仍然永久。"
        "TTL 也偏离（规范 300s → 900s）：后台保活上报间隔正好是 300s，"
        "TTL 等于上报间隔意味着用户不在前台时这个值几乎永远是 stale。"
        "另外：同一状态重复上报只刷新当前值、不写明细 —— 否则用户开着专注模式"
        "工作四小时会在历史里留下 48 条一模一样的记录。"
    ),
    fields=(
        FieldDefinition(
            key="state",
            value_type="enum",
            privacy_class="personal",
            nullable=False,
            enum=("stationary", "walking", "running", "cycling",
                  "automotive", "unknown"),
            comparison_strategy="state_change",
            aggregation_strategy="duration_by_state",
            wake_eligible=False,
            query_visibility="always",
        ),
        FieldDefinition(
            # Core Motion 本来就给置信度。低置信度的「可能在跑步」不该当事实用 ——
            # 带上它，调用方才能自己决定信不信。
            key="confidence",
            value_type="number",
            unit="ratio",
            privacy_class="public",
            valid_range=(0.0, 1.0),
            query_visibility="always",
        ),
    ),
)


PHOTO_LIBRARY_ADDED = SignalDefinition(
    key="photo_library_added",
    label="相册新增照片",
    schema_version=1,
    capability="photos",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=0.0,          # 「最近一次新增」，不按普通 TTL 失效
    identity_strategy="source_event_id",
    attribution_strategy="instant",
    history_retention_days=7,
    source_profile="device_occurrence",
    note=(
        "一张照片一条 count=1，不是「今天 5 张」报一次 —— 拆成一条条才能让照片"
        "走通用管线：跨午夜天然各归各的日、某条字段有问题只拒那一条。"
        "传输上仍然可以一个信封装多条，不多发请求。\n"
        "🔴 删照片【不回减】过去某日的数量：它记的是「那天发生过什么」，"
        "不是「现在还剩几张」。\n"
        "🔴 前提未满足：iOS 现在上报的是随机 id，同一张照片两次上报算出两个不同"
        "身份 —— 去重表再完美也挡不住。要 iOS 改成用 PHAsset.localIdentifier 的"
        "端上哈希（和 wifi_anchor_id 同一套做法）。已列入 iOS 待办。\n"
        "去重指纹的保留期：规范建议永久；我们查下来当前实现【找不到超过 7 天的"
        "重放路径】，所以按「覆盖明细保留期 + 富余」取 30 天更实在。"
        "规范自己也是条件句：「若 producer 可以在超过 7 天后重放，才必须永久保留」。"
    ),
    fields=(
        FieldDefinition(
            key="count",
            value_type="integer",
            unit="count",
            privacy_class="personal",
            nullable=False,
            valid_range=(1, 1),        # 永远是 1 —— 一张照片一条
            aggregation_strategy="daily_total",
            comparison_strategy="occurrence",
            trend_model="fluctuating",
            wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="added_at",
            value_type="timestamp",
            privacy_class="personal",
            nullable=False,
            query_visibility="on_demand",
        ),
    ),
)


#: manifest 的全部内容。逐条过完 Seven 的文档后往里加。
MINIMAL_SIGNALS: dict[str, SignalDefinition] = {
    s.key: s for s in (
        # 阶段二的五个代表信号
        BATTERY, PRESENCE_RECOVERY, STEPS, LOCATION_CITY, FOCUS_STATE,
        # §5.1 时间、设备与短期环境
        TIME_CONTEXT, BROADCAST, SCREEN_CHANGE, AUDIO_ROUTE, WEATHER,
        # §5.3 行为、应用与媒体
        MOTION_STATE, PHOTO_LIBRARY_ADDED,
    )
}

#: 明确【不做】的信号，写下来免得以后有人当成漏项。
DECLINED_SIGNALS: dict[str, str] = {
    "network_connection": (
        "不做。产品规范标的是「建议/待确认」不是要求。理由：我们能收到的上报，"
        "必然是「有网」那一刻发出的 —— 「没网」那段永远传不到服务端，"
        "这个信号自证不了自己。（hx 2026-08-28）"
    ),
}


__all__ = [
    "BATTERY", "PRESENCE_RECOVERY", "STEPS", "LOCATION_CITY", "FOCUS_STATE",
    "TIME_CONTEXT", "BROADCAST", "SCREEN_CHANGE", "AUDIO_ROUTE", "WEATHER",
    "MOTION_STATE", "PHOTO_LIBRARY_ADDED",
    "MINIMAL_SIGNALS", "DECLINED_SIGNALS",
]
