# PerceptKit 感知字段与存储（工程对齐补充 / 初步结论 v0.1）

> 日期：2026-08-27
> 面向对象：PerceptKit 工程师、iOS 感知上报工程师、宿主 Runtime 工程师及其 AI Coding Agent
> 文档性质：对《PerceptKit 产品目标、当前实现差距与后续交付要求》的补充，聚焦“采集到的感知字段如何标准化、使用和存储”
> 重要说明：本文描述的是**目标规范与初步产品结论**，不是对当前 PerceptKit 已实现能力的描述。

## 0. 给工程师和工程 AI 的阅读规则

本文的结论分为三级：

| 标记 | 含义 | 执行方式 |
|---|---|---|
| `已确认` | 产品侧已经明确表达的目标 | 实现和接口设计应遵守；如与旧文档或当前代码冲突，以本文关于字段和存储的结论为准 |
| `工程要求` | 为保证幂等、可追溯和可插拔而必须具备的工程语义 | 可以选择不同数据库或代码结构，但不得丢失该保证 |
| `建议/待确认` | 当前最合理的初步方案，但仍允许讨论 | 不要悄悄固化为不可迁移的公共契约；实现前应明确记录最终选择 |

特别纠正此前反馈文档中的一处示例：

- **撤回 `device_boot` / `device_restart` 示例。**当前不需要手机开机或重启信号。
- 产品所说的是“设备从锁屏状态被解锁”，标准信号应为 `device_unlock`。
- `device_unlock` 不得命名为 restart、boot、started，也不得让 Agent 将它解释为手机重新开机。

本文不是要求把旧 Feedling 数据库原样搬入 PerceptKit。本文要求 PerceptKit 定义**逻辑数据模型、字段语义和 StoragePort 行为**；宿主决定使用 PostgreSQL、SQLite、文档数据库或其他实现。

---

## 1. 本次已经确认的产品结论

1. `已确认` 所有 wake/device 旁路信号都必须先标准化为普通 Signal/Observation，再由 EventDefinition 产生 Event；不能继续保留“只有 wake 能读懂”的特殊字段。
2. `已确认` PerceptKit 不负责具体采集、HTTP 路由、加密、解密、密钥、enclave 或某个数据库实现。
3. `已确认` PerceptKit 必须定义字段规范、逻辑存储结构、处理规则、查询语义、Event 接口和 StoragePort/WakePort 行为。
4. `已确认` Current、历史时间线、长期聚合、来源镜像是四种不同数据形态，不能全部塞进一个 current JSON，也不能所有数据一律永久保存原始 payload。
5. `已确认` 城市级 Location 的变化历史永久保存；精细位置锚点由用户命名的 Wi-Fi/蓝牙连接表达，两者不能混为一个 `home/work` 字段。
6. `已确认` Focus、Motion 的状态变化明细和长期聚合都永久保存。
7. `已确认` App open/close、可重建 session 和长期统计永久保存，不再按固定条数截断。
8. `已确认` Music playback 明细、session 和长期统计永久保存。
9. `已确认` Health 标准样本和长期趋势永久保存。
10. `已确认` Calendar、Reminder 是来源镜像：当时能够访问到的历史和未来都应同步；来源新增、修改、删除时，本地随之更新；不需要保存每一次上报快照。
11. `已确认` `photo_library_added` 每新增一张照片贡献 `count = 1`；每日新增总数永久保存；照片以后被删除不回减历史新增数量。
12. `已确认` Unlock 只保存最近一次解锁及此前锁屏/未解锁时长，不保存长期解锁时间列表。
13. `已确认` Screen Change 只留 Current；Battery 只留 Current；Battery 增加 Low Power Mode。
14. `已确认` Broadcast、Weather、Audio Route 历史保留 7 天。
15. `已确认` Wi-Fi、蓝牙连接历史保留 7 天。
16. `已确认` Time Zone 只在发生变化时追加变化记录并永久保存；本地时间本身可以实时推导，不需要不断写历史。

---

## 2. 统一术语

| 术语 | 准确定义 | 示例 |
|---|---|---|
| Capability | 用户授权或产品展示层的一类感知能力 | Location、Health、Calendar |
| Signal Definition | 某类数据的字段、类型、TTL、历史和聚合规则 | `battery`、`motion_state` |
| Observation | 某一时刻真实发生或测量到的一条标准化事实 | 10:03 Motion 变为 walking |
| Current Projection | 从 Observation 或来源镜像派生的最新状态 | 当前城市是上海；最近一次解锁在 10:03 |
| Timeline | 按发生时间追加的明细 | App open/close、Location 城市变化 |
| Daily Aggregate | 从明细派生的每日统计 | 当日步数、Focus 分钟数、照片新增数量 |
| Source Mirror | 跟随外部可变集合维护的当前副本 | Calendar Event、Reminder Item |
| Metadata | 描述数据本身的控制信息，不是用户感知内容 | `schema_version`、`source_event_id`、`occurred_at`、单位、来源 |
| EventDefinition | 用户或宿主定义“什么条件算一个事件” | 步数从 2,975 跨过 3,000 |
| PerceptionEvent | 某条规则被命中后产生的一次不可变事实 | `activity.step_goal_reached` |
| WakeReceipt | Runtime 对 Event 的接收结果 | accepted、suppressed、enqueue_failed |

Metadata 不是照片，也不是一段额外内容。它是让系统知道“这条数据来自哪里、什么时候发生、是否重复、按哪个版本解释”的字段。Metadata 默认不应原样暴露给 Agent。

---

## 3. 数据保存的四种标准模式

