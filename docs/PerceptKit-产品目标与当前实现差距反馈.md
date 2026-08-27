# PerceptKit 产品目标、当前实现差距与后续交付要求

> 日期：2026-08-26
> 文档性质：产品目标对齐与工程反馈
> 适用对象：PerceptKit 维护者、原 Feedling perception 链路维护者、iOS 上报侧和 Agent runtime 接入方

> 修订说明：本版根据 2026-08-26 评审意见，收窄了 Report Contract 的核心范围，明确加解密不属于 PerceptKit 核心协议；同时将“城市级 Location”和“Wi-Fi / 蓝牙位置锚点”拆开，并补充当前全部信号、TTL、历史聚合与实际存储现状。

## 1. 这份反馈要解决什么问题

这次检查后，我认为我们对“把 perception 从原仓库解耦成 PerceptKit”的理解存在明显偏差，而且这个偏差不是某个函数、某张表或者某条测试没有写好，而是对最终交付物的边界理解不同。

我原本期待的 PerceptKit 是：

> 一个可以快速插入任意 Agent runtime 的完整感知插件 SDK。iOS 或其他数据源只要按照统一协议上报，宿主只要实现 PerceptKit 定义的存储接口和 wake 接口，就可以获得从数据接收、标准化、当前状态、历史追溯、规则求值、Event 生成到唤醒 Agent runtime 的完整能力。

当前交付的 PerceptKit 实际上是：

> 从原宿主中抽取出来的一组纯函数判断算法。它假设上报、解密、标准化、存储、读取旧状态、Event 生成和 runtime 接线都已经由宿主完成。

这两者不是同一个完成度。当前实现并非完全没有价值，但它只完成了目标插件中的“判断算法层”，没有完成真正让其他 runtime 可以快速接入的协议、端口和编排层。

本文希望完整表达我的产品目标，并给出后续需要做到什么程度的明确要求，避免继续围绕当前实现做局部补丁，却没有解决插件本身不可直接接入的问题。

## 2. 先给结论

当前 PerceptKit `v0.1.0` 不能被视为已经完成的可插拔 perception SDK。

它目前比较准确的定位是：

```text
Perception judgment kernel / 感知判断内核
```

而我需要的最终定位是：

```text
Portable perception plugin SDK / 可移植感知插件 SDK
```

当前状态可以概括为：

```text
原 feedling-mcp
├── iOS 上报路由
├── envelope 解密
├── resolver
├── PostgreSQL 存储
├── current/history 查询
├── 固定 Event 生成
├── wake 控制
├── Runtime V2 接线
├── Resident Runtime 接线
└── 内部 perception_kernel 副本

独立 perceptkit
└── 从内部 kernel 抽出的纯函数
    ├── catalog
    ├── observation
    ├── identity
    ├── attribution
    ├── history
    ├── retention
    ├── streaks
    ├── trend_models
    ├── wake
    ├── fields
    ├── glance
    └── prompts
```

也就是说，完整链路仍然留在原宿主，独立 PerceptKit 只拿走了部分算法。原宿主当前也没有依赖外部 `perceptkit` 包，仍然 import 自己的 `backend/perception_kernel`。因此现在不仅没有完成完整插件化，还存在两份内核继续漂移的风险。

这次反馈不是要求把原宿主的所有代码和 PostgreSQL 表原样搬进 PerceptKit。原宿主现有的数据结构本身也有需要重新考虑的地方。正确方向应该是：

1. 从原宿主提炼通用协议和逻辑模型；
2. 把通用处理管线放进 PerceptKit；
3. 通过端口隔离具体 iOS、数据库和 Agent runtime；
4. 让 Feedling 成为 PerceptKit 的第一个 adapter，而不是 PerceptKit 继续依赖 Feedling 的内部概念。

## 3. 我对 PerceptKit 的目标定义

### 3.1 最终使用体验

一个新的 Agent runtime 接入 PerceptKit 时，理想上只需要完成三件事：

1. 实现或对接一个 `ReportAdapter`，把 iOS、Android、可穿戴设备或其他来源的数据转换成 PerceptKit 标准上报格式；
2. 实现一个 `StoragePort`，把 PerceptKit 定义的逻辑数据对象映射到宿主自己的数据库；
3. 实现一个 `WakePort`，把 PerceptKit 产生的标准 `PerceptionEvent` 交给宿主的 Agent runtime。

接入体验应接近：

```python
kit = PerceptionKit(
    storage=my_storage_adapter,
    wake=my_runtime_adapter,
    definitions=my_event_definitions,
)

result = kit.ingest(report)
```

宿主不应该为了接入而阅读 Feedling 的 `service.py`、`store.py`、数据库 migration、V1/V2 worker 或旧测试来猜测调用顺序。

### 3.2 PerceptKit 必须包含什么

PerceptKit 应当包含：

- 完整、版本化的数据上报协议；
- 统一的数据字典和 capability/signal/field manifest；
- 原始上报到标准 Observation 的解析、校验和标准化；
- current、observation、history、rule state、event outbox 等逻辑存储规范；
- `StoragePort` 接口及其一致性、幂等和事务语义；
- 从 Observation 到 current/history 的标准处理管线；
- `EventDefinition` 及内置规则求值器；
- 可扩展的自定义规则 evaluator 接口；
- 稳定的 `PerceptionEvent` envelope；
- `WakePort` 和 `WakeReceipt` 协议；
- current、history、trend 等标准查询 API；
- adapter conformance tests；
- 一套不依赖 Feedling V1/V2 的端到端接入示例和模板。

### 3.3 PerceptKit 明确不包含什么

PerceptKit 不应强制包含：

- 具体的 PostgreSQL、SQLite、DynamoDB 或其他生产数据库实现；
- iOS 端具体使用 HealthKit、Core Location、EventKit 的采集代码；
- iOS 网络重试和后台任务的具体实现；
- 某个 Agent runtime 的任务队列、worker、checkpoint 或消息模型；
- 某个产品 V1/V2 的主动说话风格；
- 模型最终是否给用户发送消息的决定；
- Feedling 的内部表名、job 类型、runtime lane 或部署结构。

“不提供具体数据库实现”不等于“不定义存储结构”；“不负责 iOS 上报实现”也不等于“不定义上报协议”。PerceptKit 必须规定逻辑语义和接口，宿主负责映射和实现。

## 4. 目标端到端链路

完整链路应当由 PerceptKit 明确定义为：

```text
iOS / Android / wearable / other producer
        │
        │  ReportEnvelope
        ▼
ReportAdapter / schema validation
        │
        ▼
Normalization + privacy projection
        │
        ▼
Canonical Observation
        │
        ├── 幂等身份
        ├── occurred_at / received_at
        ├── availability
        ├── 单位和字段类型
        └── 来源和修订信息
        │
        ▼
StoragePort transaction
        │
        ├── Observation log
        ├── Current projection
        ├── History aggregate
        ├── Rule state
        └── Event outbox
        │
        ▼
EventDefinition evaluator
        │
        ▼
PerceptionEvent
        │
        ▼
WakePort
        │
        ▼
Agent runtime
        │
        ▼
WakeReceipt
```

