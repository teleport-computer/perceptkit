"""默认 manifest —— 32 个信号。

**它是怎么长到 23 个的。** 一开始只有五个：管线的正确性（幂等、乱序、TTL、
聚合重算、规则求值、投递可靠性）和信号数量无关，所以先用五个把管线跑通，
剩下的只是往表里填格子。管线验完之后按真机上真实出现的来源逐个补齐，
现在 23 个。改这个数字时**这一行也要跟着改** —— 一份说"五个"的文件
会让下一个接入的人（和下一个工程 AI）按五个去设计。

最早那五个之所以是那五个，是因为它们**恰好覆盖四种存储形态和三种身份策略**，
这个骨架现在仍然成立：

    battery            current_only          · singleton
    presence_recovery  current_only          · source_event_id  · occurrence 事件
    steps              timeline + aggregate  · source_event_id  · threshold 事件
    location_city      timeline + aggregate  · deterministic    · 变化才追加
    focus_state        timeline + aggregate  · deterministic    · 时长聚合

**和产品规范的三处有意出入**，都在各自的 ``note`` 里写明了原因，
并且都进了 ``OPEN-QUESTIONS.md`` 等确认 —— 不是默默改掉。
"""
from __future__ import annotations

from dataclasses import replace

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
        FieldDefinition(
            # 🔴 **凭什么说这个人回来了。**
            #
            # `presence_recovery` ≠ `device_unlock`：app 回到前台只说明 app
            # 回到了前台，手机可能一直没锁过。没有这一格的话，下游唯一能
            # 说的只有「回来了」，而实际发生的是有人把它讲成了「刚解锁手机」
            # —— 那是一句**编出来的**事实。
            #
            # 三种证据的强度依次递增，但**没有一种能证明解锁**：
            #     app_entered_foreground          从后台切回来
            #     app_became_active               拿到了焦点（可能只是关掉了通知）
            #     protected_data_became_available  数据保护解除 —— 最接近解锁，
            #                                      但设备也可能是被 Face ID 解开后
            #                                      立刻又交给了别人
            # 真正的解锁 producer 出现时，它该是一个**独立信号**
            # （`device_unlock`），而不是往这里塞第四个值。
            key="evidence",
            value_type="enum",
            privacy_class="public",
            enum=("app_entered_foreground", "app_became_active",
                  "protected_data_became_available", "unknown"),
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
        FieldDefinition(
            # 来源的计数器重置时换一个值（HealthKit 换设备、用户重装…）。
            # **重置不是修订**：把「重置到 0」当成修订，它会被"取 max"的
            # 单调假设吃掉，当天的数永远停在重置前的最大值。
            # 生产方不给时按单调计数器处理（退回取 max），所以老客户端不受影响。
            key="counter_epoch_id",
            value_type="string",
            privacy_class="public",
            aggregation_strategy="none",
            query_visibility="never",
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
            key="region",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="exact",
            # 省 / 州。**不参与唤醒** —— 换省一定伴随换城市，
            # 两个都能唤醒就是同一件事报两次。
            wake_eligible=False,
            query_visibility="always",
            note="城市和国家之间那一层。同名城市（好几个 Springfield）靠它区分。",
        ),
        FieldDefinition(
            key="accuracy_m",
            value_type="number",
            unit="m",
            privacy_class="personal",
            valid_range=(0, None),
            comparison_strategy="none",
            query_visibility="on_demand",
            note=(
                "这次定位有多准。**它不是位置本身，是位置的可信度** —— "
                "误差半径 5 公里时说「你在上海」和误差 20 米时说，是两句"
                "可信度完全不同的话，而不带这个字段就分不出来。"
            ),
        ),
        FieldDefinition(
            key="placemark_source",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="none",
            query_visibility="on_demand",
            note=(
                "城市名是怎么来的：系统反地理编码 / 缓存 / 用户手填。"
                "来源不同可信度不同，排查「为什么说我在另一个城市」时是第一条线索。"
            ),
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
    # 同上：时间点快照，时长由聚合层从相邻观测算。
    attribution_strategy="instant",
    # 明细 1 年、聚合永久（hx 2026-08-28）。产品规范给的是两者都永久，
    # 但明细是聚合的几十倍体量，而"上周三下午你专注了多久"时间越久越没人问。
    # 和 motion_state 同一条决定 —— 这两个信号必须一致。
    history_retention_days=365,
    aggregate_retention_days=PERMANENT,
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
    # 同上：时间点快照，时长由聚合层从相邻观测算。
    attribution_strategy="instant",
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
    # 这几个信号发的是**时间点快照**，没有 start_at / end_at ——
    # 时长是聚合层从相邻两条观测的时间差里算出来的，不是观测自带的。
    # 先前这里声明的是 split_at_midnight，于是管线每条都去找一个不存在的
    # 区间、警告、再退回按 occurred_at 归属，**结果和 instant 一模一样**。
    # 声明成实际发生的样子，行为逐字节不变，只是不再骗读它的人。
    # 跨午夜的时长该怎么摊，是**聚合层**的问题（见 FEATURE_LOG 的待办），
    # 不该伪装成一个归属策略。
    attribution_strategy="instant",
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
    # 同上：时间点快照，时长由聚合层从相邻观测算。
    attribution_strategy="instant",
    # 产品规范给的是"永久"。改成明细 1 年 + 聚合永久 —— 明细是聚合的 60 倍体量,
    # 但能答的问题正好反过来:明细答「上周三下午」时间越久越没人问,
    # 聚合答「今年比去年」时间越久越值钱。（hx 2026-08-28）
    history_retention_days=365,
    # 同 focus_state：明细 1 年、聚合永久。
    aggregate_retention_days=PERMANENT,
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
    # 单条明细 7 天，**每日新增数量永久**。两个数必须分开写：不写聚合那个
    # 就会继承明细的 7 天，于是「8月1日新增了 5 张」一周后被扫掉 ——
    # 而那是一件发生过的事实，不是「现在还剩几张」。
    aggregate_retention_days=PERMANENT,
    source_profile="device_occurrence",
    note=(
        "一张照片一条 count=1，不是「今天 5 张」报一次 —— 拆成一条条才能让照片"
        "走通用管线：跨午夜天然各归各的日、某条字段有问题只拒那一条。"
        "传输上仍然可以一个信封装多条，不多发请求。\n"
        "🔴 删照片【不回减】过去某日的数量：它记的是「那天发生过什么」，"
        "不是「现在还剩几张」。\n"
        "身份必须由设备给一个稳定值：内容信封 id 是每次上传新生成的，用它的话"
        "同一张照片重传就是两张，去重表再完美也挡不住。iOS 送的是"
        "SHA256(固定 namespace + PHAsset.localIdentifier)——固定 namespace 而不是"
        "wifi_anchor_id 那种设备本地随机密钥：照片 id 是本机相册的高熵 UUID，"
        "别的设备上不存在，固定 namespace 就够不可逆；而且重装后仍然稳定，"
        "正好让重扫相册时认出「这些都数过了」。\n"
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
            # 🔴 求和，不是取 max。每张照片各贡献一份 1，取 max 的话
            # 「今天新增了几张」永远答 1，而这条聚合是永久保存的。
            # app_usage.open_count 先踩过同一个坑；现在
            # check_counting_strategies_can_actually_count 会拦住第三次。
            aggregation_strategy="occurrence_count",
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


# ---------------------------------------------------------------------------
# §5.5 Health 与长期趋势
#
# 保留期一律「样本与聚合都永久」（照产品规范）—— 和 Focus/Motion/Music 不一样：
# 健康数据的【明细本身】就有长期价值（"我三年前的静息心率是多少"真的有人问），
# 而且量小得多，一天几十条不是几百条。（hx 2026-08-28）
#
# 三道防线一起用（hx 2026-08-28）：换算 → 值域 → 跳变。
# 单独任何一道都挡不住"设备把单位标错"：70 kg 标成 lb，换算完 31.8 kg，
# 值域完全合法，只有"一次掉了 55%"这条能看出不对。
# ---------------------------------------------------------------------------

HEALTH_SLEEP = SignalDefinition(
    key="health_sleep",
    label="睡眠",
    schema_version=1,
    capability="health_sleep",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0,
    identity_strategy="source_event_id",
    # 一个阶段就是一条并列的事实，不是"最新的那条睡眠"。不分维度的话
    # 三条阶段观测会落到同一个 dimension_key 上互相覆盖，当前值只剩最后
    # 进来的那个阶段 —— 现在没人读它，但那是给下一个使用者埋的雷。
    # 聚合层也用这一格当分桶键（见 aggregate.fold_into_day）。
    dimension_fields=("stage",),
    # 整段归【结束】那天：8月27日 23:40 睡、28日 07:20 醒 → 全算 28 日。
    # 和人说话的方式一致 —— 28 号早上你说"我昨晚睡了七个半小时"。
    attribution_strategy="episode_end",
    history_retention_days=PERMANENT,
    source_profile="health_sample",
    fields=(
        FieldDefinition(
            key="stage", value_type="enum", privacy_class="sensitive", nullable=False,
            enum=("awake", "core", "deep", "rem", "asleep", "unknown"),
            # 阶段自己**不聚合**，它是分桶的键。求和挂在 duration_minutes 上
            # （真正被加总的是分钟）。
            #
            # 曾经声明成 duration_by_state —— 那是**驻留**算法，靠相邻观测的
            # 时间差反推时长。而来源直接把"这个阶段 250 分钟"告诉我们了，
            # 一次上报里几条阶段观测的时刻完全相同，差值为 0，桶被写成
            # {"core": 0.0}：比空的更糟，它看起来像数据。
            aggregation_strategy="none",
            comparison_strategy="state_change", query_visibility="on_demand",
        ),
        FieldDefinition(
            key="duration_minutes", value_type="number", unit="minutes",
            privacy_class="sensitive", valid_range=(0.0, 1440.0),
            # 🔴 **不能是 daily_total**：那个走 CUMULATIVE，当天代表值取
            # **max** 而不是求和。core 250 / deep 70 / rem 110 会被答成
            # "昨晚睡了 250 分钟"，而不是 430 —— 一个错的数字，不报错。
            aggregation_strategy="duration_sum_by_state",
            trend_model="fluctuating",
            comparison_strategy="threshold_crossing",
            max_relative_jump=None,   # 睡眠时长天天不同，跳变检查没有意义
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(key="start_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
        FieldDefinition(key="end_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
    ),
)

HEALTH_WORKOUT = SignalDefinition(
    key="health_workout", label="运动", schema_version=1,
    capability="health_workout", storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0, identity_strategy="source_event_id",
    attribution_strategy="episode_end", history_retention_days=PERMANENT,
    source_profile="health_sample",
    fields=(
        FieldDefinition(
            key="workout_type", value_type="string", privacy_class="sensitive",
            nullable=False, aggregation_strategy="event_list",
            comparison_strategy="occurrence", wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="duration_minutes", value_type="number", unit="minutes",
            privacy_class="sensitive", valid_range=(0.0, 1440.0),
            aggregation_strategy="daily_sum", trend_model="fluctuating",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="active_energy_kcal", value_type="number", unit="kcal",
            accepted_units=("kj",), privacy_class="sensitive",
            valid_range=(0.0, 20000.0), query_visibility="on_demand",
            # 每次运动各贡献一份，当天求和。此前完全不聚合 ——
            # 「今天一共消耗多少/跑了多远」压根答不出来。
            aggregation_strategy="daily_sum",
            # 当天总量天天不同，比的是"比平时多还是少" —— 和睡眠时长同一档。
            trend_model="fluctuating",
                ),
        FieldDefinition(
            key="distance_m", value_type="number", unit="m",
            accepted_units=("km", "mi"), privacy_class="sensitive",
            valid_range=(0.0, 500000.0), query_visibility="on_demand",
            # 每次运动各贡献一份，当天求和。此前完全不聚合 ——
            # 「今天一共消耗多少/跑了多远」压根答不出来。
            aggregation_strategy="daily_sum",
            # 当天总量天天不同，比的是"比平时多还是少" —— 和睡眠时长同一档。
            trend_model="fluctuating",
                ),
        FieldDefinition(key="start_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
        FieldDefinition(key="end_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
    ),
)

HEALTH_VITALS = SignalDefinition(
    key="health_vitals", label="生命体征", schema_version=1,
    capability="health_vitals", storage_mode="current_timeline_aggregate",
    current_ttl_sec=3600.0, identity_strategy="source_event_id",
    attribution_strategy="instant", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note=(
        "⚠️ 建模方式和规范不同。规范用 metric + value + unit（一条观测一个指标），"
        "那需要「同一信号下多条并列当前值」的支持 —— 这个能力我们还没有"
        "（已记为已知缺口）。这里先按【每个指标一个字段】建模，和宿主现状一致，"
        "今天就能跑。等多维当前值做出来再切回规范的形态。"
    ),
    fields=(
        FieldDefinition(
            key="resting_heart_rate", value_type="number", unit="bpm",
            privacy_class="sensitive", valid_range=(20.0, 200.0),
            aggregation_strategy="numeric_dist", trend_model="fluctuating",
            comparison_strategy="numeric_delta", max_relative_jump=0.5,
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="current_heart_rate", value_type="number", unit="bpm",
            privacy_class="sensitive", valid_range=(20.0, 250.0),
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="hrv_sdnn_ms", value_type="number", unit="ms",
            privacy_class="sensitive", valid_range=(0.0, 500.0),
            aggregation_strategy="numeric_dist", trend_model="fluctuating",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="respiratory_rate", value_type="number", unit="breaths_per_minute",
            privacy_class="sensitive", valid_range=(4.0, 60.0),
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="oxygen_saturation_pct", value_type="number", unit="percent",
            privacy_class="sensitive", valid_range=(50.0, 100.0),
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="vo2_max", value_type="number", unit="ml_kg_min",
            privacy_class="sensitive", valid_range=(5.0, 100.0),
            aggregation_strategy="main_of_day", trend_model="drifting",
            query_visibility="on_demand",
        ),
    ),
)

HEALTH_ACTIVITY = SignalDefinition(
    key="health_activity", label="今日活动量", schema_version=1,
    capability="health_activity", storage_mode="current_timeline_aggregate",
    current_ttl_sec=3600.0, identity_strategy="source_event_id",
    # 上游直接给本地日期。和 steps 一样是日内单调累加的量。
    attribution_strategy="source_local_date", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note=(
        "和 steps 同一种形态：日内单调累加，当天代表值取【最大值】不是求和。"
        "取最大值天然不怕跨天回退 —— 00:01 的新一天读数会归到新的一天，"
        "不会和昨天的数字相减产生负增量。"
    ),
    fields=(
        FieldDefinition(
            key="active_energy_kcal", value_type="number", unit="kcal",
            accepted_units=("kj",), privacy_class="sensitive",
            valid_range=(0.0, 20000.0), aggregation_strategy="daily_total",
            trend_model="fluctuating", comparison_strategy="threshold_crossing",
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="exercise_minutes", value_type="number", unit="minutes",
            privacy_class="sensitive", valid_range=(0.0, 1440.0),
            aggregation_strategy="daily_total", trend_model="fluctuating",
            comparison_strategy="threshold_crossing", wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="stand_minutes", value_type="number", unit="minutes",
            privacy_class="sensitive", valid_range=(0.0, 1440.0),
            aggregation_strategy="daily_total", trend_model="fluctuating",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="mindful_minutes", value_type="number", unit="minutes",
            privacy_class="sensitive", valid_range=(0.0, 1440.0),
            aggregation_strategy="daily_total", trend_model="fluctuating",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            # 来源的计数器重置时换一个值（HealthKit 换设备、用户重装…）。
            # **重置不是修订**：把「重置到 0」当成修订，它会被"取 max"的
            # 单调假设吃掉，当天的数永远停在重置前的最大值。
            # 生产方不给时按单调计数器处理（退回取 max），所以老客户端不受影响。
            key="counter_epoch_id",
            value_type="string",
            privacy_class="public",
            aggregation_strategy="none",
            query_visibility="never",
        ),
    ),
)

def _split_off(base: SignalDefinition, *, key: str, label: str,
               fields: tuple[str, ...], note: str = "",
               **over: object) -> SignalDefinition:
    """从一个多指标信号里切出一个单指标信号。

    **字段属性原样带过来**，不手抄 —— 十二个信号手抄一遍，必然有一处
    单位或值域抄错，而那种错落库之后看不出来（值合法、单位标错）。

    为什么要拆（2026-09-06 拍板）：保留期、身份策略、当前值有效期是
    **整个信号共用**的，而这些指标的生命周期本来就不一样 ——
    体重要永久留、身高几年才变一次、实时心率是"最近一次读数"而
    静息心率是"一次测量"。挤在一个信号里，它们被迫共用一套声明；
    更要命的是逐条样本天然一次只带一个指标，存进去会把同信号的
    兄弟字段从当前值里**静默抹掉**（实测过）。
    """
    picked = tuple(f for f in base.fields if f.key in fields)
    missing = set(fields) - {f.key for f in picked}
    if missing:
        raise ValueError(f"{key}: 源信号里没有这些字段 {sorted(missing)}")
    return replace(base, key=key, label=label, fields=picked,
                   note=note or base.note, **over)


HEALTH_BODY = SignalDefinition(
    key="health_body", label="身体测量", schema_version=1,
    capability="health_body", storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0, identity_strategy="source_event_id",
    attribution_strategy="instant", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note=(
        "🔴 这一组是「用户改数据」最常发生的地方（体重录错、手动补录）——"
        "修订机制主要为它们服务。也是单位最容易标错的一组（kg / lb），"
        "所以 max_relative_jump 卡得比别的紧：70 kg 被标成 lb，"
        "换算完 31.8 kg 值域完全合法，只有「一次掉 55%」能看出不对。"
    ),
    fields=(
        FieldDefinition(
            key="weight_kg", value_type="number", unit="kg",
            accepted_units=("lb", "g"), privacy_class="sensitive",
            valid_range=(2.0, 500.0), aggregation_strategy="main_of_day",
            trend_model="drifting", comparison_strategy="numeric_delta",
            max_relative_jump=0.2, wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="bmi", value_type="number", unit="kg_m2",
            privacy_class="sensitive", valid_range=(5.0, 100.0),
            aggregation_strategy="main_of_day", trend_model="drifting",
            max_relative_jump=0.2, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="body_fat_ratio", value_type="number", unit="ratio",
            privacy_class="sensitive", valid_range=(0.01, 0.75),
            aggregation_strategy="main_of_day", trend_model="drifting",
            max_relative_jump=0.3, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="height_cm", value_type="number", unit="cm",
            accepted_units=("in", "m", "ft"), privacy_class="sensitive",
            valid_range=(30.0, 280.0), max_relative_jump=0.1,
            query_visibility="on_demand",
        ),
    ),
)

HEALTH_METABOLIC = SignalDefinition(
    key="health_metabolic", label="代谢点值", schema_version=1,
    capability="health_metabolic", storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0, identity_strategy="source_event_id",
    attribution_strategy="instant", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note=(
        "血糖本身波动就大（餐前餐后能差一倍），所以不设跳变阈值 —— "
        "设了会天天误报。血压相对稳定，设一个宽的。"
    ),
    fields=(
        FieldDefinition(
            key="blood_glucose_mmol_l", value_type="number", unit="mmol_l",
            accepted_units=("mg_dl",), privacy_class="sensitive",
            valid_range=(0.5, 40.0), aggregation_strategy="numeric_dist",
            trend_model="fluctuating", comparison_strategy="threshold_crossing",
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="blood_pressure_systolic_mmhg", value_type="number", unit="mmhg",
            privacy_class="sensitive", valid_range=(50.0, 260.0),
            aggregation_strategy="numeric_dist", trend_model="fluctuating",
            max_relative_jump=0.6, comparison_strategy="threshold_crossing",
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="blood_pressure_diastolic_mmhg", value_type="number", unit="mmhg",
            privacy_class="sensitive", valid_range=(30.0, 180.0),
            aggregation_strategy="numeric_dist", trend_model="fluctuating",
            max_relative_jump=0.6, query_visibility="on_demand",
        ),
    ),
)

HEALTH_CYCLE = SignalDefinition(
    key="health_cycle", label="经期", schema_version=1,
    capability="health_cycle", storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0, identity_strategy="source_event_id",
    attribution_strategy="instant", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note="周期型：看【间隔】不看数值高低 —— 「比平均晚了 4 天」才是信号。",
    fields=(
        FieldDefinition(
            key="is_active_period", value_type="boolean", privacy_class="sensitive",
            aggregation_strategy="main_of_day", comparison_strategy="state_change",
            wake_eligible=True, query_visibility="on_demand",
        ),
        FieldDefinition(
            key="flow_level", value_type="enum", privacy_class="sensitive",
            enum=("none", "light", "medium", "heavy", "unspecified"),
            comparison_strategy="state_change", query_visibility="on_demand",
        ),
        FieldDefinition(key="start_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
        FieldDefinition(key="end_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
    ),
)

HEALTH_MOOD = SignalDefinition(
    key="health_mood", label="心情", schema_version=1,
    capability="health_mood", storage_mode="current_timeline_aggregate",
    current_ttl_sec=86400.0, identity_strategy="source_event_id",
    attribution_strategy="instant", history_retention_days=PERMANENT,
    source_profile="health_sample",
    note="用户自己记的，一天可能好几条 —— 所以是 event_list 不是取当天某一个值。",
    fields=(
        FieldDefinition(
            key="valence", value_type="number", unit="scale_minus1_to_1",
            privacy_class="sensitive", valid_range=(-1.0, 1.0),
            aggregation_strategy="numeric_dist", trend_model="fluctuating",
            comparison_strategy="numeric_delta", wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="valence_classification", value_type="string",
            privacy_class="sensitive", query_visibility="on_demand",
        ),
        FieldDefinition(
            key="kind", value_type="string", privacy_class="sensitive",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="labels", value_type="array", privacy_class="sensitive",
            query_visibility="on_demand",
        ),
        FieldDefinition(key="recorded_at", value_type="timestamp",
                        privacy_class="sensitive", query_visibility="on_demand"),
    ),
)


#: manifest 的全部内容。逐条过完 Seven 的文档后往里加。
# ---------------------------------------------------------------------------
# proximity_anchor —— 「此刻在哪个东西旁边」
# ---------------------------------------------------------------------------

PROXIMITY_ANCHOR = SignalDefinition(
    key="proximity_anchor",
    label="连接性锚点",
    schema_version=1,
    capability="location",
    # 产品规范 §3.3 和 §7.13 两处都把 Wi-Fi / 蓝牙连接归为「当前 + 短期
    # 时间线」，没有长期聚合。先前这里写成 current_timeline_aggregate
    # 并配了永久聚合，理由是"同 focus/motion" —— **那是我们自己的外推，
    # 产品方没这么说，也没人拍过板**。改回他的分类，把"要不要给锚点留
    # 永久 dwell 聚合"作为建议提出去，而不是先斩后奏。
    storage_mode="current_short_timeline",
    # 和 location_city 一样取 900s：它同样跟着整份快照走，
    # 前台 30s / 后台保活 5min / 被挂起后不可控。
    current_ttl_sec=900.0,
    # anchor_id 本身就是稳定身份，但一次「连着」不是一个事件 ——
    # 用 (signal, occurred_at, 值摘要) 造确定性键，重传能对上。
    identity_strategy="deterministic_digest",
    # 同上：时间点快照，时长由聚合层从相邻观测算。
    attribution_strategy="instant",
    # 产品规范 §1-15：Wi-Fi / 蓝牙连接历史保留 7 天。
    # 每个 anchor 各一条当前值。同时连着家里和公司是两个答案；
    # 用户搬家后新旧网络都叫 "home",按名字看是一个、按 anchor_id 看是两个。
    dimension_fields=("anchor_id",),
    history_retention_days=7,
    source_profile="location",
    note=(
        "和 location_city 是【两个信号，不是一个字段的粗细两档】。城市回答"
        "「在哪座城」，锚点回答「在哪个地方」—— 混进同一个字段，搬家之后"
        "新旧两个「home」就看不出区别了（产品规范 §5.2-6 点名的场景）。\n"
        "两处和规范不一致，都是 iOS 平台限制：\n"
        "① anchor_type 的 bluetooth 这一档基本拿不到 —— iOS 不给第三方看"
        "系统级蓝牙连接，只有音频输出设备这一个子集，那部分走 audio_route。\n"
        "③ 我们建议给它加一层永久的 dwell 聚合（每天在各锚点待了多久）：体量很小，"
        "而「今年在家的时间比去年多吗」正是时间越久越值钱的那类问题。"
        "**但规范把这个信号归为「当前 + 短期时间线」，没有长期聚合，所以现在照规范做，"
        "这条只是建议。**\n"
        "② connect/disconnect 边缘取决于「app 被后台唤起时还读不读得到 Wi-Fi」，"
        "这一条正在真机实测。读不到的话 dwell 只能从相邻快照推，"
        "精度 = 上报间隔，且用户全程在后台的那段会整块漏掉。"
    ),
    fields=(
        FieldDefinition(
            key="anchor_id",
            value_type="string",
            privacy_class="personal",
            nullable=False,
            comparison_strategy="exact",
            wake_eligible=True,
            query_visibility="always",
            note=(
                "端上算的 HMAC 假名（iOS 拿 BSSID 算，截断 64bit），"
                "稳定但不可逆 —— 后端能认出「又是这个网络」，还原不出 BSSID。"
                "重装 app 会换一批密钥，锚点重新学，这是可接受的代价。"
            ),
        ),
        FieldDefinition(
            key="anchor_type",
            value_type="enum",
            privacy_class="personal",
            nullable=False,
            enum=("wifi", "bluetooth"),
            comparison_strategy="exact",
            query_visibility="always",
            note="bluetooth 这一档 iOS 基本给不了，见信号级 note。",
        ),
        FieldDefinition(
            key="label",
            value_type="string",
            privacy_class="personal",
            comparison_strategy="none",
            query_visibility="always",
            note=(
                "用户给这个锚点起的名字。**可以改名，改名不影响 anchor_id** ——"
                "身份和标签分开正是规范 §5.2-4 要的。"
            ),
        ),
        FieldDefinition(
            key="is_connected",
            value_type="boolean",
            privacy_class="personal",
            nullable=False,
            comparison_strategy="state_change",
            aggregation_strategy="duration_by_state",
            wake_eligible=True,
            query_visibility="always",
        ),
        FieldDefinition(
            # 和 location_city.coordinate 同一个道理：声明出来，是为了让
            # 「永不持久化、永不给 agent」成为一条可被测试检查的规则。
            key="raw_identifier",
            value_type="string",
            privacy_class="restricted",
            query_visibility="never",
            note="原始 BSSID / 设备地址。端上算完假名就丢弃，永远不该出设备。",
        ),
    ),
)


# ---------------------------------------------------------------------------
# music_playback —— 在听什么
# ---------------------------------------------------------------------------

MUSIC_PLAYBACK = SignalDefinition(
    key="music_playback",
    label="正在播放",
    schema_version=1,
    capability="now_playing",
    storage_mode="current_timeline_aggregate",
    # 产品规范给 600s，照做。音乐这个信号的采集节奏和 focus/motion 不一样 ——
    # 系统播放器是事件驱动的，切歌 2 秒后就有新值，不靠保活轮询兜底。
    current_ttl_sec=600.0,
    identity_strategy="deterministic_digest",
    # 同上：时间点快照，时长由聚合层从相邻观测算。
    attribution_strategy="instant",
    # 明细 1 年、聚合永久（hx 2026-08-28，同 focus/motion 那条决定）。
    history_retention_days=365,
    aggregate_retention_days=PERMANENT,
    note=(
        "两处和产品规范不同，都是 iOS 平台限制：\n"
        "① **没有 track_id**。Apple 是给歌曲持久化 ID 的，但 iOS 侧当初为隐私"
        "主动砍掉了 —— 那个 ID 能反查用户整个曲库。我们用 (title, artist) 的"
        "哈希当稳定身份，代价是同名同歌手的两首（现场版 / 录音室版）会被"
        "当成同一首。\n"
        "② **播放边缘只覆盖一半播放器**。iOS 订阅的是 systemMusicPlayer，"
        "也就是 Apple Music / 系统播放器：切歌 2 秒后就上报，起止时刻是准的。"
        "Spotify、网易云用自己的播放器，不发这些通知，只能靠快照采到的那几个点。"
        "所以派生 session 的 quality **不能一刀切成 estimated** —— 规范 §5.3 "
        "写的是「只有轮询样本就标 estimated」，实际是同一个信号两种精度并存，"
        "一律标 estimated 会把本来准确的那一半信息丢掉。"
    ),
    fields=(
        FieldDefinition(
            key="track_key",
            value_type="string",
            privacy_class="sensitive",
            nullable=True,
            # ★ 「什么都没在放」是一个**正常状态**，而它没有曲目。
            #
            #   非空的话，播放器停着的那条观测因为缺必填字段被整条拒掉 ——
            #   于是 kit 永远不知道「音乐停了」，只知道「在放什么」。
            #   真机上这条占了 83 次差异里的 83 次：老路记着 stopped，
            #   kit 那边一片空白。
            #
            #   规范 §5.3 把 track_id 列为字段，没说它任何时候都必须有值。,
            comparison_strategy="exact",
            wake_eligible=True,
            query_visibility="on_demand",
            note=(
                "(title, artist) 的哈希，替代规范里的 track_id。"
                "**不是 Apple 的持久化 ID** —— 那个能反查曲库，iOS 侧不给。"
            ),
        ),
        FieldDefinition(
            key="title",
            value_type="string",
            privacy_class="sensitive",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="artist",
            value_type="string",
            privacy_class="sensitive",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="album",
            value_type="string",
            privacy_class="sensitive",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="playback_state",
            value_type="enum",
            privacy_class="sensitive",
            nullable=False,
            enum=("playing", "paused", "stopped"),
            comparison_strategy="state_change",
            aggregation_strategy="duration_by_state",
            wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="position_seconds",
            value_type="number",
            unit="s",
            privacy_class="sensitive",
            valid_range=(0, None),
            comparison_strategy="none",
            query_visibility="on_demand",
            note="播放进度。不参与比较 —— 它每秒都在变，当成变化会把每次采样都算成一次事件。",
        ),
        FieldDefinition(
            key="edge_quality",
            value_type="enum",
            privacy_class="sensitive",
            nullable=False,
            enum=("measured", "estimated"),
            comparison_strategy="none",
            query_visibility="always",
            note=(
                "这条记录的起止时刻是**真事件**还是**从相邻快照推的**。\n"
                "systemMusicPlayer（Apple Music）来的标 measured，"
                "第三方播放器只能靠快照采到，标 estimated。\n"
                "**把这个写进数据本身**，是因为读到值的人不一定读过文档 —— "
                "同一个信号两种精度并存，不说出来就会被当成一样准。"
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# app_usage —— 打开过哪些 app
# ---------------------------------------------------------------------------

APP_USAGE = SignalDefinition(
    key="app_usage",
    label="应用使用",
    schema_version=1,
    capability="app",
    storage_mode="current_timeline_aggregate",
    current_ttl_sec=900.0,
    identity_strategy="source_event_id",
    attribution_strategy="instant",
    history_retention_days=PERMANENT,
    aggregate_retention_days=PERMANENT,
    source_profile="device_occurrence",
    note=(
        "⚠️ **这个信号的覆盖面天然残缺，不是实现没做好。**\n"
        "iOS 拿不到前台 app（`frontmost_app` 恒为 null），数据全靠用户在"
        "「快捷指令」里逐个 app 手动配自动化。没配的 app 在我们眼里完全不存在 ——"
        "用户刷了三小时小红书，只要没登记它，看上去就像一整天没用手机。\n\n"
        "**所以刻意不建「每天用了多久」这类时长统计。** 产品规范 §1-7 要求"
        "永久保存可重建的 session 和长期时长统计，但那份统计会是一份"
        "大概率残缺的轨迹，而 agent 基于它判断「他今天用手机多不多」"
        "从根上不成立 —— 错得不明显，比没有更糟。\n\n"
        "**做的是只靠 open 就能答、且答得准的那部分**：最近打开了什么、"
        "今天打开了几次。这两个问题不需要 close，所以配了 open 没配 close 的"
        "用户也拿得到正确答案。\n\n"
        "已把三个选项交给产品方（照做但标不可信 / 只做 open 侧 / 等原生能力），"
        "**当前实现是「只做 open 侧」**。要改回完整时长统计是加东西，不是改东西 ——"
        "反过来（先建了永久时长表再撤）那张表已经在收数据了。"
    ),
    fields=(
        FieldDefinition(
            key="app_id",
            value_type="string",
            privacy_class="sensitive",
            nullable=False,
            comparison_strategy="exact",
            wake_eligible=True,
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="app_name",
            value_type="string",
            privacy_class="sensitive",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="category",
            value_type="string",
            privacy_class="sensitive",
            comparison_strategy="none",
            query_visibility="on_demand",
        ),
        FieldDefinition(
            key="action",
            value_type="enum",
            privacy_class="sensitive",
            nullable=False,
            enum=("open", "close"),
            comparison_strategy="exact",
            query_visibility="on_demand",
            note=(
                "`close` **收下但不据此算时长**。用户可能只配了 open 那条自动化，"
                "于是绝大多数 session 根本没有结束事件 —— 拿有 close 的那部分"
                "算平均时长，会得到一个只反映「谁配得全」的数字。"
            ),
        ),
        FieldDefinition(
            key="open_count",
            value_type="integer",
            unit="count",
            privacy_class="sensitive",
            valid_range=(0, None),
            # 每条 open 贡献 1，当天**求和**。**只靠 open，所以可信** ——
            # 这正是砍掉时长统计之后仍然答得准的那部分。
            # ⚠️ 曾经写的是 daily_total（取 max），于是「今天打开了几次」
            # 永远等于 1 —— 这个信号唯一保证答得准的问题，答错了。
            aggregation_strategy="occurrence_count",
            comparison_strategy="none",
            query_visibility="on_demand",
            trend_model="fluctuating",
            note="「今天打开了几次」。不需要 close 就能答，所以配置不全也不影响。",
        ),
    ),
)



# ---------------------------------------------------------------------------
# 上面三个多指标信号切成单指标（2026-09-06 拍板，理由见 _split_off 的文档）
#
# 🔴 切出来的才是 manifest 里的正式信号；HEALTH_BODY / HEALTH_VITALS /
#    HEALTH_METABOLIC 三个只作为**声明的模板**存在，不进 MINIMAL_SIGNALS。
#    留两套并存 = 第二套目录，正是 0.3.0 刚清掉的那种双重真相。
# ---------------------------------------------------------------------------

HEALTH_WEIGHT = _split_off(
    HEALTH_BODY, key="health_weight", label="体重", fields=("weight_kg",),
    note=("「用户改数据」最常发生的地方（录错、手动补录）——修订和撤回主要"
          "为它服务。也是单位最容易标错的：70 kg 被标成 lb，换算完 31.8 kg "
          "值域完全合法，只有「一次掉 55%」能看出不对。"),
)
HEALTH_BMI = _split_off(
    HEALTH_BODY, key="health_bmi", label="BMI", fields=("bmi",),
    note=("通常由 app 从体重和身高算出来，不一定有独立的来源样本 —— "
          "所以它可能拿不到稳定身份，撤回也就落不到它头上。"),
)
HEALTH_BODY_FAT = _split_off(
    HEALTH_BODY, key="health_body_fat", label="体脂率",
    fields=("body_fat_ratio",),
)
HEALTH_HEIGHT = _split_off(
    HEALTH_BODY, key="health_height", label="身高", fields=("height_cm",),
    storage_mode="current_only", history_retention_days=0,
    note=("几年才变一次，**不存历史**。挤在 health_body 里时它跟着存了明细，"
          "而它自己的字段没有聚合策略 —— 那些明细没有任何东西读得到，"
          "只是白占地方。拆开之后校验器直接把这条指出来了。"),
)

HEALTH_RESTING_HR = _split_off(
    HEALTH_VITALS, key="health_resting_hr", label="静息心率",
    fields=("resting_heart_rate",),
    note=("一天测一次，是「一次测量」不是「当日代表值」—— 用户能指着某一次说"
          "「删掉它」。⚠️ 当前值有效期沿用了 health_vitals 的 1 小时，"
          "对一天一次的量偏短；改它是独立的产品决定，本次不动。"),
)
HEALTH_HRV = _split_off(
    HEALTH_VITALS, key="health_hrv", label="心率变异性",
    fields=("hrv_sdnn_ms",),
)
HEALTH_RESPIRATORY = _split_off(
    HEALTH_VITALS, key="health_respiratory", label="呼吸率",
    fields=("respiratory_rate",),
    note=("⚠️ 拆分暴露的旧账：它在趋势表里声明了 fluctuating，但字段没有聚合策略，"
          "而趋势是从日聚合读的 —— 于是「最近呼吸率怎么样」永远读到空。"
          "挤在 health_vitals 里时靠兄弟字段蒙混过了 manifest 校验。"),
)
HEALTH_RESPIRATORY = replace(HEALTH_RESPIRATORY, fields=(
    replace(HEALTH_RESPIRATORY.fields[0], aggregation_strategy="numeric_dist",
            trend_model="fluctuating"),
))
HEALTH_OXYGEN = _split_off(
    HEALTH_VITALS, key="health_oxygen", label="血氧",
    fields=("oxygen_saturation_pct",),
    note="同 health_respiratory：声明了趋势却没有聚合，趋势永远读到空。",
)
HEALTH_OXYGEN = replace(HEALTH_OXYGEN, fields=(
    replace(HEALTH_OXYGEN.fields[0], aggregation_strategy="numeric_dist",
            trend_model="fluctuating"),
))
HEALTH_VO2MAX = _split_off(
    HEALTH_VITALS, key="health_vo2max", label="最大摄氧量",
    fields=("vo2_max",),
)
HEALTH_CURRENT_HR = _split_off(
    HEALTH_VITALS, key="health_current_hr", label="实时心率",
    fields=("current_heart_rate",), storage_mode="current_only",
    history_retention_days=0,
    note=("**这一个不走逐条样本。** 运动时每几秒一条，而它的语义就是"
          "「最近一次读数」——不是一条你会想删掉的测量记录。"
          "当日权威值那一档：同一天最新的查询结果赢。"),
)

HEALTH_GLUCOSE = _split_off(
    HEALTH_METABOLIC, key="health_glucose", label="血糖",
    fields=("blood_glucose_mmol_l",),
    note=("波动本来就大（餐前餐后能差一倍），所以不设跳变阈值 —— 设了会天天误报。"),
)
HEALTH_BLOOD_PRESSURE = _split_off(
    HEALTH_METABOLIC, key="health_blood_pressure", label="血压",
    fields=("blood_pressure_systolic_mmhg", "blood_pressure_diastolic_mmhg"),
    note=("🔴 收缩压和舒张压**留在同一个信号里**，因为来源侧它们是一次读数"
          "（HealthKit 建模成 correlation）。拆成两个信号就丢了「这是同一次量的」"
          "这个事实 —— 撤回时两条各自被删，中间任何一步失败就留下半条读数。"),
)

MINIMAL_SIGNALS: dict[str, SignalDefinition] = {
    s.key: s for s in (
        # 阶段二的五个代表信号
        BATTERY, PRESENCE_RECOVERY, STEPS, LOCATION_CITY, FOCUS_STATE,
        # §5.1 时间、设备与短期环境
        TIME_CONTEXT, BROADCAST, SCREEN_CHANGE, AUDIO_ROUTE, WEATHER,
        # §5.2 位置与连接性锚点
        PROXIMITY_ANCHOR,
        # §5.3 行为、应用与媒体
        MOTION_STATE, PHOTO_LIBRARY_ADDED, MUSIC_PLAYBACK, APP_USAGE,
        # §5.5 健康与长期趋势
        HEALTH_SLEEP, HEALTH_WORKOUT, HEALTH_ACTIVITY,
        HEALTH_CYCLE, HEALTH_MOOD,
        # 身体测量、体征、代谢：拆成单指标（见 _split_off）。
        # 逐条样本一次只带一个指标，挤在一个信号里会把兄弟字段
        # 从当前值里静默抹掉。
        HEALTH_WEIGHT, HEALTH_BMI, HEALTH_BODY_FAT, HEALTH_HEIGHT,
        HEALTH_RESTING_HR, HEALTH_HRV, HEALTH_RESPIRATORY,
        HEALTH_OXYGEN, HEALTH_VO2MAX, HEALTH_CURRENT_HR,
        HEALTH_GLUCOSE, HEALTH_BLOOD_PRESSURE,
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
    "PROXIMITY_ANCHOR",
    "MOTION_STATE", "PHOTO_LIBRARY_ADDED", "MUSIC_PLAYBACK", "APP_USAGE",
    "HEALTH_SLEEP", "HEALTH_WORKOUT", "HEALTH_VITALS", "HEALTH_ACTIVITY",
    "HEALTH_BODY", "HEALTH_METABOLIC", "HEALTH_CYCLE", "HEALTH_MOOD",
    "MINIMAL_SIGNALS", "DECLINED_SIGNALS",
]