### 3.1 Current Only

只保留最近可信值，旧值被覆盖，不建立长期时间线。

适用：Battery、Unlock、Screen Change、本地时间。

### 3.2 Current + Timeline + Aggregate

保留最新状态，同时保存变化明细，并派生每日或窗口聚合。

适用：Location、Focus、Motion、App、Music、Health、Time Zone Change。

### 3.3 Current + Short Timeline

保留当前状态，变化明细只保留有限时间。

适用：Wi-Fi/蓝牙连接、Network、Broadcast、Weather、Audio Route。

### 3.4 Source Mirror

本地保存外部来源当前存在的完整项目集合；项目可以对应过去或未来，但本地不保存每次同步版本。

适用：Calendar、Reminder。

注意：Calendar 中“一年前的 Event”依然可以存在于当前来源镜像中。这不等于我们在保存“每次上传历史”，而是镜像中的 Event 自己带有过去的 `start_at/end_at`。

---

## 4. 最小标准上报与 Observation

### 4.1 Report Envelope

PerceptKit 核心不要求固定 HTTP API，但 Adapter 交给 PerceptKit 的逻辑输入至少应等价于：

```json
{
  "schema_version": 1,
  "report_id": "report_01J...",
  "producer": "ios",
  "producer_instance_id": "opaque-device-instance",
  "reported_at": "2026-08-27T10:30:04+08:00",
  "observations": []
}
```

| 字段 | 要求 |
|---|---|
| `schema_version` | 必填；决定整个 report 的解释版本 |
| `report_id` | 必填；一批上报的稳定幂等 ID |
| `producer` | 必填；例如 ios、android、wearable，但不得绑定 Feedling V1/V2 |
| `producer_instance_id` | 建议；宿主生成或匿名化，不应是可直接追踪的硬件序列号 |
| `reported_at` | 建议由 producer 提供；宿主另行注入 `received_at` |
| `observations` | 0..N 条标准 Observation；批量上报不改变单条语义 |

`subject_id/user_id` 应由宿主从已认证上下文注入，不能仅信任客户端自报。加密 envelope 在进入 PerceptKit 之前由 Adapter 解密。

### 4.2 Canonical Observation

```json
{
  "signal": "steps",
  "signal_schema_version": 1,
  "source_event_id": "healthkit-sample-or-stable-event-id",
  "occurred_at": "2026-08-27T10:30:00+08:00",
  "availability": "observed",
  "value": {
    "step_count": 3012
  },
  "timezone": "Asia/Shanghai"
}
```

所有 Observation 共有的逻辑字段：

| 字段 | 必需性 | 语义 |
|---|---:|---|
| `signal` | 必填 | 对应统一 manifest 中的 signal key |
| `signal_schema_version` | 必填 | 该 signal payload 的版本 |
| `source_event_id` | 事件/样本型必填 | 上游稳定身份；用于重传幂等 |
| `occurred_at` | 必填 | 实际测量或事件发生时间，不是上传时间 |
| `received_at` | 宿主注入 | 宿主收到数据的时间 |
| `availability` | 必填 | `observed`、`no_data`、`unavailable` |
| `value` | observed 时必填 | 由 signal manifest 定义的 typed payload |
| `timezone` | 建议 | 发生时的 IANA 时区，用于本地日期归属 |
| `source_revision` | 仅支持修订的来源 | Health/Calendar 等来源版本 |

### 4.3 Availability 的最小语义

| 状态 | 含义 | 是否更新数值 Current | 是否进入数值趋势 |
|---|---|---:|---:|
| `observed` | 已经获得可靠值；`0` 和 `false` 都是合法值 | 是 | 是 |
| `no_data` | 查询成功，但指定范围没有样本 | 否；只更新 coverage/诊断 | 否 |
| `unavailable` | 来源当前无法提供数据 | 否；可标记当前不可用 | 否 |

额外规则：

- 不需要 `observed_zero`；零值就是 `observed` 的合法 value。
- `stale` 不是上报状态，而是查询 Current 时通过 `occurred_at + current_ttl` 推导。
- 用户关闭某项能力属于宿主授权配置，不应伪装成感知事实反复上报给 Agent。
- `unavailable.reason` 如需记录，应使用粗粒度诊断值；权限细节默认不进入 Agent context。

---

## 5. 完整信号字段与保存规则总表

以下是目标规范。`Current TTL` 表示超过这个时间不能再称为“当前”；仍可作为带 `as_of` 的 last known 返回。TTL 是初始默认值，可由 manifest 版本化调整。

### 5.1 时间、设备与短期环境