其中：

- 上报成功并不自动等于触发 Event；
- 触发 Event 不自动等于 wake 被接受；
- wake 被接受也不自动等于 Agent 必须给用户发送消息；
- `wake != speak` 是当前代码里正确的原则，应继续保留；
- 每一层都需要稳定、可测试的数据结构，而不是由宿主临时拼字典。

## 5. 当前 PerceptKit 实际完成了什么

当前仓库不是空壳，已经包含一些有价值的纯算法：

| 模块 | 当前能力 | 可保留价值 |
|---|---|---|
| `catalog.py` | 21 个 capability、20 个 signal、输出字段、TTL、部分 wake 属性 | 可作为未来 manifest 的起点 |
| `observation.py` | observed / observed_zero / no_observation / unavailable 四态 | 可作为现状参考，但正式协议应简化为 observed / no_data / unavailable；零值就是 observed 的合法 value |
| `identity.py` | 根据 source、metric、sample_id 生成稳定幂等 key | 可成为幂等策略的一部分 |
| `attribution.py` | 瞬时和区间数据的本地日期归属 | 可复用 |
| `history.py` | 每日聚合、趋势、跨域摘要 | 可作为 history engine |
| `retention.py` | 部分信号的保留期声明 | 需要进入统一 manifest 并由 adapter 执行 |
| `streaks.py` | 连续 N 天和 edge-trigger | 可成为一种内置 Event rule |
| `trend_models.py` | 波动、漂移、周期模型 | 可复用 |
| `wake.py` | 简单变化判断、顺序判断和防抖 | 需要重构进通用 rule/event 体系 |
| `fields.py` | Agent-facing 字段投影 | 可作为查询投影层的一部分 |
| `glance.py` | 低精度事实摘要 | 可作为可选输出投影 |
| `prompts.py` | 模型如何使用 perception facts 的说明 | 需要去除产品 V1/V2 语义 |

这些功能现在的共同特点是：调用方必须已经准备好正确的 Python 字典、旧状态、历史 rows 和时间参数。PerceptKit 不负责把外部上报可靠地变成这些输入，也不负责把结果可靠地写回宿主存储。

## 6. 当前实现与目标之间的主要差距

### 6.1 没有正式 Report Contract

Report Contract 仍然需要，但它不应该变成一套笨重、绑定原宿主的传输协议。它需要解决的核心问题只是：**一个宿主怎样把一批已经采集到的数据，以稳定、可校验、可版本化的方式交给 PerceptKit。**

当前 PerceptKit 无法独立回答这组最小问题：

- iOS 应该发送什么 JSON；
- 一批 report 如何标识和去重；
- 每种 signal 的 payload 类型是什么；
- 上报时间和测量时间如何区分；
- 一次批量上报怎样表达；
- observed、no data 和 unavailable 如何表达；
- schema 如何演进。

这里不要求 PerceptKit 负责 HTTP 路由、掉线队列、加密、解密、enclave 或密钥管理。原宿主中的 `ios_contract_v2.py`、上报路由和 enclave 解密逻辑属于 iOS / Feedling adapter 的实现，不应该原样提炼成 PerceptKit 核心协议。

修订和删除也不应强迫所有 signal 都实现。它们应放在需要这类语义的 source profile 中，例如 Health 样本修订、Calendar 增量同步和 tombstone。

### 6.2 Catalog 不是完整数据字典

当前 catalog 只声明了 input、capability、outputs、resolver 名称、TTL 和 significant 等少量属性。

它没有统一声明：

- 字段类型；
- 单位；
- 可空性；
- 合法范围；
- 来源语义；
- 隐私级别；
- current/history/event 三种用途；
- measurement identity；
- 适用的 source profile，以及该 profile 是否支持修订和删除；
- 聚合模型；
- retention；
- comparator；
- 默认 wake eligibility；
- 查询权限；
- schema version。

此外，catalog 中存在 `resolver="health_vitals"`、`resolver="weather"` 等名称，但 PerceptKit 仓库并没有 resolver 实现，实际 resolver 仍留在原宿主。

### 6.3 没有 StoragePort

PerceptKit 目前没有定义外部 runtime 应实现的存储接口。

因此宿主无法只做一个 adapter，而必须自行决定：

- current 怎么存；
- observation 是否保存；
- history 怎么组织；
- 并发如何控制；
- Event 如何持久化；
- retry 如何去重；
- TTL 如何执行；
- 用户删除数据时删哪些记录；
- Calendar 修改和删除怎样同步；
- history 聚合错误怎样重算。

这不是一个可以快速接入的 SDK 接口。

### 6.4 没有统一处理管线

PerceptKit 有单独的 `classify()`、`measurement_key()`、`observation_order()`、`record_daily()`、`is_significant_change()` 等函数，但没有一个公开服务把它们按照稳定顺序串起来。

缺少类似：

```python
result = kit.ingest(report)
```

或：

```python
result = processor.process_observation(observation)
```

宿主必须自己组织调用顺序，很容易导致不同 runtime 在重复数据、乱序数据、TTL、历史聚合和 Event 生成上行为不一致。

### 6.5 没有 EventDefinition

当前 PerceptKit 无法声明：

- 步数从 2,999 跨到 3,000 时触发；
- 步数从 4,999 跨到 5,000 时触发；
- 手机开机时触发；
- 连续三天睡眠少于六小时；
- 到达某个地点后触发；
- 某个值恢复正常后，再次异常时重新触发；
- 每天只触发一次；
- 触发后多久 rearm。

现在只有若干固定 wake source 和固定 trigger。用户不能通过稳定模板新增 Event，宿主只能自己写逻辑。

### 6.6 没有标准 PerceptionEvent 和 WakePort

当前 Event 和 runtime 接线仍在原 Feedling 宿主中，并直接分叉到 Pooled Runtime V2 和 Resident Runtime。

PerceptKit 中没有：

- 稳定 `PerceptionEvent` envelope；
- `EventOutbox` 语义；
- `WakePort`；
- `WakeReceipt`；
- accepted / duplicate / rejected 状态；
- crash 后重试和防重复协议。

因此它没有解决“插入任意 runtime”的核心问题。

### 6.7 外部包尚未真正接回原宿主

原 `feedling-mcp` 当前仍 import 内部 `backend/perception_kernel`，没有依赖外部 `perceptkit`。

现在实际存在：

```text
feedling-mcp/backend/perception_kernel
perceptkit/src/perceptkit
```

两份代码的模块数量也已经不同。独立包来源于原宿主一个 feature worktree，并非当前 main 的简单一一映射。若继续各自修改，会出现规则、字段、prompt 和 history 算法漂移。

### 6.8 仍包含 Feedling V1/V2 语义

PerceptKit 的 prompt、catalog 注释和示例仍出现 V1/V2、旧路由、旧 migration 和宿主行为假设。

通用插件可以有：

- package version；
- report schema version；
- event schema version。

但不应把某个产品的 V1/V2 对话策略变成核心公共契约。Agent 最终说不说话、一次谈几个话题，应由宿主 runtime 决定。

