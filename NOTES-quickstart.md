# NOTES — README + quickstart 收尾

记录本次改动实际跑过的命令和真实输出（不是转述，是复制粘贴）。

## 1. 现状勘察

```
$ cat pyproject.toml
```
关键字段：`name = "sensegate"`、`version = "0.1.0"`、`dependencies = []`、
无 PyPI 发布相关配置 —— 确认"还没发布到 PyPI"为真，README 未沿用旧文案
里的 `pip install sensegate`（那条装不到东西）。

参照 `/tmp/mg-peek`（memgarden 的 checkout）的 `README.md` 与
`examples/quickstart.py`，照其结构和"先说不是什么"的开场方式改写。

## 2. examples/quickstart.py 语法与行为验证

```
$ python3 -c "import ast; ast.parse(open('examples/quickstart.py').read())"
```
第一次运行报错（第 30 行字符串里嵌了未转义的英文直引号导致提前截断字符串）：
```
SyntaxError: invalid syntax. Is this intended to be part of the string?
```
改成中文书名号「」后：
```
$ python3 -c "import ast; ast.parse(open('examples/quickstart.py').read())" && echo SYNTAX_OK
SYNTAX_OK
```

跑通产品路径（这是 README 里教用户跑的命令）：

```
$ uv run python3 examples/quickstart.py
① 一条测量：先分清「有值」「测到是零」「没测到」「不可用」四态
     昨晚(有数据) -> observed
     前晚(没戴表) -> no_observation  （不能当成 0 分钟，也不能当成没发生过）
     权限关闭那几天 -> unavailable  （不可采信，不参与任何判断）

② 一条测量该记在哪一天：睡眠区间整体归「醒来」那天
     23:10 睡下、06:15 醒来 -> 记在 2026-08-23

③ 到了常去的地方：这次变化值不值得戳一下 agent
     第一次到达 -> 戳醒=True  原因=arrival
     30 秒后同一次到达(GPS 抖动) -> 戳醒=False  原因=debounced  （被防抖拦下）

④ 连续偏离：只在「从正常跨入异常」那一刻叫一次，不许反复叫
     当前连续偏离天数 -> 3
     达到 3 天阈值 -> fire=True  reason=streak_reached
     第 4 晚仍不好(已经叫过) -> fire=False  reason=already_firing
     第 5 晚睡好了 -> fire=False  reason=recovered  （锁已打开，下次再连续 3 天会重新叫）

⑤ 此刻的状况压成一份摘要 —— 只有 True/False，没有一个具体数字
     {'location': {'available': True, 'notable_change': True}, 'health': {'available': True, 'notable_change': True}}
     （模型读到这份摘要，只知道「地点变了」「健康数据有显著变化」——
      想知道具体睡了多久、变化多大，得自己去查工具，而不是被喂进来。）

⑥ 同一件事，两种指标要用两种读法
     health_sleep 的模型 -> fluctuating
     平时(中位数) 295.0 分钟，今天 180.0 分钟，偏离 -115.0（down）

     health_body 的模型 -> drifting
     ✗ 用波动型的读法(跟中位数比) -> 「比平时轻了 14.0 公斤」  —— 这个人根本没有一个叫「平时」的体重
     ✓ 用漂移型的读法(看方向和速率) -> 一年内共 -20.0 公斤，平均每月 -1.668 公斤，还在加速=True

⑦ 最后：这份摘要该怎么讲给模型听（说明书是库给的，话不是库替它说的）
     This is a low-resolution glance, not a list of things to report. ...

     Reading the board: each domain (location/media/app/health/weather/mood/reminders/calendar/photos/screen) ...

跑通了：没有网络、没有 API key、没有数据库——库只回答判断，
戳醒之后 agent 继续睡 / 只看一眼 / 开口说话，这三选一从来不是它管的事。
```
（长文本段落在此省略中间部分，完整文本见 `src/sensegate/prompts.py` 的
`V1_GLANCE_HOWTO` / `V1_BOARD_HOWTO`，实际运行时是全文打印的。）

验证"裸 python3 找不到包"（README 里明确写了这个预期行为）：

```
$ python3 examples/quickstart.py
Traceback (most recent call last):
  ...
ModuleNotFoundError: No module named 'sensegate'
```

## 3. 新增测试

`tests/test_examples.py`：用 `subprocess.run([sys.executable, quickstart路径])`
跑一遍 quickstart，断言 `returncode == 0`、stdout 含"跑通了"、且 ①~⑦
七个步骤标记都出现过（不是提前 return 或跑了一半）。用 `sys.executable`
而不是裸 `python3`，避免"pytest 所在环境能跑、用户 README 里的命令跑不动"
这种分裂。

## 4. 全量测试

```
$ uv run pytest -q
........................................................................ [ 67%]
...................................                                      [100%]
107 passed in 0.09s

$ uv run pytest --collect-only -q | tail -3
tests/test_wake.py::test_durable_wake_signals_default_allow[photo_added]
tests/test_wake.py::test_durable_wake_signals_default_allow[broadcast_state]
tests/test_wake.py::test_unknown_signal_name_defaults_to_allow

107 tests collected in 0.02s
```
基线是 106（本次改动前，见 git log 上一个 commit 状态），新增 1 条
（`test_quickstart_runs_cleanly_end_to_end`），collect 数与 run 数一致，
没有被 conftest 的 `_PURE_UNIT` 白名单坑到（本仓库没有这类白名单机制，
`testpaths = ["tests"]` 直接全量收集）。

## 5. 远程仓库确认

```
$ git remote -v
origin  git@github.com:teleport-computer/sensegate.git (fetch/push)

$ git ls-remote origin
(空 — 远程仓库目前没有任何 ref，是全新的空仓库)

$ gh repo view teleport-computer/sensegate --json isPrivate,name,url
{"isPrivate":true,"name":"sensegate","url":"https://github.com/teleport-computer/sensegate"}
```
确认私有、确认远程为空（本地 `main` 尚未推送过）。

## 6. 提交与推送

```
$ git commit -m "Rewrite README and add a runnable quickstart example"
[main 45dae76] Rewrite README and add a runnable quickstart example
 4 files changed, 438 insertions(+), 33 deletions(-)
 create mode 100644 NOTES-quickstart.md
 create mode 100644 examples/quickstart.py
 create mode 100644 tests/test_examples.py

$ git push -u origin main
To github.com:teleport-computer/sensegate.git
 * [new branch]      main -> main
branch 'main' set up to track 'origin/main'.
```
远程之前完全是空仓库（无任何 ref），这次推送是第一次把 `main` 推上去。
仓库可见性未改动，仍是 `teleport-computer/sensegate` 下的私有仓库。