| 状态 | Signal | 标准字段 | Current TTL | 明细/聚合 | 保留规则 | 主要用途 |
|---|---|---|---:|---|---|---|
| 已确认 | `time_context` | `time_zone_id: string`, `utc_offset_seconds: int`, `locale?: string` | 5 分钟 | 时区变化记录 | 时区变化永久；本地时间不存历史 | 当前时间语境、跨时区变化 |
| 已确认 | `battery` | `level_ratio: number[0,1]`, `is_charging: bool`, `is_low_power_mode_enabled: bool` | 10 分钟 | 无 | 只留 Current | 当前电量、低电量条件 |
| 已确认 | `broadcast` | `is_active: bool`, `occurred_at` | 5 分钟 | start/stop transition | 7 天 | 当前是否开启屏幕采集 |
| 已确认 | `screen_change` | `changed: bool`, `occurred_at` | 短期瞬时 | 无；pHash 仅作临时比较 | 只留 Current | 表达最近是否发生画面变化 |
| 已确认 | `device_unlock` | `unlocked_at`, `absence_before_unlock_seconds: number>=0`, `source_event_id` | 不按普通 TTL 失效；查询时返回 age | 不保存长期列表 | 只留最近一次 Current | 锁屏后解锁触发；不是 boot/restart |
| 已确认 | `audio_route` | `output_type`, `is_bluetooth`, `device_label?: string`, `occurred_at` | 10 分钟 | route transition | 7 天 | 当前声音输出方式 |
| 建议/待确认 | `network_connection` | `is_connected: bool`, `connection_type: wifi/cellular/ethernet/other/none`, `occurred_at` | 5 分钟 | connection transition | 7 天；当前按连接类统一 | 当前是否联网 |
| 已确认 | `weather` | `condition`, `temperature_c`, `apparent_temperature_c`, `humidity_ratio`, `precipitation_probability`, `uv_index`, `is_daylight`, `alerts[]`, `valid_at`, `location_scope` | 30 分钟 | 短期观测 | 7 天 | 当前及近期环境上下文 |

说明：

- Screen Change 只表示“发生了变化”，不得把原始屏幕、长期 pHash 序列或截图默认写入该 Signal。
- Broadcast 与 Screen Change 是两件事：Broadcast 表示屏幕采集会话是否开启；Screen Change 表示采集期间或采集源检测到的画面变化。
- `device_unlock` 需要上游能够可靠提供解锁发生时间；`absence_before_unlock_seconds` 若不能由 iOS 直接提供，必须有可靠的 lock/last-active baseline 才能计算，不能猜测。

### 5.2 Location 与位置锚点

| 状态 | Signal | 标准字段 | Current TTL | 明细/聚合 | 保留规则 | 主要用途 |
|---|---|---|---:|---|---|---|
| 已确认 | `location_city` | `city/locality`, `region?: string`, `country_code`, `occurred_at`, `accuracy_m?: number`, `placemark_source?: string` | 15 分钟 | 城市变化时间线；可派生 dwell | 明细和聚合永久 | 当前城市、迁移、常驻变化 |
| 已确认 | `proximity_anchor` | `anchor_id`, `anchor_type: wifi/bluetooth`, `label`, `is_connected: bool`, `occurred_at` | 5 分钟 | connect/disconnect、dwell session | 7 天 | 用户命名的 home/work/房间等精细位置 |

Location 规则：

1. 广义 Location 主要指城市/粗粒度地区，不是 `home/work`。
2. iOS 可以上传已解析的城市，也可以由 Adapter 临时使用经纬度反向解析。
3. 精确经纬度、BSSID、完整街道地址默认不进入 canonical Current/History。
4. `anchor_id` 是稳定身份；`label` 可以改名。历史连接不能只以 `home` 文本作为主键。
5. 城市发生变化时追加时间线；连续上报相同城市不需要重复追加明细，但应更新 Current 的 `observed_at`。
6. 用户搬家后即使新旧锚点都叫 home，也必须能通过不同 `anchor_id` 区分。

### 5.3 行为、应用与媒体

| 状态 | Signal | 标准字段 | Current TTL | 明细/聚合 | 保留规则 | 主要用途 |
|---|---|---|---:|---|---|---|
| 已确认 | `focus_state` | `is_active: bool`, `occurred_at` | 5 分钟 | 状态变化；每日总时长、段数、最长段 | 明细与聚合永久 | 当前是否专注、长期专注规律 |
| 已确认 | `motion_state` | `state: stationary/walking/running/cycling/automotive/unknown`, `confidence?: number`, `occurred_at` | 5 分钟 | 状态变化；每日各状态时长与切换次数 | 明细与聚合永久 | 当前活动状态、长期活动规律 |
| 已确认 | `app_usage` | `app_id`, `app_name`, `category?: string`, `action: open/close`, `occurred_at`, `source_event_id` | 15 分钟 | open/close、session、每日时长/次数 | 全量永久 | 最近 App、使用轨迹和长期趋势 |
| 已确认 | `music_playback` | `track_id`, `title`, `artist`, `album?: string`, `playback_state: playing/paused/stopped`, `position_seconds?: number`, `occurred_at`, `source_event_id` | 10 分钟 | transition、session、每日 artist/track/时长 | 全量永久 | 当前音乐、播放历史和偏好趋势 |
| 已确认 | `photo_library_added` | `count: 1`, `added_at`, `source_event_id` | 最近一次新增 | 每日 `added_count` | 单条明细建议 7 天；每日数量永久 | 知道何时新增照片、每天新增多少 |

行为类规则：

- Focus/Motion 只在状态改变时追加 transition；重复的同状态上报仅刷新 Current，不制造无意义明细。
- App session 是从 open/close 派生的。缺失 close 时必须标为 incomplete/open-ended，不能编造结束时间。
- Music 若只有轮询样本、没有可靠 play/pause edge，派生 session 必须带 `quality=estimated`；不能把采样间隔无条件当成播放时长。
- `photo_library_added` 一张照片一条 `count=1`。批量上报时可以使用稳定 batch ID + ordinal 生成每张的幂等 ID。
- 照片后来删除不回减过去某日的 `added_count`。
- 不要求长期保存照片 ID；可永久保存不可逆/不透明的 dedupe identity，以防旧批次重放后重复计数。
- 照片实际内容和 Photo Attributes 是另一项可选能力，不属于 `photo_library_added` 的必需字段。

### 5.4 Calendar 与 Reminder 来源镜像