## 7. 上报协议需要做到什么程度

### 7.1 最小 Report Contract

Report Contract 有必要，但核心应保持很薄。建议最小格式如下：

```json
{
  "schema_version": 1,
  "report_id": "report_123",
  "observations": [
    {
      "signal": "health_vitals",
      "occurred_at": "2026-08-26T13:30:00Z",
      "state": "observed",
      "value": {
        "step_count": 3012
      },
      "sample_id": "optional_source_identity"
    }
  ]
}
```

核心字段只需要承担以下职责：

- `schema_version`：让协议可演进；
- `report_id`：标识一批上报，便于批级幂等和排查；
- `observations[]`：承载一个或多个标准观测；
- `signal`：选择对应的 signal manifest；
- `occurred_at`：设备实际测量或事件实际发生时间；
- `state`：本次是否真正观测到数据；
- `value`：由 signal manifest 定义的 typed payload；
- `sample_id`：仅在来源能够提供稳定样本身份时使用。

`subject_id` 不一定由 iOS 放进 report；宿主通常应从已认证连接中注入，避免信任客户端自报身份。`received_at` 也应由宿主接收层生成。`producer`、匿名化 `device_id`、timezone 等可以作为可选扩展，但不应让最小接入变复杂。

### 7.2 时间和身份边界

核心至少必须区分：

- `occurred_at`：设备测到或事件发生的时间，由数据源提供；
- `received_at`：宿主收到 report 的时间，由宿主注入；
- `effective_date`：PerceptKit 根据 `occurred_at` 和时区归属得到的聚合日期；
- `updated_at`：宿主存储记录最后更新时间。

离线补传和乱序判断依赖 `occurred_at` / `received_at` 的区别。`report_id` 处理 report 级重传；`sample_id` 或 source event id 处理 observation 级重传。没有稳定样本身份的瞬时值可以采用由 manifest 明确的 identity strategy。

### 7.3 状态只保留最小语义

协议层建议只保留三种状态：

```text
observed
no_data
unavailable
```

- `observed`：已经获得可靠值；`0`、`false`、空数组都可以是合法 value，不需要另设 `observed_zero`；
- `no_data`：本次查询成功，但在指定范围内没有样本；
- `unavailable`：来源当前无法提供该数据。

必要时可带一个可选 `reason`，例如 `permission_denied`、`not_supported`、`source_error`。不建议在核心协议中穷举所有操作系统权限状态；这些细节可以保留在 adapter diagnostics 中。

`stale` 不是 iOS 上报状态，而是 PerceptKit 读取 current 时依据 `occurred_at + current_ttl` 推导出的结果。

### 7.4 复杂语义放入 Source Profile

不是所有 signal 都需要 revision、delete、cursor 或 tombstone。应在最小 Report Contract 之上定义少量按来源启用的 profile：

| Source Profile | 额外语义 |
|---|---|
| Health sample | `sample_id`、单位、样本修订、source-side delete |
| Calendar / Reminders sync | full / incremental、coverage window、sync cursor、revision、tombstone |
| Location | 城市 / locality、country / region、可选粗粒度坐标和 placemark 来源 |
| Device occurrence | 稳定 source event id，例如 boot、photo added |
| Proximity anchor | anchor id、Wi-Fi / Bluetooth 类型、进入 / 离开状态 |

这样既能支持复杂来源，又不会让 Battery、Time、Weather 等简单信号承担无用字段。

### 7.5 加解密属于 Adapter，不属于 PerceptKit 核心

iOS 是否加密、使用什么 envelope、在哪里解密、是否经过 enclave，应由 iOS 上报 adapter 和宿主的安全边界共同决定。PerceptKit 接收的是已经通过宿主认证、解密和基本传输校验后的 report。

PerceptKit 可以声明字段的 `privacy_class`，帮助宿主决定日志、持久化和查询投影，但不应该定义具体密码算法、密钥生命周期或 enclave 路由。canonical Observation 也不应被 `context_snapshot`、`/app_open`、`/photo/evaluate` 等 Feedling 路由名绑死。

## 8. Manifest 和字段规范需要做到什么程度

每个 capability/signal/field 应由一个统一 manifest 声明，至少包括：

```text
key
label
schema_version
value_type
unit
nullable
valid_range / enum
privacy_class
current_ttl
history_mode
history_retention
identity_strategy
attribution_strategy
aggregation_strategy
comparison_strategy
wake_eligible
query_visibility
normalizer
source_profile（可选）
```

这里的 `privacy_class` 是数据处理提示，不表示 PerceptKit 自己负责加解密。revision、delete、cursor 等字段只由对应 `source_profile` 声明，不应成为所有 signal 的公共负担。

新增一个字段时，不应再要求开发者同时手工修改 catalog、fields、history、retention、attribution、trend model 和 prompt 中多张互不关联的表。

允许少数算法需要专门实现，但 manifest 应当是路由这些算法的单一入口，测试应检查：

- 所有字段都有类型和单位；
- 所有可历史化信号都有 retention；
- 所有 resolver/normalizer 名称都有实现；
- 所有 event-eligible 字段都有 comparator；
- current/history/query 投影不会漂移。

## 9. 存储规范需要做到什么程度

### 9.1 先明确当前事实：PerceptKit 里面没有存储表

截至本次核验的 `v0.1.0`，PerceptKit 中没有：

- database migration；
- SQL / ORM model；
- `StoragePort`；
- 可直接采用的 reference schema；
- retention cleanup job；
- 把 report 持久化为 current / history / event 的实现。

它目前只有 Python 字典形状、字段 TTL、daily aggregation 算法和部分 retention 声明。因此现在无法回答“接入 PerceptKit 后数据库最终会出现哪几张表”，因为这个答案尚未被设计和交付。

原 Feedling 宿主确实有实际表和 JSONB 存储，但那是原宿主的现状实现，不是 PerceptKit 已经提供的能力，也不应未经评审就直接复制成插件规范。

### 9.2 不限定数据库，但必须定义逻辑记录

建议至少定义以下逻辑对象：

#### Observation

一条经过标准化、可以去重、修订和删除的观测事实。

```text
observation_id
subject_id
signal
source
occurred_at
received_at
availability
value
unit
source_sample_id（可选）
source_extension（可选；由 Health / Calendar 等 profile 定义 revision、delete 等）
```

#### CurrentValue / CurrentProjection

某个 subject、signal、field 的当前值。

```text
subject_id
signal
field
value
observed_at
expires_at
availability
source_observation_id
version
```

#### HistoryAggregate

某个日期或时间窗口的派生聚合。

```text
subject_id
signal
window_start
window_end
aggregation_version
doc
updated_at
```

#### SourceSyncState

用于 Calendar、Reminders 等外部可修改集合的同步状态。

```text
subject_id
source
sync_cursor
coverage_start
coverage_end
snapshot_kind: full | incremental
captured_at
```

#### EventDefinition

用户或宿主定义的触发规则。

#### EventState

保存某条规则的 baseline、是否已 firing、上次触发时间和 rearm 状态。

