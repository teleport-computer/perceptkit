# 变更记录

## 0.2.1 — 未发布

### 新增

- **conformance 多了第 ⑪ 条：两个来源镜像都要能往返。**
  这一条是从一个真实现上倒推出来的 —— 宿主的 Postgres adapter 写提醒时读了
  `r.source_created_at`、读回来时又把它当构造参数传回去，而
  `ReminderItemMirror` 上根本没有这个字段（`CalendarEventMirror` 有，
  它是照那个写的）。**写会抛、读也会抛，整条提醒镜像从来没通过过一次**，
  而这套套件全绿。

  原因是 ⑧ 一直在用日历，所以日历那半始终被覆盖着，提醒那半一次都没被碰过。
  **一个只测一半的套件，给出的是「都测过了」的印象。**


## 0.2.0 — 2026-08-31

**这一版有破坏性变更。** 宿主升级时要改 import，见下。

### 破坏性

- **纯计算的八个模块搬进 `perceptkit.algorithms`**
  （`attribution` / `glance` / `history` / `identity` / `observation` /
  `streaks` / `trend_models` / `wake`）。

  ```python
  # 之前
  from perceptkit import trend_models
  from perceptkit.history import ...

  # 之后
  from perceptkit.algorithms import trend_models
  from perceptkit.algorithms.history import ...
  ```

  产品规范 §18 要求这一层单独存在（「不能再把 contract、算法、存储、
  宿主 runtime 接线混成一层」）。先前它们和 `kit.py`（装配和接线）平级。

  `catalog` / `fields` / `retention` / `prompts` **留在顶层没动** ——
  它们是声明表和待定项，不是算法。

- **`list_events` 改为返回 `(事件, 下一页游标)`**，不再是一个列表。
  产品规范 §15 要求所有 list 查询分页或有明确上限。

### 新增

- `list_definitions()` —— 当前配了哪些规则（规范 §15 列了它，先前完全没有）。
- `list_events(status=...)` —— 按投递状态筛。「为什么没提醒我」的答案
  往往是 suppressed 或 rejected，不是 pending。
- `export_subject()` —— 按人导出。规范 §8 要的是「定位、导出、删除」，
  导出那一半先前是空的。
- `recompute_aggregates()` —— 聚合算法升级后重算历史。**默认拒绝重算
  明细可能已被保留期清理的日子**：拿残缺明细折出来的永久统计会错一个
  数量级，而且旧值已被覆盖。
- 重复日程按查询窗口滚动展开（确定的子集；不认识的规则明确拒绝，不猜日期）。
- `run_wake_conformance()` / `run_report_conformance()` —— 规范 §20 并列的
  三种 adapter conformance，先前只有 storage 那一套。
- manifest 的第五条自动检查：投影不漂移。它抓的一类是泄漏 ——
  声明了「永不给 agent」的字段如果 wake_eligible，前后值会随事件信封
  存下来、投出去、进模型上下文。
- manifest 拆出 `aggregate_retention_days`：明细和聚合是两个保留期。
- 新增信号：`proximity_anchor` / `music_playback` / `app_usage`。
- 事件信封的 `context` 带上触发字段的单位。

### 修复

- **同一个信号里两种聚合算法会互相覆盖，当天第二条上报直接崩。**
  聚合分派把**整条 payload** 递给了每个字段的 merger，于是一个字段声明的
  算法写到了所有字段头上。`health_vitals` 同时有 `numeric_dist`（静息心率）
  和 `main_of_day`（vo2_max）：后者把字段写成裸数字，前者下一条进来读
  `cell["min"]` 抛 `AttributeError` —— **每个用户每天第二次上报都会踩**。
  同一个原因还让声明 `none` 的字段凭空长出聚合（`weather` 只声明了
  `temperature_c`，紫外线、湿度、体感温度全被写了 min/max/sum/count）。
  现在每个 merger 只拿到它自己那个字段。

  没被单测抓到，是因为所有单测用的信号都只声明了一种算法；影子第一次接
  真数据当天就炸了。

- **`unavailable` 现在会写进当前值。** 先前撤销权限后查询仍报 `fresh` ——
  把一个已经读不到的值当成当前事实。现在状态记下来、最后可靠值留作
  `last_known`。
- **设备时钟明显错误的判据**：超过 24 小时的未来时间拒收（过去不限，
  离线补传是正常的）。先前没有任何判据，手机时间设成明年会把今天的数据
  写进明年，不报错。
- **事件 `context` 的白名单和长度上限真的实现了** —— 先前只写在文档里。
- **日历/提醒的读取走端口**，不再摸具体实现的私有属性（那会让任何真实
  存储静默返回空）。
- **一致性套件自己不再摸内存实现的内部** —— 先前它对每个真 adapter
  都报一个假失败。
- 接受规范 §7.1 用的字段名 `state` / `sample_id` 作为别名。照那份文档
  实现的 producer 先前每一条观测都被拒。

## 0.1.0

首个公开版本：判断内核（纯函数）。