| 状态 | 逻辑对象 | 标准字段 | Current 语义 | 历史语义 | 保留规则 |
|---|---|---|---|---|---|
| 已确认 | `calendar_event` | `source_calendar_id`, `source_event_id`, `title`, `start_at`, `end_at`, `is_all_day`, `time_zone_id`, `location_text?`, `notes?`, `url?`, `attendees?`, `recurrence_rule?`, `recurrence_series_id?`, `status?`, `source_created_at?`, `source_updated_at?` | 当前来源中仍存在、且在已同步覆盖范围内的 Event 集合 | 不保存每次同步快照；Event 自身可位于过去或未来 | 跟随来源；来源删除则本地彻底删除 |
| 已确认 | `reminder_item` | `source_list_id`, `source_reminder_id`, `title`, `notes?`, `due_at?`, `time_zone_id?`, `priority?`, `is_completed`, `completed_at?`, `recurrence_rule?`, `source_created_at?`, `source_updated_at?` | 当前来源中仍存在的 Reminder 集合 | 不保存每次同步快照 | 跟随来源；来源删除则本地彻底删除 |

同步规则：

1. 初次接入尽可能分段回填来源允许读取的完整历史与未来。
2. 无限重复日程保存 recurrence rule/series identity，不要求一次展开到无限未来。
3. 日常通过来源变更通知、App 启动、周期任务或增量 cursor 刷新。
4. 每次同步必须说明是 full snapshot 还是 incremental delta，以及覆盖范围。
5. Full snapshot 只能删除其明确覆盖范围内已经消失的项目，不能用局部窗口误删窗口外数据。
6. 来源删除后本地彻底删除，不保留用户可查询的 tombstone；为正确同步可在内部短期记录删除处理结果，但不得当成 Calendar 历史展示。
7. `last_successful_sync_at` 必须可查询。同步长期失败时，应显示 stale，而不能继续声称日历是最新完整数据。
8. Calendar/Reminder 内容是否进入 Agent context 应通过字段投影和权限控制；“本地完整镜像”不等于“每次全部塞进模型上下文”。

### 5.5 Health 与长期趋势

所有 Health 标准样本都应包含以下公共 metadata：

```text
source_sample_id
occurred_at 或 start_at/end_at
source_name/source_type
source_revision（若来源支持）
unit
availability
```

| 状态 | Signal | 标准业务字段 | 推荐聚合 | 保留规则 |
|---|---|---|---|---|
| 已确认 | `steps` | `step_count: integer>=0`, `local_date`；若是区间增量则带 `start_at/end_at` | 每日本地日总数；阈值 crossing state | 标准样本/贡献记录与每日总数永久 |
| 已确认 | `health_sleep` | `stage: awake/core/deep/rem/asleep/unknown`, `start_at`, `end_at`, `duration_minutes` | 每日各阶段分钟、总睡眠、session | 样本与聚合永久 |
| 已确认 | `health_workout` | `workout_type`, `start_at`, `end_at`, `duration_minutes`, `active_energy_kcal?`, `distance_m?` | 每日次数、总时长、类型分布 | 样本与聚合永久 |
| 已确认 | `health_vitals` | `metric: resting_heart_rate/current_heart_rate/hrv_sdnn/respiratory_rate/oxygen_saturation/vo2_max`, `value`, `unit` | 每日 min/max/avg/count；按 metric 分开 | 样本与聚合永久 |
| 已确认 | `health_activity` | `active_energy_kcal`, `exercise_minutes`, `stand_minutes`, `mindful_minutes`, `local_date` | 每日累计最大值/最终值 | 样本与聚合永久 |
| 已确认 | `health_body` | `weight_kg?`, `bmi?`, `body_fat_ratio?`, `height_cm?` | 每日/最近测量、长期漂移 | 样本与聚合永久 |
| 已确认 | `health_metabolic` | `blood_glucose_mmol_l?`, `blood_pressure_systolic_mmhg?`, `blood_pressure_diastolic_mmhg?` | 每日分布、长期趋势 | 样本与聚合永久 |
| 已确认 | `health_cycle` | `flow_level?`, `is_active_period?`, `start_at/end_at?` | 周期/每日状态 | 样本与聚合永久 |
| 已确认 | `health_mood` | `valence`, `valence_classification?`, `kind?`, `labels[]?`, `recorded_at` | 每日 entries、趋势 | 样本与聚合永久 |

Health 工程规则：

- `0` 不能因为是假值而丢失；它可能是合法测量。
- 不同单位必须在进入 canonical Observation 时统一，原始单位可作为 metadata 保留。
- 每个来源样本必须幂等；不能只用“用户 + 日期 + metric”覆盖所有样本。
- 聚合不能作为唯一事实来源，否则修订、删除或算法升级后无法可靠重算。
- `建议/待确认` 来源侧样本修订或删除时，应更新/删除对应 canonical sample，并重算受影响日期；这是保证 Health 准确性的推荐方案。
- “永久保存”仍受用户删除账号、主动清空和权限撤销策略约束，不代表不可删除。

---

## 6. Retention、Freshness 和来源删除总表

