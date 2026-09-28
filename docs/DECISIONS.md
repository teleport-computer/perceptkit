# 产品决定：谁、什么时候、定了什么

外部复核明确要求过：**「改注释不等于批准」**。所以拍过板的产品决定记在这里，
带日期和拍板人；发版前逐条核对「进去了没有」。

会红的东西优先 —— 能写成测试的一律写成测试，这张表只负责让漏掉变得**看得见**。

| 日期 | 拍板人 | 决定 | 现在落在哪 |
|---|---|---|---|
| 2026-09-17 | hx | 身高、实时心率**只存当前值**，不留历史 | manifest `storage_mode="current_only"`；本表即批准记录 |
| 2026-09-17 | hx | 用户删掉一条数据后，**引用它的提醒记录里的原值也抹掉**，只留「有过一条已被删除的数据触发过」；已经发出的聊天消息不动 | `tests/test_event_value_scrub.py`；需宿主实现可选端口 `scrub_event_snapshots` |
| 2026-09-17 | hx | 睡眠多来源重叠时**每晚只用一个来源**（当晚睡着时长最长的；差 15 分钟以内选带分期的），不跨来源相加 | 待做，在 iOS 采集侧 |
| 2026-09-17 | hx | `screen_change` 要做；屏幕指纹**在服务端从已上传的帧算**，不改 iOS | 待做，在宿主侧 |
| 2026-09-22 | hx | io 要做「用户自己编辑感知规则」 | 待出方案 |

## PerceptKit 一致性整改合同（2026-09-28）

**拍板人：Z。** 本节是 PerceptKit 对 D01–D14 的正式产品决定。它定义实现和
发布必须兑现的合同，不表示这些能力已经实现或发布。完整背景、验收矩阵和收口
证据见《PerceptKit 一致性协议与收口验收基线》（2026-09-28）。

### 共同边界

Observation、Revision、Retraction 是权威事实；Current、DailyAggregate、RuleState
是可重建投影；EventOutbox 只是可失效的效果意图（可记录投递状态及
`invalidated_at`/reason）；独立、持久化的 delivery receipt / WakeReceipt 才是不可抹除的
外部效果审计事实。Query/Export/Agent context 只读投影，不得反过来作为事实来源。一次
接受返回 `accepted`/`applied` 时，要求同步的投影必须和权威事实一起提交；否则只能留下
带稳定身份、可重试、可观察的 durable rebuild，且查询不得把未完成投影当成完整最新结果。