#### EventOutbox

已经生成但尚未被 runtime 确认接收的事件。

#### WakeReceipt

runtime 对事件的接收结果。

这些是逻辑职责，不表示必须一项对应一张 SQL 表。宿主可以使用 PostgreSQL、SQLite、文档数据库或其他方案，但需要证明能够满足相同的查询、幂等、重算、删除和一致性语义。

### 9.3 StoragePort 应规定行为，不规定 SQL

接口至少应覆盖：

```python
class StoragePort(Protocol):
    def append_observation(...): ...
    def get_current(...): ...
    def put_current(...): ...
    def list_observations(...): ...
    def get_aggregate(...): ...
    def put_aggregate(...): ...
    def get_rule_state(...): ...
    def put_rule_state(...): ...
    def enqueue_event(...): ...
    def pending_events(...): ...
    def record_wake_receipt(...): ...
```

真实接口可以根据事务设计调整，但必须覆盖这些语义。

### 9.4 必须写清楚的一致性保证

需要明确：

- 同一个 observation 重传不会重复累计；
- 迟到旧数据不能覆盖更新 current；
- 同时间不同内容是 conflict，不应静默覆盖；
- current、history、rule state 和 event 之间哪些必须原子提交；
- Event 必须先 durable，再交给 runtime；
- runtime 超时或崩溃后可重试；
- 同一个 event id 不会重复造成可见副作用；
- 修订和删除可以重算受影响的 aggregate；
- 用户删除账号时所有逻辑数据都能被定位和删除。

当前原宿主中 current 写成功后，daily history 失败只记 warning，二者不是一个原子边界。新规范需要明确是否接受这种不一致；不能继续作为未说明的实现细节。

## 10. Current、历史和全量存储的目标

### 10.1 Current

Current 用来回答“现在是什么状态”。必须有字段级 TTL。

例如：

- Battery：10 分钟；
- Location：15 分钟；
- Weather：30 分钟；
- Calendar 当前视图：由同步覆盖范围和 captured_at 共同决定；
- Health Vitals：按具体字段设定；
- timezone/locale：可作为稳定上下文，不随普通 sensor TTL 过期。

超过 TTL 后：

- Agent 查询应返回 stale/unavailable，而不是把旧值当当前事实；
- 是否物理删除旧值由存储策略决定；
- 最近可靠值可以作为 `last_known` 返回，但必须带 `as_of`，不能冒充 current。

### 10.2 Observation 时间线

不能只存 current，也不能只存不可逆的 daily aggregate。

对需要修订、删除、重算或触发精确 Event 的数据，应保存标准化 observation 或等价的可重放记录。

不要求永久保存所有原始敏感 payload。可以按 signal 使用不同策略：

- 原始精确位置只在 normalizer 中短暂使用，默认不持久化；
- 标准化的粗地点 observation 可按隐私策略保留；
- HealthKit 稳定 sample id、revision、delete 需要保留；
- 体重等低频记录需要可修订；
- Battery/时间等瞬时数据可仅保留 current；
- 照片字节由宿主对象存储保存，PerceptKit 只保存 metadata 和 pointer contract。

### 10.3 HistoryAggregate

History 是派生数据，不应成为唯一事实来源。

当前 PerceptKit 的 daily aggregation 算法可以保留，但需要：

- 明确 aggregation version；
- 支持从 observation 重算；
- 修订或删除 observation 后能更新聚合；
- 明确 retention；
- 明确 window 和 timezone；
- 明确 current 与 aggregate 的事务/恢复策略。

### 10.4 不建议“所有原始数据永久全量存储”

我并不是要求把所有 iOS 原始数据永久保存。正确方向是分层：

```text
短期或不保存的 raw payload
        ↓
按信号策略保存的 canonical observations
        ↓
current projection
        ↓
长期、低敏感度的 aggregates
        ↓
bounded events / receipts
```

每种 signal 的策略应由 manifest 声明，而不是所有数据共用同一种保留方式。

## 11. 几类重点数据应如何处理

### 11.1 Location

这里需要把两类不同数据拆开，不能都叫 Location：

#### A. 广义 Location：iOS 上报的城市 / 粗粒度地理位置

这才是本文主要所指的 Location。它通常来自 Core Location 获取的经纬度，再由 iOS 或宿主反向解析为 placemark；也可能由 iOS 直接上报已经解析好的城市信息。规范至少应表达：

```text
locality / city
country / region
occurred_at
coordinate（可选，仅在允许的隐私边界内短暂处理）
accuracy / placemark_source（可选）
```

需要同时支持：

- current city / locality；
- city / locality / country 的时间线；
- 从时间线派生“过去三个月从成都迁移到上海”这类变化；
- 明确经纬度是在 iOS 解析还是宿主解析；
- 默认不把精确经纬度和完整地址永久写入历史。

#### B. Proximity Anchor：Wi-Fi / 蓝牙命名的位置锚点

`home`、`work`、某个房间等更精细的位置，不应和城市 Location 混为同一字段。这类信息更适合通过用户命名的 Wi-Fi 或 Bluetooth anchor 表达：

```text
anchor_id
anchor_type: wifi | bluetooth
label: home | work | 自定义名称
state: enter | present | leave
occurred_at
dwell/session（派生）
```

anchor label 可以改名，同一个 `home` 也可能随搬家迁移，因此稳定身份应是 `anchor_id`，而不是 `label` 本身。BSSID 等原始硬件标识不应直接暴露给 Agent 或作为跨宿主公共字段。

#### 当前代码实际情况

当前 `location_signal` 输出的是：

```text
place_label
wifi_label
country
locality
wifi_anchor_id
```

原宿主 resolver 会暂时使用经纬度和宿主配置的 geofence 推导 `place_label`，接收设备提供的 `wifi_label` / `wifi_anchor_id`，并从 locale / placemark 推导 `country`、保留 `locality`。从已核验代码看，原始坐标和 BSSID 默认没有进入最终 current/history。

但当前 daily history 只按 `place_label` 聚合 `place_dwell`，`locality` 和 `country` 只保留 current。因此“三个月前在成都、现在在上海”不能可靠追溯；如果两个时期的 `place_label` 都叫 `home`，历史更无法看出城市变化。

此外，wake 层还有 `connectivity_anchor`、`wifi_anchor`、`bluetooth_anchor`，但它们并未形成一套完整的 anchor Observation / history 规范。工程师需要说明 iOS 端真实 Location producer、经纬度解析位置，以及 Wi-Fi / Bluetooth anchor 的身份和 enter/leave 数据究竟如何产生。

### 11.2 Calendar

Calendar 不能只是一条 TTL 为一小时的 `calendar_next_event`。

需要定义同步语义：

- provider/source calendar id；
- provider event id；
- event revision/etag；
- created/updated/deleted；
- recurrence identity；
- full snapshot 或 incremental delta；
- sync cursor；
- coverage window；
- captured_at；
- tombstone；
- 当前有效日程查询。