| 数据 | Current | 明细 | 聚合/镜像 | 来源删除后的处理 |
|---|---|---|---|---|
| Time | 实时派生 | 只存时区变化 | 时区变化永久 | 不适用 |
| Battery + Low Power Mode | 10 分钟 freshness | 不存 | 不存 | 不适用 |
| Unlock | 最近一次 | 不存长期 list | 不存 | 不适用 |
| Screen Change | 最近一次 | 不存 | 不存 | 不适用 |
| Broadcast | 5 分钟 freshness | 7 天 | 不要求永久 | 到期清理 |
| Weather | 30 分钟 freshness | 7 天 | 不要求永久 | 到期清理 |
| Audio Route | 10 分钟 freshness | 7 天 | 不要求永久 | 到期清理 |
| Network | 5 分钟 freshness | 建议 7 天 | 不要求永久 | 到期清理 |
| Wi-Fi/蓝牙 Anchor | 5 分钟 freshness | 7 天 | session 同明细 7 天 | 到期清理 |
| Location City | 15 分钟 freshness | 永久 | dwell/迁移聚合永久 | 来源修正时按同 identity 修订 |
| Focus | 5 分钟 freshness | 永久 | 永久 | 不适用 |
| Motion | 5 分钟 freshness | 永久 | 永久 | 不适用 |
| App | 15 分钟 freshness | 永久 | 永久 | 不因 App 卸载删除历史 |
| Music | 10 分钟 freshness | 永久 | 永久 | 不因曲库变化删除已发生历史 |
| Photo Library Added | 最近一次 | 建议 7 天 | 每日新增数量永久 | 删除照片不回减 |
| Calendar | `last_successful_sync_at` 表达 freshness | 不存上传版本 | 当前来源镜像 | 来源删除则本地删除 |
| Reminder | `last_successful_sync_at` 表达 freshness | 不存上传版本 | 当前来源镜像 | 来源删除则本地删除 |
| Health | 按 metric 定义 freshness | 永久 | 永久 | 建议同步修订/删除并重算 |

---

## 7. 宿主必须能够映射的逻辑存储对象

这些是逻辑对象，不要求一项对应一张 SQL 表。宿主可以合并物理表，但必须保持字段、查询和一致性语义。

### 7.1 IngestReceipt

用途：Report 批级幂等和排查。

```text
subject_id
producer
report_id
payload_digest
received_at
status
error_code
```

唯一身份：`(subject_id, producer, report_id)`。

相同 identity + 相同 digest 返回原结果；相同 identity + 不同 digest 必须报 conflict，不能静默覆盖。

### 7.2 Observation

用途：标准化事实、时间线、重算来源。

```text
observation_id
subject_id
signal
signal_schema_version
source
source_event_id
source_revision
occurred_at
received_at
effective_local_date
timezone
availability
typed_value
created_at
```

推荐唯一身份：`(subject_id, source, signal, source_event_id)`；没有来源 ID 的纯状态型数据必须由 manifest 定义 deterministic identity strategy。

### 7.3 CurrentProjection

用途：快速回答“现在/最近是什么”。

```text
subject_id
signal
dimension_key
typed_value
availability
observed_at
received_at
expires_at
source_observation_id
version
```

唯一身份：`(subject_id, signal, dimension_key)`。

迟到 Observation 可以进入历史，但只有 `occurred_at` 更新的数据才能覆盖 Current；同一时间不同内容必须进入 conflict/reconciliation 路径。

### 7.4 DailyAggregate

用途：趋势、统计和快速查询。

```text
subject_id
signal
local_date
timezone_attribution
aggregation_kind
aggregation_version
typed_aggregate
source_coverage
updated_at
```

唯一身份：`(subject_id, signal, local_date, aggregation_kind, aggregation_version)`。

聚合必须可从 Observation/来源样本重算，或保存等价的贡献账本。算法升级通过 `aggregation_version` 重建，不能原地悄悄改变旧统计语义。

### 7.5 CalendarEventMirror

```text
subject_id
source_account_id
source_calendar_id
source_event_id
recurrence_identity
source_revision
event_fields
source_created_at
source_updated_at
last_seen_sync_id
updated_at
```

唯一身份必须包含来源账户/日历范围，避免不同账户碰巧使用相同 event id。

### 7.6 ReminderItemMirror

```text
subject_id
source_account_id
source_list_id
source_reminder_id
source_revision
reminder_fields
last_seen_sync_id
updated_at
```

### 7.7 SourceSyncState

```text
subject_id
source
collection_kind
sync_cursor
coverage_start
coverage_end
snapshot_kind: full/incremental
last_attempted_at
last_successful_sync_at
last_error_code
```

### 7.8 DurableDedupeIdentity

用途：原始明细已经按 retention 清理，但其永久聚合仍不能被旧数据重放重复累计。

```text
subject_id
signal
source
source_event_identity_digest
first_applied_at
aggregate_scope
```

Photo Added 是典型场景：照片 ID 不需要永久保存，但一个不透明的 dedupe digest 可以永久或至少覆盖 producer 的最大重放期保存。

### 7.9 EventDefinition

```text
definition_id
subject_id/scope
version
enabled
signal
field
condition
lifecycle
event_type
wake_policy
created_at
updated_at
```

### 7.10 EventRuleState

```text
definition_id
subject_id
scope_key
previous_value
firing_state
last_fired_at
rearm_state
version
```

### 7.11 EventOutbox

```text
event_id
definition_id
definition_version
subject_id
event_type
occurred_at
detected_at
fact_snapshot
dedupe_key
delivery_state
attempt_count
next_attempt_at
created_at
```

### 7.12 WakeReceipt

```text
event_id
attempt_id
status: accepted/conversation_suppressed/enqueue_failed/duplicate
runtime_ref
reason
received_at
```

Event 必须先进入 durable outbox，再调用 WakePort。只有 runtime 真正 accepted 后，才提交“已经 wake”的冷却和额度状态。

### 7.13 Signal 到逻辑存储对象的标准映射