| ID | 最终决定 | 正常 / 失败与并发 | 迁移与用户可见结果 |
|---|---|---|---|
| D01 | **事实可以多条，Current 必须由 signal 声明语义。** 不同 Fact identity 即使同一时间、同一 signal 也是不同事实，进入 Timeline 和应有 Aggregate。`health_sleep` 不从原始睡眠片段强选单值 Current；当前读取为 Timeline + DailyAggregate，将来如需“最近一晚”另建 Sleep Episode。 | Current 语义不影响合法事实进入 Aggregate；禁止用数据库排序或单值 Current 冲突否定事实。 | 现有将睡眠片段当 Current 的读取须迁至 Timeline/Aggregate（或未来 Episode）；用户查询昨夜总睡眠不会得到一条原始片段冒充答案。 |
| D02 | **Report 不可变，最新版属于 Fact。** 同 `report_id` 且 canonical semantic payload 完全相同才是 duplicate；同 ID、不同语义是 report conflict。修订使用新 `report_id`、相同 `source_event_id`、更高 `source_revision`。 | 重传不再更新 Aggregate、RuleState 或生成新 Event；同一 Fact identity + 同 revision + 不同内容持久化 conflict，不推进依赖候选的投影。 | 旧 producer 的重传仍可重复；修订和 timezone 变化不再被误杀为 duplicate，旧批次中未携带的其他事实不会被误删。 |
| D03 | **Current CAS 重试耗尽时整次同步事实变更回滚并返回 retryable error。** 当前不以异步 rebuild 替代这个合同。 | Observation、identity、Aggregate、RuleState、Outbox 不得留下半成功；竞争耗尽后客户端可安全重试。 | Adapter 必须提供原子事务/等价保证；用户不会看到“事实收到了但当前值或统计没更新”的永久分叉。 |
| D04 | **`pending` 和尚未外发的 `claimed` Event 可进入独立 `invalidated` 终态。** worker 在外发前强制复核 trigger validity；请求已发但回执未知进入 `unknown/reconcile`。 | retraction/correction 与 claim 竞争时由 validity check 和 claim token/fence 保证失效事实不能外发；`invalidated` 不等于静默策略的 `suppressed`。 | 旧 pending/claimed event 需可失效；用户不会收到依据已经删除或修正事实生成、但尚未真正送达的提醒。 |
| D05 | **已 delivered Event 对应的 delivery receipt / WakeReceipt 保留审计，不回收聊天，不自动发纠正消息。** receipt 必须独立持久化，并以稳定 event/effect identity 记录已经产生的外部效果；EventOutbox 本身只保留投递状态和 `invalidated_at`/reason。快照清理直接/派生失效原值。 | 外部效果不可假装未发生，且其权威事实不得仅由 Event 状态表达；若未来要主动纠正，必须是独立产品功能，不能由 Kit 隐式补发。 | 需迁移/清理 receipt 审计快照中的失效值，并保持它与 Event 的稳定关联；用户已见消息保持原样，之后的查询不会继续泄露已删除值。 |
| D06 | **RuleState 按有效事实、definition version、scope 和稳定顺序确定性重放。** invalidated trigger 不占 fired 状态；历史不足标记 `incomplete`，不猜 previous。 | correction/retraction 与 ingest 共享 Fact lock/version；重建失败不得以手改 `previous_value` 冒充完成。 | 旧 RuleState 可从完整明细重建；明细不足时明确 `incomplete`。删除 72 后再上报 73 会按 70→73 正确判断，而不是被旧状态吞掉。 |
| D07 | **Conflict 是 durable `ConflictRecord`，有 `pending`/`resolved` 状态。** 同 Fact identity、同 revision、不同 canonical content 保留候选和来源证据；更高 revision 可自动解决，人工 resolution 接口后置。 | conflict 不能只留在一次 IngestOutcome；未解决前不能标为 fully applied，也不能推进依赖候选的投影（或标相关 projection conflicted）。并发候选通过同一 Fact key 串行化/版本检查收敛。 | 旧内存型冲突结果不视为已修复；需要持久迁移和查询/审计入口。用户看到可解释的待解决数据，而不是错误值静默冒充 Current。 |
| D08 | **单位合同：未声明单位视为 canonical；支持可选 per-field units，先转换再校验，并保留 source unit metadata。** `accepted_units` 必须进入运行链。 | 未知或不被 signal 接受的单位拒收，转换/值域/跳变校验失败不得污染事实或投影；同一语义的重传仍按 canonical payload 去重。 | 老 producer 不带单位继续按 canonical；升级 producer 可逐字段带单位。用户输入 lb/g 等声明支持的单位后得到正确统一数值，而不是被当作 canonical 错算。 |
| D09 | **显式提供非法 IANA timezone 时拒收 observation。** 只有未提供 timezone 时才可用 host fallback，且必须带 quality 标记。 | 不能把显式错误静默降级为 offset；失败 observation 不得写入按日 Aggregate。并发/重传遵循 D02 的 Report/Fact 身份合同。 | 老 payload 缺 timezone 可兼容，但要可见其 fallback quality；明确错误的 producer 必须修正后重发。用户不会因错误时区把事实静默归到错误日期。 |
| D10 | **v0.10 直接将公共 `get_current` 统一为 `signal -> entries[]`；IO/Rokku 同一交付批次迁移。** v0.9.1 不建立长期双公共 API。仅发现真实无法同批迁移的消费者时，允许有明确删除版本的临时兼容壳。 | 多维查询不得静默丢 dimension；compatibility shell 若获准，必须只做明确旧调用的迁移缓冲，不能成为第二套长期语义。 | 这是 v0.10 breaking cutover，不在 v0.9.1 偷渡。IO/Rokku 同批更新；用户能取回所有 anchors/维度，而不是只得到排序最新的一条。 |
| D11 | **聚合版本切换：新版本先全量或范围重算并记录 coverage，完成后原子切换 active version。** 普通 query/trend/streak 只读 active 且完整版本。 | 重算失败或中断继续读旧 active；不得混读新旧版本。并发写入按 `subject + signal + local_date + kind + aggregation_version` 共同串行化、CAS 或等价协议。 | 旧聚合保留用于审计、对照和回滚；新版本完成前用户继续看到完整旧口径，切换后同一查询不会半新半旧。 |
| D12 | **生产环境必须使用持久 `DefinitionProvider`；Kit 内存 archive 仅限测试/开发。** 历史 Event 按当时 definition version 解释。 | definition 缺失不得编造规则含义；重启、规则升级/删除后仍须查询到历史定义，否则明确报缺失/incomplete。 | 生产 adapter 要迁移并保存 definition history；测试可继续使用内存 archive。用户可以解释“为什么当时提醒”，而不是因规则更新丢失历史语义。 |
| D13 | **历史数据无法可靠重建时，标记 `incomplete`，不用部分明细覆盖旧 Aggregate；优先从来源重同步。** | 明细过期、coverage 不足或 definition history 缺失时不得伪造精确结果；重建重试须带稳定范围/版本身份，避免并发部分覆盖。 | 旧数据分为“明细仍在可重建”“可从来源重同步”“不可可靠修复”并分别报告；用户看到不完整状态，而不是一份看似精确但已污染的历史。 |
| D14 | **发布切分：v0.9.1 只收正确性与兼容修复；v0.10 承担公共查询、单位等破坏性合同。** | 任一版本都不能以半完成投影、双语义查询或静默数据降级伪装成功；发布失败可回滚到上一 active projection/version，兼容壳按删除版本移除。 | v0.9.1：D02/D03/D04/D05/D06/D07 的正确性与兼容修复，以及不改变公共合同的修复；v0.10：D10 public query cutover、D08 public unit contract 和其他需要 breaking schema/API 的迁移。IO/Rokku 与 Kit pin、迁移、部署须分别留证，不能以单一 Kit release 代替端到端交付。 |