iOS 可以只上报系统允许访问的范围，但必须告诉 PerceptKit“这次是完整快照还是增量”“覆盖哪个时间范围”。不能把某次上传过的事件永久追加到 daily list 后就认为完成同步。

### 11.3 App 使用

建议逻辑上保存 open/close 事件时间线，并派生 session 和统计：

- current app 有短 TTL；
- open/close 是 append-only observation；
- 同一事件有 source event id；
- 乱序 open/close 可重建；
- history retention 明确按时间，而不是仅按固定 2,000 条；
- 用户关闭权限后停止读取，并按策略删除或停止使用历史。

当前原宿主使用两个 `user_logs` stream，各截断为 2,000 条；这不是通用、明确的长期历史规范。

### 11.4 Health、步数和体重

需要保留来源样本身份，支持：

- 重传幂等；
- sample revision；
- source-side deletion；
- unit normalization；
- 当日累计和长期趋势；
- threshold-crossing Event；
- 不同健康字段的 wake eligibility。

仅保存 `min/max/sum/count` 会导致重复计入、修订和删除无法正确回滚。当前 `identity.py` 的注释实际上已经指出这个问题。

### 11.5 音乐播放

需要区分：

- current now-playing；
- playback state transition；
- session；
- 每日 artist/track 聚合；
- 历史 retention；
- 同一 track 的重复上报去重。

当前 daily tally 算法可以作为派生层，但不应代替完整的输入语义。

### 11.6 Weather

Weather 更接近有 TTL 的外部缓存：

- current 必须有 freshness；
- 可以保留有限 daily aggregate；
- 不建议永久保存每次原始天气 payload；
- 来源、定位范围和 forecast validity 应明确。

### 11.7 当前全部信号、Current TTL 与 History 现状

下面这张表按本次核验的 PerceptKit `v0.1.0` catalog / history / retention 代码整理。它描述的是**当前代码声明和算法能力**，不是已经落地的数据库设计。

| Signal | 当前输出字段 | Current TTL | 当前 history mode | 声明的 history retention |
|---|---|---:|---|---:|
| `time` | `local_time`, `timezone`, `locale` | 5 分钟 | 无 | 未声明 |
| `battery` | `battery_level`, `charging` | 10 分钟 | 无 | 未声明 |
| `broadcast` | `broadcast_state`, `broadcast_active` | 5 分钟 | 无 | 未声明 |
| `focus` | `focus_authorization_status`, `in_focus` | 5 分钟 | `duration_by_state` | 90 天 |
| `location_signal` | `place_label`, `wifi_label`, `country`, `locality`, `wifi_anchor_id` | 15 分钟 | `place_dwell`，当前只按 `place_label` | 声明永久 |
| `motion_state` | `motion_state` | 5 分钟 | `duration_by_state` | 90 天 |
| `calendar_next_event` | `calendar_next_event`, `calendar_events`, `calendar_events_truncated` | 1 小时 | `event_list` | 60 天 |
| `playback` | `now_playing` | 10 分钟 | `tally` | 365 天 |
| `audio_route` | `output_type`, `is_bluetooth`, `device_name` | 10 分钟 | `duration_by_state` | 90 天 |
| `weather` | `condition`, `temperature`, `apparent_temperature`, `humidity`, `precipitation_chance`, `uv_index`, `is_daylight`, `alerts` | 30 分钟 | `numeric_dist` | 90 天 |
| `reminders` | `next_reminder`, `reminders`, `overdue_count`, `due_today_count`, `reminders_truncated` | 1 小时 | `event_list` | 60 天 |
| `app` | `app_name`, `app_category`, `app_state` | 15 分钟 | PerceptKit daily history 无对应聚合 | 未声明 |

Health 相关信号如下：

| Signal | 当前输出字段 | Current TTL | 当前 history mode | 声明的 history retention |
|---|---|---:|---|---:|
| `health_sleep` | `asleep_minutes`, `core_minutes`, `deep_minutes`, `rem_minutes` | 24 小时 | `main_of_day` | 声明永久 |
| `health_workout` | `workout_type`, `duration_min`, `count_today` | 24 小时 | `event_list` | 声明永久 |
| `health_vitals` | `resting_heart_rate`, `step_count`, `current_heart_rate`, `hrv_sdnn_ms`, `respiratory_rate`, `oxygen_saturation_pct`, `vo2_max` | 1 小时 | `numeric_dist`；`step_count` 使用每日最大值表达当日累计 | 声明永久 |
| `health_activity` | `active_energy_kcal`, `exercise_minutes`, `stand_minutes`, `mindful_minutes` | 1 小时 | `cumulative` | 声明永久 |
| `health_body` | `weight_kg`, `bmi`, `body_fat_pct`, `height_cm` | 24 小时 | `main_of_day` | 声明永久 |
| `health_metabolic` | `blood_glucose_mmol_l`, `blood_pressure_systolic`, `blood_pressure_diastolic` | 24 小时 | `numeric_dist` | 声明永久 |
| `health_cycle` | `flow_level`, `is_active_period` | 24 小时 | `main_of_day` | 声明永久 |
| `health_mood` | `valence`, `valence_classification`, `kind`, `label_count`, `recorded_today` | 24 小时 | `subjective` | 声明永久 |

还存在两类不完全进入上述 signal catalog 的数据：

| 类别 | 字段 / 信号 | 当前情况 |
|---|---|---|
| Photos capability | `has_faces`, `face_count`, `scene_hint`, `scene_confidence`, `time_of_day`, `is_burst`, `is_indoor`, `has_text_block`, `is_screenshot`, `place_label` | 没有普通 SignalDefinition、TTL 或 PerceptKit retention；原宿主分别保存 metadata 和图片 payload |
| 固定 wake / device signals | `connectivity_anchor`, `wifi_anchor`, `bluetooth_anchor`, `unlock_after_absence`, `screen_phash`, `photo_added`, `broadcast_state` | 位于 wake 路径之外或旁路；原宿主主要只保存 fingerprint baseline、时间戳和 source event identity，并非标准 observation timeline |

需要特别注意：`retention.py` 明确只是在声明保留期，没有实现清理。它另外给 `health_body` 设了 90 天、`health_metabolic` 30 天、`health_cycle` 60 天、`health_vitals` 7 天的 measured-at TTL，但原宿主 current 读取仍主要使用 catalog TTL。两套 TTL 的适用关系目前并未完整接线，需要工程师统一解释和实现。

### 11.8 上表最终存成什么：当前还没有答案

PerceptKit 当前并没有把上面的信号实际写成任何表。原宿主目前大致使用：

| 原宿主存储 | 实际用途 |
|---|---|
| `user_blobs` / `perception_state` | 将多数 current 字段放在一个 JSONB；单字段形状近似 `{"v": value, "ts": timestamp, "msg": optional}` |
| `perception_daily` | 每个 user / date / signal 一条 daily aggregate 文档 |
| `perception_items` | collection item 和部分照片 metadata |
| `user_logs` | `app_usage`、`app_close`、`perception_events`、wake context 等流式记录 |
| `perception_signal_state_v2` | 固定 wake 信号的 HMAC fingerprint baseline |
| `frame_envelopes` | 照片 payload |