下表回答“每类数据最终进入哪类表”。`短期 Observation` 表示宿主可以在到期后清理明细；`临时处理` 表示不要求落入长期 Observation 表。

| Signal / 对象 | Observation/Timeline | CurrentProjection | DailyAggregate | Source Mirror/Sync | Durable Dedupe | Event 相关 |
|---|---|---|---|---|---|---|
| `time_context` | 时区改变时永久追加 | 是 | 否 | 否 | report 级 | 可选 changed |
| `battery` | 临时处理 | 是 | 否 | 否 | report 级 | 可用于 threshold/change |
| `broadcast` | 短期 Observation，7 天 | 是 | 否 | 否 | source event/report | 可用于 enters/leaves |
| `screen_change` | 临时处理 | 是 | 否 | 否 | source event/report | 可用于 occurrence |
| `device_unlock` | 不保存长期 list | 是 | 否 | 否 | source event id | occurrence + RuleState + Outbox |
| `audio_route` | 短期 Observation，7 天 | 是 | 非必需 | 否 | source event/report | 可选 changed |
| `network_connection` | 短期 Observation，建议 7 天 | 是 | 否 | 否 | source event/report | 可选 enters/leaves |
| `weather` | 短期 Observation，7 天 | 是 | 非必需 | 否 | report/valid_at | 可选 threshold/alert occurrence |
| `location_city` | 永久变化 Timeline | 是 | 可选 dwell/迁移，永久 | 否 | source event或确定性 identity | changed/enters/leaves |
| `proximity_anchor` | 短期 Observation，7 天 | 每个 anchor 一条 Current | session 可选，随明细 7 天 | 否 | anchor + source event | enters/leaves |
| `focus_state` | 永久 transition | 是 | 永久 | 否 | source event/确定性 transition | changed/enters/leaves/duration |
| `motion_state` | 永久 transition | 是 | 永久 | 否 | source event/确定性 transition | changed/enters/leaves/duration |
| `app_usage` | 永久 open/close Timeline | 是 | 永久 | 否 | source event id | occurrence/duration/custom |
| `music_playback` | 永久 transition/session | 是 | 永久 | 否 | source event id | occurrence/changed/custom |
| `photo_library_added` | 单条建议 7 天 | 是 | 每日 count 永久 | 否 | 必须覆盖最大重放期 | occurrence/threshold/custom |
| `calendar_event` | 不存每次上传 Timeline | 当前集合查询由 Mirror 提供 | 可按需派生，不作 canonical 必需项 | CalendarEventMirror + SourceSyncState | source event + revision | occurrence/time-window/custom |
| `reminder_item` | 不存每次上传 Timeline | 当前集合查询由 Mirror 提供 | 可按需派生，不作 canonical 必需项 | ReminderItemMirror + SourceSyncState | source item + revision | due/completed/custom |
| `steps` | 永久标准样本/贡献记录 | 是 | 每日 total 永久 | 否 | source sample id | threshold_crossing/streak |
| 其他 Health | 永久标准样本 | 按 metric 提供 last reliable/current | 永久 | Health sync state（来源支持时） | source sample id + revision | threshold/delta/streak/custom |

所有进入 Event 的信号都共用 EventDefinition、EventRuleState、EventOutbox 和 WakeReceipt，不应为照片、解锁、步数分别创建互不兼容的 wake 表。

---

## 8. StoragePort 的最低行为契约

StoragePort 不需要暴露 SQL，但至少应支持等价操作：

```python
class StoragePort(Protocol):
    def claim_report(...): ...
    def append_observation(...): ...
    def get_current(...): ...
    def compare_and_put_current(...): ...
    def list_observations(...): ...
    def get_aggregate(...): ...
    def put_aggregate(...): ...
    def upsert_calendar_events(...): ...
    def upsert_reminders(...): ...
    def apply_source_snapshot(...): ...
    def get_sync_state(...): ...
    def put_sync_state(...): ...
    def get_rule_state(...): ...
    def put_rule_state(...): ...
    def enqueue_event(...): ...
    def claim_pending_event(...): ...
    def record_wake_receipt(...): ...
```

必须声明并通过 conformance tests 证明：

1. Report 和 Observation 幂等。
2. 旧 Observation 不覆盖新 Current。
3. 同时间、同 identity、不同内容不会静默覆盖。
4. 永久聚合不会因重放重复累计。
5. Observation、Current、Aggregate、Rule State 和 Event Outbox 的必要变更处于同一原子边界，或有可证明的恢复/reconciliation 机制。
6. Event 在 dispatch 前已经 durable。
7. Event delivery 按 `event_id` 幂等。
8. Calendar/Reminder 局部同步不会误删覆盖范围外项目。
9. Retention cleanup 不会误删永久聚合所需的唯一事实或 dedupe 身份。
10. 用户数据可以按 subject 定位、导出和删除。

---

## 9. 一条上报进入系统后的标准处理顺序

```text
Adapter 已完成认证 / 解密 / 传输校验
        ↓
校验 Report schema_version、report_id
        ↓
按 signal manifest 校验字段、类型、单位、枚举
        ↓
标准化为 Canonical Observation
        ↓
按 report_id + source_event_id 幂等判断
        ↓
写 Observation 或更新 Source Mirror
        ↓
仅在 occurred_at 更新时更新 Current
        ↓
更新/重算 Timeline 与 DailyAggregate
        ↓
读取 EventDefinition + EventRuleState 求值
        ↓
命中时原子写入 RuleState + PerceptionEvent Outbox
        ↓
提交事务
        ↓
调用 WakePort
        ↓
保存 WakeReceipt；accepted 后提交 wake 冷却/额度
```

