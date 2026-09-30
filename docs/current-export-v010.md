# v0.10 Current 与导出合同（开发中，未发布）

## Current：每个维度一条，只有一种公共形状

`PerceptionKit.get_current()` 与 `queries.get_current()` 返回
`dict[str, list[CurrentView]]`。所有请求的 signal 都有 key；未知信号、尚无数据、
`current_policy=none`（包括睡眠分段）返回 `[]`。

每条 entry 有 `signal`、`dimension_key`、`state`、`value`、`last_known`、`as_of`。
数组按 `dimension_key` 升序排列。TTL、availability、隐私字段过滤逐维度执行。
例如 home 已过期、office 仍新鲜，两条都返回，分别标为 stale / fresh；
stale / unavailable 的 `value=None`，最后可靠值和时间仍可用于解释。
删除后留下的 unavailable 维度记录仍作为 entry 返回，值为 None；没有记录才是 []。

```python
views = kit.get_current(subject_id=user_id, signals=["proximity_anchor"], now=now)
for entry in views["proximity_anchor"]:
    render(entry.dimension_key, entry.state, entry.value, entry.last_known, entry.as_of)
```

`get_last_known(subject_id=..., signal=...)` 同样返回 `list[CurrentView]`，
按维度排序、缺数据为 []，每条 state 为 last_known、value 为 None。
已知单维度的调用可以验证长度后取 `[0]`；多维信号必须迭代，不能挑最新一条。
StoragePort.get_current 已经是维度数组，其合同不变。没有 legacy API 或形状探测开关。

公开 `dimension_key` 的前提是 manifest 的公开身份能力声明：对
`current_policy="latest"`，每个 dimension_fields 引用必须唯一对应已声明字段，
该字段必须 `query_visibility="always"`，且 privacy_class 不得为 restricted。
never/on_demand 字段不能通过拼进 key 绕过隐私投影；也不通过普通 hash 伪匿名。
validate_manifest 返回明确错误，PerceptionKit 初始化立即拒绝，直接 query/export
入口及运行时替换 manifest 后的读取也执行同一检查。Storage raw dimension_key
仍属内部表示，只有满足该公开业务身份合同才允许作为公共 entry identity。

dimension_fields 当前复用两种角色：`latest` 是公共 Current 身份，`none` 仅用于
内部聚合分桶。睡眠 stage 属后者，保持 on_demand；所有 Current/last-known 返回 []，
所以内部 stage key 不公开。以后将 none 改为 latest 时，公开能力检查立即重新生效。

## Export：完整性、窗口与命名

`export_subject` 返回 JSON-compatible 对象。`per_signal_limit=None` 分页读到底；
正整数为每个命名集合上限。只有找到第 cap+1 个匹配项才算截断；恰好 cap 项完整。
`truncated` 是升序、不重复的集合名列表，空数组表示这些集合没有被调用方 cap 截断。
分页请求最多 500 行，只读取证明 cap+1 所需的剩余行；允许用一个空末页确认结束。
零、负数、布尔或非整数 cap 会抛出 ValueError。

已知边界：重复日历展开沿用 recurrence.MAX_INSTANCES=200，单一系列在大窗口内
可能超过这个内部上限；当前查询并不将该展开上限计入 truncated。因此即使
`per_signal_limit=None` 且 `truncated=[]`，也不能宣称日历所有重复实例无限完整。
本轮不扩修 recurrence；最终验收必须带出此遗留，后续用独立重复实例游标/完整性
合同处理。基础日历条目分页与调用方 cap 的完整性已按上述合同验证。

| 导出 key / 截断名字 | start/end 的含义 |
| --- | --- |
| observations[signal] / signal | occurred_at，双端包含 |
| daily_aggregates[signal] / daily_aggregates:signal | start.date() 到 end.date()，包含两个本地日期；不转成 UTC 日期 |
| aggregate_generations[signal] / aggregate_generations:signal | requested coverage 与 start.date()/end.date() 有交集；包括没有任何 aggregate row 的失败尝试 |
| calendar_events | 沿用日历查询窗口及重复事件展开语义；参考存储按 start_at 筛选，时间不明的条目保留 |
| reminders | 不适用；无统一发生时间，包含已完成项 |
| events | occurred_at，双端包含；全部 9 种投递状态 |
| conflicts | created_at，即候选被隔离的检测时间，双端包含；保留原始候选/解决证据 |
| current[signal] | 当前投影，不是历史快照；保留全部维度的数组，空信号省略；窗口不裁掉维度，end 仅作为新鲜度判断时间 |