这些表只能用于理解旧行为，不能被描述成“PerceptKit 已经提供的表结构”。后续工程交付至少要给出 Current、Observation timeline、Daily/window aggregate、Source sync state、EventDefinition、Event rule state、Event outbox、Wake receipt 这些逻辑对象的 reference mapping；至于宿主最终映射为几张 SQL 表，可以由宿主决定。

## 12. EventDefinition 和自定义 Event

### 12.1 必须区分 Definition 和 Occurrence

需要两个不同概念：

```text
EventDefinition
用户或宿主定义“什么时候触发”

PerceptionEvent
某条规则命中后产生的一次不可变事件事实
```

规则可以自由组合和扩展，但产生的 Event envelope 必须稳定，才能被任意 runtime 接收。

### 12.2 声明式规则模板

例如每天步数跨过 3,000 时触发一次：

```yaml
id: daily_steps_3000
version: 1
enabled: true

source:
  signal: health_vitals
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
  severity: informational

wake:
  enabled: true
  cooldown_seconds: 0
```

5,000 步是另一条独立 definition。

必须使用 crossing 语义：

```text
previous < 3000 and current >= 3000
```

不能简单使用 `current >= 3000`，否则 3,001、3,010、3,100 的每次上报都会重复触发。

手机开机示例：

```yaml
id: device_started
version: 1
enabled: true

source:
  signal: device_boot

condition:
  type: occurrence

deduplication:
  key: boot_session_id

event:
  type: device.started

wake:
  enabled: true
  cooldown_seconds: 300
```

前提是上游设备 adapter 能可靠提供该信号和 identity。

### 12.3 建议的内置 rule 类型

P0 至少应考虑：

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

不需要一开始就实现通用、无限能力的表达式语言。优先做一组语义清晰、可持久化、可测试的规则。

### 12.4 自定义 evaluator

可信任的 runtime 开发者应可以注册代码型 evaluator：

```python
class RuleEvaluator(Protocol):
    kind: str

    def evaluate(
        self,
        definition,
        previous,
        current,
        history,
        state,
    ) -> RuleResult:
        ...
```

普通用户配置使用安全的声明式规则，不允许把任意未受信任代码作为 Event Definition 执行。

## 13. 标准 PerceptionEvent

用户可以自定义规则和 `event.type`，但事件 envelope 不能每次随意变化。

建议至少包含：

```json
{
  "event_id": "evt_...",
  "definition_id": "daily_steps_3000",
  "definition_version": 1,
  "subject_id": "user_...",
  "type": "activity.step_goal_reached",
  "signal": "health_vitals",
  "field": "step_count",
  "occurred_at": "2026-08-26T10:30:00+08:00",
  "received_at": "2026-08-26T10:30:04+08:00",
  "previous": 2975,
  "current": 3012,
  "condition": {
    "type": "threshold_crossing",
    "operator": "gte",
    "value": 3000
  },
  "context": {
    "scope": "2026-08-26",
    "unit": "count"
  },
  "schema_version": 1
}
```

事件只表达：

- 发生了什么；
- 哪条规则命中；
- 新旧事实是什么；
- 为什么符合触发条件。

事件不表达：

- Agent 必须说什么；
- 必须给用户发通知；
- 使用哪个模型；
- 进入 Feedling V1 还是 V2。

## 14. WakePort 和可靠投递

建议公开：

```python
class WakePort(Protocol):
    def wake(self, event: PerceptionEvent) -> WakeReceipt:
        ...
```

`WakeReceipt` 至少需要：

```text
event_id
status: accepted | duplicate | rejected
runtime_ref
reason
received_at
```

可靠性要求：

1. Observation 被接受后，Event 必须先进入 durable outbox；
2. outbox 提交成功后才能调用 WakePort；
3. 进程在调用前或调用后崩溃都可以重试；
4. runtime 按 `event_id` 幂等；
5. outcome unknown 时不能盲目当成功；
6. receipt 必须可以持久化和查询；
7. wake 被接受后，runtime 是否开口仍由 runtime 自己决定。

同时应支持只生成 Event、不立即 dispatch：

```python
result = kit.ingest(report, dispatch=False)

for event in storage.pending_events():
    receipt = runtime.wake(event)
    storage.record_wake_receipt(receipt)
```

这样可以兼容同步、异步、队列和轮询式 runtime。

## 15. 查询接口

PerceptKit 至少需要稳定的 Python 查询 API；宿主可以再把它们暴露成 REST、MCP、CLI 或原生工具。

建议包括：

```text
get_current(subject_id, signals)
get_last_known(subject_id, signal)
list_observations(subject_id, signal, from, to, cursor)
get_history(subject_id, signal, window)
get_trend(subject_id, signal, field, window)
list_events(subject_id, status, cursor)
list_definitions(subject_id)
```

所有 list 查询必须分页或有明确上限。

工具层不应该写死成 Feedling 的 MCP schema。PerceptKit 提供数据和契约，宿主决定如何暴露工具。

## 16. Runtime 中立性要求

PerceptKit 核心代码和公共 schema 不应出现：

- Feedling V1/V2；
- Resident/Pooled runtime；
- `agent_jobs`；
- `proactive_jobs`；
- Feedling 内部 wake kind；
- Feedling 特定路由；
- Feedling 数据库表名；
- 某个 worker 文件和模块名。

可以保留：

- `schema_version`；
- `definition_version`；
- package semantic version；
- 通用 `runtime_ref`；
- 通用 `origin_refs`。

Feedling 应实现独立 adapter：

```text
FeedlingStorageAdapter
FeedlingWakeAdapter
FeedlingIOSReportAdapter
```

这些 adapter 可以留在 Feedling 仓库，也可以放在单独 integration package，但不应污染 PerceptKit 核心。

## 17. 隐私与安全边界

PerceptKit 的规范需要明确：

- credentials 不进入 Observation、Event、workspace 或 Agent context；
- 精确位置、BSSID、完整地址默认不进入持久化 current/history；
- 敏感健康、日历、提醒和照片 metadata 的 visibility；
- raw payload 与 canonical observation 的不同 retention；
- Event context 必须有大小和字段白名单；
- 一个 Event 不能透传任意存储 doc；
- 宿主必须隔离 subject/tenant；
- 用户撤权后停止读取和 wake；
- 用户删除时能删除 observations、current、history、rules、events 和 receipts；
- adapter conformance test 必须包括跨用户负面测试。

## 18. 推荐的包内职责划分

不要求完全照此命名，但职责需要存在并分开：

```text
perceptkit/
├── contracts/
│   ├── report
│   ├── observation
│   ├── manifest
│   ├── event
│   └── receipt
├── ports/
│   ├── storage
│   ├── wake
│   └── normalizer
├── processing/
│   ├── ingest
│   ├── ordering
│   ├── current
│   └── history
├── rules/
│   ├── definitions
│   ├── evaluators
│   └── state
├── queries/
│   ├── current
│   ├── history
│   └── trend
├── algorithms/
│   └── 当前已有纯函数
└── conformance/
    ├── storage
    ├── wake
    └── report
```