上报成功不等于 Event 命中；Event 命中不等于 Runtime 接受；Runtime 接受也不等于 Agent 必须给用户发消息。

---

## 10. EventDefinition 最小模板与示例

### 10.1 步数跨过 3,000

```yaml
id: daily_steps_3000
version: 1
enabled: true
source:
  signal: steps
  field: step_count
condition:
  type: threshold_crossing
  operator: gte
  value: 3000
lifecycle:
  scope: local_day
  fire: once
  rearm: next_scope
event:
  type: activity.step_goal_reached
wake:
  enabled: true
  cooldown_seconds: 0
```

正确语义：`previous < 3000 and current >= 3000`。不能只判断 `current >= 3000`，否则 3,001、3,010、3,100 会重复触发。

### 10.2 锁屏后解锁

```yaml
id: device_unlocked
version: 1
enabled: true
source:
  signal: device_unlock
condition:
  type: occurrence
deduplication:
  key: source_event_id
event:
  type: device.unlocked
wake:
  enabled: true
  cooldown_seconds: 0
```

这里不得改写为 device boot/restart。是否实际 wake、是否开口，由 Runtime 再决定。

### 10.3 建议内置的规则类型

```text
changed
equals
enters
leaves
threshold_crossing
delta
occurrence
streak
absence
```

不要求一开始实现无限表达式 DSL。优先提供有限、清楚、可持久化和可测试的模板，并允许可信 Runtime 注册 typed custom evaluator。

---

## 11. 查询与 Agent 使用规范

PerceptKit 至少应提供逻辑上等价的查询：

```text
get_current(subject, signals)
get_last_known(subject, signal)
list_timeline(subject, signal, from, to, cursor, limit)
get_daily_aggregates(subject, signal, from_date, to_date)
get_trend(subject, signal, field, window)
list_calendar_events(subject, from, to, cursor, limit)
list_reminders(subject, filter, cursor, limit)
list_events(subject, type, from, to, cursor, limit)
```

使用原则：

- Current 用于低成本上下文，但 stale 值必须明确带 `as_of` 或返回 null。
- 大量历史、Calendar、Reminder、Health 明细应按需查询，不应全部塞进每次模型上下文。
- Agent-facing projection 与 canonical storage 分开；存了某字段不代表 Agent 永远能直接看到。
- 权限关闭后，读取和 wake 必须停止；宿主按产品策略处理既有历史。
- 所有 list/history 查询必须分页、有时间范围和上限。

---

## 12. 必须覆盖的边界情况

| 边界情况 | 规定行为 |
|---|---|
| 同一 report 重试 | 返回相同结果，不重复处理 |
| 同一 Observation 重试 | 不重复追加、不重复聚合、不重复 Event |
| 相同 identity 内容不同 | 标记 conflict；只有支持 revision 的来源按 revision 规则处理 |
| 离线补传旧数据 | 可进入历史；不得覆盖更晚 Current |
| 到达顺序与发生顺序不同 | 一律以 `occurred_at` 做事实顺序，以 `received_at` 做审计 |
| 手机跨时区 | 旧记录保持发生时 local date；不因当前时区变化重排历史 |
| 夏令时切换 | 使用 IANA timezone + timestamp，不只保存 UTC offset |
| 跨午夜 session | 按发生时本地日切分聚合；原 session identity 不变 |
| Producer 时钟明显错误 | 标记质量问题/拒绝，不可静默污染 Current |
| Current 过期 | 返回 stale/last_known 语义，不冒充 current |
| `no_data` | 不当成 0，不参与趋势和 streak |
| `unavailable` | 不覆盖最后可靠数值；查询时可以表达当前不可用 |
| Focus/Motion 重复同状态 | 刷新 Current，不追加重复 transition |
| App 只有 open 没有 close | session 标记 incomplete，不猜结束时间 |
| Music 长时间没有采样 | 不把整段间隔无条件算为播放；标记 estimated 或截断 |
| 步数当日回退/重置 | 不能产生负增量；按来源 identity 与累计语义重算 |
| Photo report 重试 | 使用稳定 identity；每日 count 不重复增加 |
| Photo 后来删除 | 不回减历史 added_count |
| Calendar 局部窗口同步 | 只修改明确 coverage 内数据 |
| Calendar 无限重复事件 | 保存 rule/series，并滚动展开查询窗口 |
| Calendar/Reminder 来源删除 | 本地镜像删除；不保留用户可查版本历史 |
| Anchor label 改名 | 更新 label，不改变 anchor identity；历史仍可归属同一 anchor |
| 用户搬家、新旧都叫 home | 使用不同 anchor_id，不按 label 合并 |
| Retention 到期 | 清理明细，但不破坏永久聚合和幂等正确性 |
| 聚合算法升级 | 使用新 aggregation_version 重算，不静默改写语义 |
| Runtime 在 wake 前后崩溃 | Event 仍在 outbox，可幂等重试并记录未知结果 |
| 用户删除账号 | 删除 Current、Observation、Aggregate、Mirror、Rule、Event、Receipt 及宿主保存的相关对象 |

---

## 13. 当前 PerceptKit 与本文目标的已知差异

以下基于 2026-08-27 本地核验的独立 PerceptKit 工作树，仅用于帮助排期：

