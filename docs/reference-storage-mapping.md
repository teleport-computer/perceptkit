# Reference storage mapping

> 由 `perceptkit.manifest.render_reference_mapping()` 从 manifest 生成。
> **不要手改** —— 改 manifest，然后重新生成。

`Current TTL` 到期之后这个值不再冒充「现在」，但仍可作为 last known 返回。
明细和聚合是**两个保留期**：典型形态是明细短、聚合永久 ——明细体量大而问题的价值随时间递减，聚合正好相反。

| 信号 | 落到哪些对象 | Current TTL | 明细 | 聚合 | 身份 | 日期归属 |
|---|---|---:|---:|---:|---|---|
| `audio_route` | CurrentProjection + StoredObservation | 600s | 7 天 | 同明细 | deterministic_digest | split_at_midnight |
| `battery` | CurrentProjection | 600s | 不存 | 不适用 | singleton | instant |
| `broadcast` | CurrentProjection + StoredObservation | 300s | 7 天 | 同明细 | deterministic_digest | split_at_midnight |
| `focus_state` | CurrentProjection + StoredObservation + DailyAggregate | 900s | 365 天 | 永久 | deterministic_digest | split_at_midnight |
| `health_activity` | CurrentProjection + StoredObservation + DailyAggregate | 3600s | 永久 | 同明细 | source_event_id | source_local_date |
| `health_body` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | instant |
| `health_cycle` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | instant |
| `health_metabolic` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | instant |
| `health_mood` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | instant |
| `health_sleep` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | episode_end |
| `health_vitals` | CurrentProjection + StoredObservation + DailyAggregate | 3600s | 永久 | 同明细 | source_event_id | instant |
| `health_workout` | CurrentProjection + StoredObservation + DailyAggregate | 86400s | 永久 | 同明细 | source_event_id | episode_end |
| `location_city` | CurrentProjection + StoredObservation + DailyAggregate | 900s | 永久 | 同明细 | deterministic_digest | instant |
| `motion_state` | CurrentProjection + StoredObservation + DailyAggregate | 900s | 365 天 | 永久 | deterministic_digest | split_at_midnight |
| `music_playback` | CurrentProjection + StoredObservation + DailyAggregate | 600s | 365 天 | 永久 | deterministic_digest | split_at_midnight |
| `photo_library_added` | CurrentProjection + StoredObservation + DailyAggregate | — | 7 天 | 同明细 | source_event_id | instant |
| `presence_recovery` | CurrentProjection | — | 不存 | 不适用 | source_event_id | instant |
| `proximity_anchor` | CurrentProjection + StoredObservation + DailyAggregate | 900s | 7 天 | 永久 | deterministic_digest | split_at_midnight |
| `screen_change` | CurrentProjection | 60s | 不存 | 不适用 | singleton | instant |
| `steps` | CurrentProjection + StoredObservation + DailyAggregate | 3600s | 永久 | 同明细 | source_event_id | source_local_date |
| `time_context` | CurrentProjection + StoredObservation + DailyAggregate | — | 永久 | 同明细 | deterministic_digest | instant |
| `weather` | CurrentProjection + StoredObservation | 1800s | 7 天 | 同明细 | deterministic_digest | instant |

## 和产品规范有出入的地方

### `audio_route`

当场景线索用：连车机大概率在开车、戴 AirPods 可能在通勤或想专注。它也是蓝牙锚点唯一能拿到的残片 —— iOS 不给第三方 app 看系统级蓝牙连接，只有音频输出设备这一个子集。

### `broadcast`

和 screen_change 是两件事：这个表示【采集会话开着没有】（一天开关几次），screen_change 表示【画面变了没有】（采集期间每几秒可能一次）。

### `focus_state`

替掉产品规范阶段二里的 proximity_anchor：那个信号的 bluetooth 类型 iOS 给不了（只有音频路由这个子集），enter/leave 边缘也没有。focus_state 覆盖同一种存储形态，且采集能力确凿。见 OPEN-QUESTIONS B14/B17。

### `health_activity`

和 steps 同一种形态：日内单调累加，当天代表值取【最大值】不是求和。取最大值天然不怕跨天回退 —— 00:01 的新一天读数会归到新的一天，不会和昨天的数字相减产生负增量。

### `health_body`

🔴 这一组是「用户改数据」最常发生的地方（体重录错、手动补录）——修订机制主要为它们服务。也是单位最容易标错的一组（kg / lb），所以 max_relative_jump 卡得比别的紧：70 kg 被标成 lb，换算完 31.8 kg 值域完全合法，只有「一次掉 55%」能看出不对。

### `health_cycle`

周期型：看【间隔】不看数值高低 —— 「比平均晚了 4 天」才是信号。

### `health_metabolic`

血糖本身波动就大（餐前餐后能差一倍），所以不设跳变阈值 —— 设了会天天误报。血压相对稳定，设一个宽的。