重点不是目录数量，而是不能再把 contract、算法、存储、宿主 runtime 接线混成一层。

## 19. 对当前原宿主数据结构的评价

原 Feedling 链路目前使用混合存储：

```text
user_blobs/perception_state       current JSON blob
perception_items                  部分集合 item
perception_daily                  每日聚合
perception_signal_state_v2        少量 wake 信号的 HMAC baseline
user_logs/perception_events       wake/suppressed/debounced 审计
user_logs/app_usage               App open
user_logs/app_close               App close
frame_envelopes                   照片密文
agent_jobs/proactive_jobs         两套 runtime 投递
```

这套实现可以作为行为和历史问题的参考，但不应直接成为 PerceptKit 的强制表结构，原因包括：

- current、history、event 分散且并非统一事务；
- 不保存完整 canonical observation；
- Calendar 不是真正同步模型；
- Location history 不包含城市迁移所需信息；
- App history 按 2,000 条截断；
- daily retention 没有统一执行；
- 聚合无法可靠处理 source revision/delete；
- wake 直接耦合两套 runtime；
- Event 类型固定，无法按模板自定义。

后续应提炼逻辑语义，而不是复制原表。

## 20. 最低可接受交付物

我认为下一阶段至少需要交付以下内容，才能称为“可插拔 PerceptKit”的第一个完整版本。

### 20.1 契约

- 最小 `ReportEnvelope` / `Observation`，不包含加解密实现；
- Health、Calendar、Location、Device occurrence 等按需启用的 source profile；
- `CapabilityDefinition` / `SignalDefinition` / `FieldDefinition`；
- `EventDefinition`；
- `PerceptionEvent`；
- `WakeReceipt`；
- 明确的 schema versioning 规则。

### 20.2 端口

- `StoragePort`；
- `WakePort`；
- 可选 `NormalizerPort` / adapter registry；
- 每个端口的错误、重试、幂等和事务说明。

### 20.3 处理管线

- Report 校验；
- normalizer；
- `observed / no_data / unavailable` 状态归一化；
- identity；
- ordering；
- current TTL；
- observation/history 写入；
- Event rule 求值；
- durable outbox；
- wake dispatch 和 receipt。

### 20.4 查询

- current；
- last known；
- observation timeline；
- daily/window history；
- trend；
- event 状态。

### 20.5 Event rules

- 至少实现 `changed`、`threshold_crossing`、`occurrence` 和 `streak`；
- 支持 daily scope、once、cooldown、rearm；
- 支持用户同时定义 3,000 和 5,000 步两条规则；
- 提供可信任 custom evaluator 接口。

### 20.6 Conformance tests

- Storage adapter conformance；
- Wake adapter conformance；
- Report adapter conformance；
- duplicate/stale/conflict；
- crash/retry；
- current TTL；
- history 重算；
- Calendar modify/delete；
- Location timeline；
- cross-tenant negative tests。

测试可以使用专门的 in-memory test double，但它只是测试工具，不是 PerceptKit 强制提供的生产存储实现。

### 20.7 接入模板

需要一个真正端到端的示例：

```text
读取 fixture Report
→ 标准化 Observation
→ 写入示例 StoragePort
→ 查询 current
→ 查询 history
→ 命中 3,000 步 EventDefinition
→ 生成 PerceptionEvent
→ 调用示例 WakePort
→ 保存 WakeReceipt
```

当前 quickstart 只串联纯函数，不能代表插件已经可接入。

## 21. 建议的实施阶段

### 阶段 0：先对齐 contract，不继续扩功能

- 确认本文描述的产品边界；
- 明确哪些旧 Feedling 语义保留、哪些不进入核心；
- 确认逻辑数据模型；
- 确认 Report、Observation、Event 和 ports。

在 contract 未对齐前，不建议继续增加更多 signal 或 prompt 规则，因为会扩大迁移和漂移成本。

### 阶段 1：建立可编译、可测试的协议和端口

- contracts；
- manifest；
- StoragePort；
- WakePort；
- conformance harness。

### 阶段 2：打通最小垂直链路

选择少量代表性信号：

- Battery：current + TTL；
- Steps：observation + daily aggregate + threshold Event；
- Device boot：occurrence Event；
- Location：city/locality current + coarse timeline；
- Proximity anchor：以独立 signal 验证 Wi-Fi / Bluetooth anchor identity 与 occurrence。

完整打通 Report → Storage → Event → WakeReceipt。

### 阶段 3：复杂同步与历史

- Calendar full/incremental sync；
- App open/close timeline；
- Health revision/delete；
- Music session；
- history rebuild。

### 阶段 4：Feedling 作为第一个真实 adapter

- 把原宿主接到外部 PerceptKit；
- Feedling 不再直接 import 内部 `perception_kernel`；
- V1/V2 分流只存在于 FeedlingWakeAdapter；
- 原内部副本明确删除或停止演进；
- 用行为基线验证接入前后等价部分。

## 22. 完成定义

以下场景全部通过，才可以称为完成了第一版可插拔 SDK：

1. 一个不包含 Feedling 代码的新示例 runtime 可以实现 StoragePort 和 WakePort；
2. iOS fixture 可以通过标准 ReportAdapter 进入 PerceptKit；
3. 重传同一 observation 不会重复累计；
4. 迟到数据不会覆盖 current；
5. current 超过 TTL 后不会继续冒充当前事实；
6. 可以查询标准化 observation timeline；
7. 可以查询 daily history 和 trend；
8. 2,999 → 3,000 只触发一次 3,000 步 Event；
9. 4,999 → 5,000 可以独立触发 5,000 步 Event；
10. 同一天后续 5,001 不会重复触发；
11. 第二天规则按定义 rearm；
12. device boot event 可按 source event id 去重；
13. Event 在 wake 前已经 durable；
14. WakePort 超时重试不会产生重复 runtime 副作用；
15. Calendar 修改和删除能反映到当前日程；
16. Location 可以表达成都到上海这样的 city/locality timeline；
17. Wi-Fi / Bluetooth anchor 使用稳定 identity，且不会和城市 Location 混成同一语义；
18. Feedling adapter 和另一个最小示例 adapter 使用同一套核心管线；
19. PerceptKit 核心没有 V1/V2、Feedling 表名、加解密或 worker 依赖；
20. 所有 adapter conformance tests 通过；
21. 文档足以让不了解 Feedling 原仓库的工程师独立完成接入。

## 23. 希望工程师明确回复的问题

为了确认下一步方案，希望工程师逐项回复：