| 项目 | 当前代码 | 本文目标 |
|---|---|---|
| Catalog | 21 个 Capability、20 个 Signal；字段类型/单位/retention 未统一在一个 manifest | 完整 typed manifest，统一 current/history/identity/event 规则 |
| Battery | `battery_level`, `charging` | 增加 `is_low_power_mode_enabled`，只留 Current |
| Location | `place_label`, `wifi_label`, `country`, `locality`, `wifi_anchor_id` 混在 `location_signal` | 城市 Location 与 proximity anchor 分开；城市历史永久 |
| Unlock | 不在普通 Signal catalog | `device_unlock` 普通信号；只留 Current；无 boot/restart |
| Photo Added | Photos capability 存在，但不在普通 Signal catalog | `photo_library_added` 普通信号；每日数量永久 |
| Screen Change | wake/device 旁路概念 | 普通信号，只留 Current |
| Focus/Motion | 当前 retention 均为 90 天 | 明细和聚合永久 |
| Music | 当前 retention 为 365 天，daily tally 不是完整 session | 明细/session/聚合永久 |
| Audio Route | 当前 retention 为 90 天 | 7 天 |
| Weather | 当前 retention 为 90 天 | 7 天 |
| Calendar/Reminder | 当前按 daily event list 聚合，分别声明 60 天 | 改为来源镜像，不保存上传快照历史 |
| App | Current catalog 有字段，通用 history/retention 未声明 | open/close、session、聚合永久 |
| Storage | 没有 StoragePort、reference schema 或实际表 | 定义本文逻辑对象和 conformance contract |
| Event | 固定 wake 判断，没有通用 EventDefinition/Outbox | 可插拔规则、稳定 Event、durable outbox、WakeReceipt |
| Retention 执行 | `retention.py` 只声明，不负责清理 | 宿主 adapter 执行并通过测试证明 |

不能只修改 `retention.py` 的数字就宣称完成。Calendar 镜像、App/Music 明细、标准 Unlock/Photo Signal、持久化接口和 Event 链路都需要实际实现与测试。

---

## 14. 当前仍需产品或 iOS 侧确认的少量事项

以下问题不应阻塞字段/存储接口先定型，但必须保留为显式决策：

1. `network_connection` 是否与 Wi-Fi/蓝牙一样保留 7 天，还是只留 Current。本文暂按 7 天处理。
2. `photo_library_added` 的单条 Observation 本文建议保留 7 天；每日数量永久。若 producer 可以在超过 7 天后重放，必须永久保留 dedupe digest 或明确最大重放期。
3. Photo Content / Photo Attributes 是否作为独立可选能力，以及图片内容、属性、索引和删除如何同步。本次不纳入 Photo Library Added。
4. Health 来源样本被删除或修订时，本文建议同步修正 canonical sample 并重算聚合；需要最终产品确认删除语义。
5. iOS 对 Wi-Fi/蓝牙 anchor、解锁时刻、锁屏时长、App close 和 Music transition 实际能稳定提供到什么程度，需要 producer capability matrix 和真机验证；不能仅靠字段设计假设系统一定能采集。
6. 各 Current TTL 目前沿用现有代码或讨论中的初始值，应通过真实上报频率验证后版本化调整。

---

## 15. 工程交付验收清单

交付不能只给设计文档。最低需要：

- [ ] 一份单一来源的 typed signal manifest，覆盖本文全部信号和字段。
- [ ] Report/Observation/Event 的版本化 schema。
- [ ] StoragePort 与 WakePort 接口。
- [ ] Reference storage mapping，明确本文逻辑对象如何映射到至少一种示例存储。
- [ ] 从 Report → Observation → Current/History/Mirror → Event → WakeReceipt 的端到端示例。
- [ ] 步数 3,000/5,000 crossing 和 Unlock occurrence 的 EventDefinition 示例。
- [ ] Calendar/Reminder full + incremental + coverage + source delete 测试。
- [ ] Report/Observation 重试幂等测试。
- [ ] 迟到数据不覆盖 Current 测试。
- [ ] Photo Added 重试不重复计数、删除不回减测试。
- [ ] Focus/Motion transition 和跨午夜 duration 测试。
- [ ] App 缺失 close、Music 采样间断测试。
- [ ] 聚合重算与 aggregation_version 测试。
- [ ] Retention cleanup 不破坏永久聚合/幂等测试。
- [ ] Event durable-before-wake、崩溃重试、duplicate receipt 测试。
- [ ] Current/History/Calendar/Reminder 查询分页和权限测试。
- [ ] 不依赖 Feedling V1/V2 的最小接入示例。

工程师在开始实现前，应先返回一份 mapping：

```text
本文每个 Signal
→ iOS 当前是否能采集
→ 当前实际上报字段
→ 目标 manifest 字段
→ 写入哪类逻辑对象
→ Current TTL
→ History/Aggregate/Mirror 规则
→ Retention
→ Event eligibility
→ 当前实现状态与负责人
```

若某字段无法采集、某一致性保证无法实现，或当前代码与本文冲突，应显式列出并讨论，不要静默删除字段、缩短历史或继续依赖宿主旁路。

---

## 16. 本文的最终目标

新的 Runtime 接入 PerceptKit 时，不需要阅读旧 Feedling 的路由、表、V1/V2 worker 或照片 enclave 才能猜出数据如何使用。

接入方只需要：

1. 把来源数据适配成本文定义的 Report/Observation；
2. 用自己的数据库实现本文的逻辑 StoragePort；
3. 用 EventDefinition 组织“什么情况算一个 Event”；
4. 用 WakePort 把标准 PerceptionEvent 交给自己的 Agent Runtime。

PerceptKit 应当拥有从“标准上报事实”到“标准 Event”的通用语义；具体采集、具体数据库、加解密和 Runtime 内部实现仍由宿主负责。