### `health_mood`

用户自己记的，一天可能好几条 —— 所以是 event_list 不是取当天某一个值。

### `health_vitals`

⚠️ 建模方式和规范不同。规范用 metric + value + unit（一条观测一个指标），那需要「同一信号下多条并列当前值」的支持 —— 这个能力我们还没有（已记为已知缺口）。这里先按【每个指标一个字段】建模，和宿主现状一致，今天就能跑。等多维当前值做出来再切回规范的形态。

### `location_city`

只有城市级。精细位置（home / work / 某个房间）是另一个信号（proximity_anchor），不能混进同一个字段 —— 两个时期都叫 home 就看不出搬过家。

### `motion_state`

保留期偏离规范：明细 1 年（规范给「永久」），聚合仍然永久。TTL 也偏离（规范 300s → 900s）：后台保活上报间隔正好是 300s，TTL 等于上报间隔意味着用户不在前台时这个值几乎永远是 stale。另外：同一状态重复上报只刷新当前值、不写明细 —— 否则用户开着专注模式工作四小时会在历史里留下 48 条一模一样的记录。

### `music_playback`

两处和产品规范不同，都是 iOS 平台限制：
① **没有 track_id**。Apple 是给歌曲持久化 ID 的，但 iOS 侧当初为隐私主动砍掉了 —— 那个 ID 能反查用户整个曲库。我们用 (title, artist) 的哈希当稳定身份，代价是同名同歌手的两首（现场版 / 录音室版）会被当成同一首。
② **播放边缘只覆盖一半播放器**。iOS 订阅的是 systemMusicPlayer，也就是 Apple Music / 系统播放器：切歌 2 秒后就上报，起止时刻是准的。Spotify、网易云用自己的播放器，不发这些通知，只能靠快照采到的那几个点。所以派生 session 的 quality **不能一刀切成 estimated** —— 规范 §5.3 写的是「只有轮询样本就标 estimated」，实际是同一个信号两种精度并存，一律标 estimated 会把本来准确的那一半信息丢掉。

### `photo_library_added`

一张照片一条 count=1，不是「今天 5 张」报一次 —— 拆成一条条才能让照片走通用管线：跨午夜天然各归各的日、某条字段有问题只拒那一条。传输上仍然可以一个信封装多条，不多发请求。
🔴 删照片【不回减】过去某日的数量：它记的是「那天发生过什么」，不是「现在还剩几张」。
🔴 前提未满足：iOS 现在上报的是随机 id，同一张照片两次上报算出两个不同身份 —— 去重表再完美也挡不住。要 iOS 改成用 PHAsset.localIdentifier 的端上哈希（和 wifi_anchor_id 同一套做法）。已列入 iOS 待办。
去重指纹的保留期：规范建议永久；我们查下来当前实现【找不到超过 7 天的重放路径】，所以按「覆盖明细保留期 + 富余」取 30 天更实在。规范自己也是条件句：「若 producer 可以在超过 7 天后重放，才必须永久保留」。

### `presence_recovery`

产品规范叫 device_unlock，这里改名 presence_recovery —— iOS 拿不到硬件解锁事件（precise_unlock 恒为 null），能给的只是「app 自己进后台到回前台的间隔」。沿用 unlock 这个名字会让模型解释成「用户刚解锁手机」，和产品方自己撤回 device_boot 是同一类错误。见 OPEN-QUESTIONS B10。

### `proximity_anchor`

和 location_city 是【两个信号，不是一个字段的粗细两档】。城市回答「在哪座城」，锚点回答「在哪个地方」—— 混进同一个字段，搬家之后新旧两个「home」就看不出区别了（产品规范 §5.2-6 点名的场景）。
两处和规范不一致，都是 iOS 平台限制：
① anchor_type 的 bluetooth 这一档基本拿不到 —— iOS 不给第三方看系统级蓝牙连接，只有音频输出设备这一个子集，那部分走 audio_route。
② connect/disconnect 边缘取决于「app 被后台唤起时还读不读得到 Wi-Fi」，这一条正在真机实测。读不到的话 dwell 只能从相邻快照推，精度 = 上报间隔，且用户全程在后台的那段会整块漏掉。

### `screen_change`

🔴 只存「变了 / 没变」这个布尔。**画面指纹不进这个信号，也不落库** —— 指纹序列存久了，理论上能反推用户屏幕上出现过什么。比较放在设备端做（设备本地记着上一次的指纹，只上报布尔），iOS 其他信号的 changed 标志已经是这套机制，screen 这条跟上即可。

### `time_context`

只记录时区【变化】，本地时间不存历史 —— 有时区 + UTC 时刻就能算出来。TTL 改成不失效，原因见 current_ttl_sec 上面。

### `weather`

天气是【预报】不是测量 —— valid_at 说明这份数据什么时候有效，和「我们什么时候收到的」是两件事。