1. 当时对“PerceptKit”的目标理解是什么？为什么将边界限定为纯函数 kernel？
2. 是否认同最终目标应是完整插件 SDK，而不只是 judgment kernel？
3. 原计划中 Report Contract、StoragePort、EventDefinition 和 WakePort 是否在其他任务或仓库中？
4. 为什么 resolver 名称进入了 catalog，但 resolver 实现没有进入 PerceptKit？
5. 为什么原宿主尚未依赖外部 PerceptKit，内部副本准备如何处理？
6. 当前 iOS producer 的真实代码在哪里？它实际会上报哪些 signal 和字段，上报频率、失败补传和 Calendar 同步策略是什么？
7. 原宿主现有 `perception_daily` 不保存原始 observation，如何处理样本修订和删除？
8. Current、history 和 Event 是否计划放在同一个一致性边界？
9. Calendar 当前是否仅是 snapshot，而不是 sync？如果是，后续计划是什么？
10. 广义 Location 的城市信息目前是 iOS 从经纬度解析，还是宿主解析？原始坐标是否会被持久化？
11. Wi-Fi / Bluetooth anchor 的真实 producer 在哪里？`anchor_id`、label、enter/leave 和改名/迁移目前如何表达？
12. EventDefinition 是否计划支持用户自定义 threshold、occurrence、streak 和 rearm？
13. 如何保证新的 PerceptKit 不再绑定 Feedling V1/V2？
14. 预计如何分阶段把 Feedling 变成第一个 adapter，并防止两份 kernel 漂移？
15. 请给出上述全部 signal 对应的 Current、Observation、Aggregate、Sync State、Event State 的 reference storage mapping；哪些只是逻辑对象，哪些建议落成实际表？
16. `retention.py` 中的永久 history retention、measured-at TTL 与 catalog current TTL 分别如何执行？目前为什么没有 cleanup 和统一读取语义？

## 24. 最后说明

我并不是要求 PerceptKit 自带一套强制数据库、直接采集 iOS 数据，或者替所有 Agent runtime 做最终对话决策。

我要求的是：

> PerceptKit 应当拥有从标准上报数据到标准 Event 的完整通用语义，并通过清晰的 StoragePort 和 WakePort 把具体实现交给宿主。

宿主决定“怎么存”，但 PerceptKit 必须规定“需要存哪些逻辑事实、操作语义和一致性保证”。

iOS 决定“怎么采集和上传”，但 PerceptKit 必须规定“上传什么、字段如何解释、时间和修订如何表达”。

Agent runtime 决定“怎么运行和是否给用户说话”，但 PerceptKit 必须规定“什么 Event 被产生、为什么产生、如何可靠交付”。

当前 PerceptKit 已有的纯算法可以作为这个完整插件的基础，不需要推倒重来；但当前版本距离目标交付物还有明显的协议层、数据层、Event 层和接入层差距。下一步最重要的不是继续增加零散算法，而是先对齐并补齐这些核心边界。

## 附录 A：本次核验基线与代码证据

本反馈基于以下仓库状态进行静态核验：

```text
perceptkit
commit: 760b05b
tag: v0.1.0

feedling-mcp
commit: a2d8ed93
branch: main
```

以上只能说明这些 commit 的仓库事实，不自动证明某个线上环境已经部署了同一 commit。

### A.1 PerceptKit 自身定位

- `README.md:9-10`：明确声明不采集数据、不碰存储、不调模型，整个包是纯函数；
- `src/perceptkit/__init__.py:18-25`：明确把采集、存储、加解密、鉴权、调度和模型调用交给宿主；
- `pyproject.toml:5-18`：版本 `0.1.0`，运行时依赖为空；
- `src/perceptkit/catalog.py:24-46`：当前 Capability/Signal 声明字段；
- `src/perceptkit/catalog.py:94-169`：当前 20 个 iOS-key-oriented signal、输出字段和 Current TTL；
- `src/perceptkit/history.py:10-14`：明确把 storage、ingest hook 和 read endpoint 交给宿主；
- `src/perceptkit/history.py:26-72`：当前 history shape、signal 映射，以及 Location 只使用 `place_label` 聚合；
- `src/perceptkit/retention.py:1-3,17-56`：明确 retention 仅声明不执行，并列出 history retention 与 measured-at TTL；
- `src/perceptkit/wake.py:78-149`：当前变化判断、顺序判断和固定 wake 判断；
- `src/perceptkit/glance.py:179-231`：当前固定 wake facts 投影；
- `NOTES-packaging.md:41-91`：独立包从原宿主 feature worktree 抽取，并移除了宿主集成测试。

### A.2 原宿主当前链路

- `backend/perception/routes_asgi.py:39-97`：上报、snapshot、photo、items、app open/close 路由；
- `backend/perception/perception_read_core.py:41-97`：`context_snapshot/items/config` 多路 ingest；
- `backend/perception/ios_contract_v2.py:14-104`：当前 iOS operation/encrypted signal 分类；
- `backend/perception/service.py:302-434`：当前 encrypted report 解密和 storage/differ 分流；
- `backend/perception/resolve.py:121-238`：原宿主中仍然存在的 resolver 实现；其中 200-220 明确原始坐标只用于 geofence、BSSID 被丢弃，并输出 locality/country/Wi-Fi anchor；
- `backend/perception/service.py:494-608`：current 写入和 best-effort daily rollup；
- `backend/perception/store.py:22-152`：`perception_state` JSON blob 和 timestamp-guarded merge；
- `backend/perception/store.py:219-323`：`perception_items` 操作；
- `backend/perception/store.py:344-501`：wake audit、wake context 和最多 2,000 条的 App open/close streams；
- `backend/perception/store.py:527-583`：`perception_daily` 更新和读取；
- `backend/perception/signal_state_v2.py:141-351`：固定 wake signal 的 durable fingerprint baseline；
- `backend/perception/differ_v2.py:44-58`：当前固定 durable wake signal 集合；
- `backend/perception/differ_v2.py:166-234`：固定 signal 到固定 trigger 的映射；
- `backend/perception/ingress_v2.py:36-89`：Differ Event 到 `WakeEventV2`；
- `backend/proactive/runtime_v2.py:72-98`：当前 `WakeEventV2` 结构；
- `backend/perception/service.py:762-926`：wake control 及 Runtime V2/Resident 分叉；
- `backend/model_api_runtime/v2/worker.py:8853-8863`：bounded perception wake context 进入 runtime data。

### A.3 当前数据表证据

- `backend/alembic/versions/0001_baseline.py:40-45`：`user_blobs`；
- `backend/alembic/versions/0001_baseline.py:66-87`：`frame_envelopes` 和 `user_logs`；
- `backend/alembic/versions/0002_perception_items.py:31-44`：`perception_items`；
- `backend/alembic/versions/0004_perception_daily.py:26-36`：`perception_daily`；
- `backend/alembic/versions/0077_perception_signal_state_v2.py:17-28`：`perception_signal_state_v2`。

### A.4 外部包尚未接回宿主

原宿主当前仍存在并 import：

```text
backend/perception_kernel/
```

代表性引用包括：

- `backend/perception/differ_v2.py` import `perception_kernel.wake`；
- `backend/perception/history.py` re-export `perception_kernel.history`；
- `backend/perception/catalog.py` re-export `perception_kernel.catalog`；
- `backend/model_api_runtime/v2/worker.py` import `perception_kernel.prompts`；
- `tools/chat_resident_consumer.py` import `perception_kernel.prompts`。

当前宿主依赖和 lockfile 中没有发现外部 `perceptkit` package 接入记录。