v0.10 将误称为 pending_events 的集合正式改名为 **events**；没有两个别名。
未提供 end 时，导出沿用固定远期时间判断 Current，新鲜度不表示导出时的实时时钟。
无 start 或 end 的对应时间边界开放。日聚合是**版本化审计**：每条包含
date、value、aggregation_version、generation_id、completeness 和
incomplete_reasons，不合并版本，也不声称是普通查询选中的活动版本；
活动 generation 的发布/选择见 `aggregate-generations-and-definition-history.md`。
普通 get_daily/get_trend 只读明确 active generation，不是审计查询。
`aggregate_generations` 是独立分页的尝试级审计，包含 requested coverage、
status、completeness、accounted/incomplete dates、reasons、failure 和 timestamps；
它与 `daily_aggregates` 分别应用 cap 并分别报告截断。

Kit 导出仍标记 `kit_managed_only=True`；Host 自己的载荷、加密信封、Runtime 业务数据
由 Host 合并导出。不要把这份包级导出当成整个产品的数据导出。并发导出快照一致性
由 Host 的读取事务提供；offset 分页要求在同一稳定数据视图中运行。

## Host 迁移清单（IO / Rokku 同批切换）

1. 查询工具、Agent context、序列化 DTO、UI/render、缓存类型从 signal->object
   切换到 signal->entries[]，按 dimension_key 使用；get_last_known 消费者也迁移数组。
2. 导出读取 current[signal] 的数组；将 pending_events 键和截断名改为 events。
3. StoragePort.list_conflicts 增加 start/end（created_at）及 limit=None/offset=0。
   排序固定为 created_at、conflict_id；过滤/分页在数据库执行。
4. StoragePort.get_aggregate 增加 limit=None/offset=0；排序固定为
   local_date、aggregation_kind、aggregation_version；日期过滤在分页之前。
   默认不传 limit 的旧完整读取语义不变。
5. 数据导出采用稳定事务快照；用实际数据库验证分页无重复、无遗漏和 subject 隔离。
6. StoragePort.list_aggregate_generations 增加 coverage-overlap 窗口和稳定 limit/offset；
   导出必须包含 failed/zero-row attempts，不能从 aggregate rows 反推 generation audit。
7. package 版本、依赖锁、发布产物由发布任务统一处理；本次没有改版本或发布。

库内已迁移的可执行消费者：examples/end_to_end.py，以及 queries、definitions_0_8、
end_to_end、ios_fixture、isolation、edge_cases、regressions、acceptance_regressions_0_9、
projection_atomicity、reselect_quality、report_fact_identity、revision_recompute、
failure_recovery、storage_concurrency_contract 与 export 测试。

2026-09-28 只读核对的 Host 本地远端引用快照（未 fetch，Task 7/8 开始时重新核对）：

| Host / 引用 | 具体迁移点 |
| --- | --- |
| IO origin/test `af23b44bf91777e2d1c130f815215bd5b2c38454` | tests/test_perceptkit_postgres.py:882、892 的公共 Current 直接取 `.value`，改用数组消费 |
| 同上 | backend/perception/perceptkit_adapter/storage.py:338 的 get_aggregate 增加有序分页；list_conflicts 随 6A 新增时支持窗口/分页 |
| 同上 | backend/perception/perceptkit_adapter/readback.py:102 的 max(rows) 是 storage 多维到旧字段的扁平化，Task 7 按实际支持信号审计；shadow.py:207/276 和 service.py:1455 走 storage 数组合同，不能误当公共 API 调用机械修改 |
| Rokku origin/test `1b81aa56e6fbfbbf60d2e607552352ef78d214b3` | backend/rokku/persistence/perception_store.py:319 的 get_aggregate 增加有序分页；list_conflicts 随 6A 新增时支持窗口/分页；本次静态搜索未找到直接公共 Kit Current/last-known/export 消费 |