### 发版和兼容规则（D14 的可执行解释）

1. **v0.9.1** 可以修复已承诺合同的正确性、事务、幂等、撤回/失效和持久冲突；不得新增长期并存的多维 Current 公共 API，也不得把破坏性单位合同伪装成补丁版本。
2. **v0.10** 是公共合同切换版本：`get_current` 的唯一公共返回形状是 `signal -> entries[]`；IO/Rokku 必须同批迁移。per-field unit 合同及其需要的破坏性 schema/API 也在此版本交付。
3. 临时兼容壳不是默认方案：只有确认存在无法同批迁移的真实消费者时才能增加，并且必须记录消费者、删除版本、owner 和删除验收；没有这些信息不得合入。
4. 历史迁移必须逐类声明：可自动迁移、可从来源重同步、因明细/definition history 过期而不可可靠修复。不可修复数据标 `incomplete`，不伪造回填。
5. 版本切换、迁移、adapter conformance 和宿主真实运行是分别验收的交付物；发布 Kit 包或测试通过本身不代表 IO/Rokku 已完成用户链路。

## 教训（2026-09-23）

9/17 拍的四条只写进了交接文档，次日 0.8.0 发版一条没带，**而且没有任何地方
报错** —— 决定和代码之间没有任何绑定。这和被审出来的那批 bug 是同一个形状。

所以：**拍板当轮就把它绑成会红的东西**；这张表是兜底，不是替代。
