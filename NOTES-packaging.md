# sensegate 打包笔记

镜像对象：`/tmp/mg-peek`（memgarden，同一个团队更早做的一次同类抽取）。
布局、`pyproject.toml` 写法、`__init__.py` 语气、守卫测试风格都照它来。

## 1. 依赖验证

`src/sensegate/*.py` 的全部 import（AST 扫描，排除包自身）：

```
$ python3 - <<'EOF'
import ast, pathlib, sys
SRC = pathlib.Path("src/sensegate")
stdlib = set(sys.stdlib_module_names)
...
EOF
all imports: ['__future__', 'collections', 'dataclasses', 'datetime', 'hashlib', 'json', 'math', 'sys', 'typing', 'zoneinfo']
non-stdlib (excluding self): set()
```

零第三方依赖，全部标准库。`pyproject.toml` 的 `dependencies = []`。

## 2. pyproject.toml

照 memgarden 的写法：hatchling、`src/` 布局、`license = { file = "LICENSE" }`、
`requires-python = ">=3.10"`、`[tool.pytest.ini_options] testpaths = ["tests"]`。
唯一实质差异：memgarden 有一个允许的同源第三方依赖
（`agent-protocol-core`），这个包没有等价的同源伙伴包，`dependencies = []`
是真正的空。

## 3. `src/sensegate/__init__.py`

重写成 memgarden 的语气：先说包判断什么，再列一段"不在这里的"（数据采集、
存储、加解密、账号身份与鉴权、定时器/调度、真正调模型、决定 agent 最终该
说什么），最后 re-export 十二个模块里宿主大概率会直接用到的名字（
`should_wake`、`build_perception_glance`、`classify`、`measurement_key` 等，
完整列表见 `__all__`）。

## 4. 迁移测试：改动前后

**改动前**（host 仓库 `/Users/hx/Projects/io/worktrees/feedling-mcp/feat-perception-health-chain/tests/`，
10 个 `test_perception_kernel_*.py` 文件）：

```
grep -c '^    def test_\|^def test_' 逐文件相加 = 95   （parametrize 只按函数定义算一次）
```

按 pytest 实际会收集的用例数手工展开 parametrize（未在此仓库跑 pytest —— host
仓库的 conftest 会尝试连 Postgres，用手工核对参数化列表长度代替）：

```
test_identity.py     test_missing_any_part_is_refused        1 def -> 4 cases（kwargs 列表长度 4）      +3
test_wake.py          test_deny_listed_signals_are_never_wake_worthy  1 def -> 5 cases（NOT_WAKE_WORTHY_SIGNALS 长度 5）  +4
test_wake.py          test_durable_wake_signals_default_allow         1 def -> 7 cases（手写信号列表长度 7）              +6
```

**改动前实际总数：95 + 3 + 4 + 6 = 108**

**改动后**（`sensegate/tests/`，`uv run python3 -m pytest --collect-only -q`）：

```
106 tests collected in 0.02s
```

差值 108 → 106 的构成：

- **删除 6 条宿主集成测试**（它们断言宿主的 re-export 壳和内核对象是同一批
  对象，或直接读/扫宿主仓库的源码——这个独立包里既没有宿主壳、也没有宿主
  仓库目录，留着要么 ImportError 要么变成"扫空目录、永远绿"的假测试）：
  - `test_catalog.py::test_io_shell_reexports_the_same_objects`
  - `test_projection.py::test_io_shells_reexport_kernel_objects`
  - `test_wake.py::test_kernel_vocabulary_does_not_collide_with_the_two_io_wake_kind_sets`
    （硬编码了宿主另外两套 wake_kind 常量，纯粹比对宿主内部事实）
  - `test_wake.py::test_wake_unwired_names_stay_unreferenced_outside_the_kernel`
    （扫宿主仓库 `backend/`、`tools/` 目录；这里没有这两个目录，会静默
    "扫空、永远绿"，是比失败更危险的假测试）
  - `test_wake.py::test_every_real_durable_wake_signal_is_wake_worthy`
    （直接 `read_text()` 宿主的 `perception/differ_v2.py` 源码核对信号名单，
    这个文件不存在于本仓库）
  - `test_purity.py`（宿主版本本身就是扫宿主 `backend/`+`tools/` 目录的，
    不适合直接照搬——用它的思路重写了一份，见下）
- **新增 4 条**：`test_purity.py`（3 条）+ `test_no_host_leakage.py`（1 条）

`108 - 6 + 4 = 106`，与实际收集数一致。

文件改名对照：`test_perception_kernel_X.py` -> `test_X.py`；`catalog`/`projection`
两个文件里删掉了宿主集成用例后保留文件名不变（`projection` 原本就是
`fields.py` + `glance.py` 两个模块的联合测试，没有对应单模块名可用）。
所有文件里的手写 `sys.path` bootstrap 全部删除，`import perception_kernel.X`
/`from perception_kernel import X` 全部改成 `import sensegate.X` /
`from sensegate import X`。

## 5. `tests/test_purity.py`

照抄 memgarden 版本的结构（AST 扫描 `src/sensegate` 下每个文件的顶层
import），**唯一差异是允许名单是空集**（memgarden 允许同源的
`agent-protocol-core`，这个包没有等价的伙伴包）。三条测试：源码树非空
（护住扫描路径没写错）、无第三方 import、判断内核不碰网络/DB/进程/线程。

## 6. `tests/test_no_host_leakage.py`

选词标准：**只挑结构性的、不含糊的标识符**，不禁常见词。具体选了这些：

| 词 | 为什么 |
|---|---|
| `perception_kernel` | 宿主迁移前的内部包名——留着就是没洗干净的直接证据 |
| `io_cli` | 宿主内部 CLI 工具名 |
| `chat_resident_consumer` | 宿主 VPS 自托管常驻进程的模块名 |
| `differ_v2` / `signal_state_v2` / `effect_outbox` / `tool_executor_v2` | 宿主运行时内部模块名 |
| `model_api_runtime` | 宿主的运行时 lane 名——这个包要兼容多条宿主 runtime，写死一条的名字就是假设了宿主架构 |
| `OpenClaw` | 宿主生态里的第三方插件名 |
| `backend/*.py` / `tools/chat_resident_consumer.py` | 宿主仓库的目录布局，这个包不该假设自己在哪个宿主仓库下 |
| `perception_daily` | 宿主数据库的实际表名——这个包不碰存储，不该知道表叫什么 |
| `usr_[0-9a-f]{8,}` | 真实用户 id 的形状（照抄 memgarden） |
| `ADMIN_KEY` | 凭据名（照抄 memgarden） |

**刻意没禁**裸的 `io`（宿主产品名）：只有两个字母，是标准库模块名
（`import io`）的合法用法，也是大量英文缩写/变量名的子串，禁了假阳性远大于
真阳性；用更具体的标识符（内部模块名、插件名、表名、目录）来兜底同一个
"产品名泄漏"担忧，风险小得多。

扫描范围只覆盖 `src/`、`README.md`、`pyproject.toml`，**刻意不扫
`tests/`**——这条测试自己就活在 `tests/` 里，它的 `BANNED` 正则字面量本身
含有这些词，扫自己会自证失败（踩过一次：第一版把 `tests/` 也扫进去，
测试文件里解释"删掉了什么"的说明文字被自己的正则命中）。

## 7. 一个意外发现：`src/sensegate/*.py`（"已经就位"的文件）本身就在泄漏

写完 leakage 测试、第一次跑之前先手动 grep 了一遍 `src/`（不是等测试跑起来
才发现），结果是：这批已经放在 `src/sensegate/` 里的源码**本身携带大量
宿主内部细节**，集中在 `wake.py`、`prompts.py`、`fields.py`、`catalog.py`、
`history.py` 五个文件的注释/docstring 里：

- 宿主内部文件路径 + 行号（如 `proactive/gate.py:89`）
- 宿主内部函数名/常量名（如 `_proactive_v2_wake_kind`、`_COLLISION_WAKE_KINDS`、
  各种 `SWITCH_*_WAKE_ENABLED` 开关名）
- 宿主运行时 lane 名（`model_api_runtime/v2/...`）
- 宿主内部模块名（`differ_v2.py`、`chat_resident_consumer.py`、
  `signal_state_v2.py`、`backend/capabilities/tool_schema.py`）
- 宿主内部工具名（`io_cli`）与生态插件名（`OpenClaw`）
- 宿主产品名的直接提及（`IO'S OWN foreground/background/inactive phase`）
- 宿主数据库表名（`perception_daily`）与技术选型（Postgres）
- 一处真人名字（"hx 拍板"，指代团队里的决策者）
- 一处指向宿主仓库内部测试 fixture 的路径断言
  （`tests/fixtures/perception_kernel/prompt_baseline.json`，该路径在这个
  独立包里根本不存在，留着是死断言）

这些注释本身的**设计理由是有价值的**（比如为什么 wake 源要单独起名、为什么
默认允许+否决名单而非白名单），所以没有整段删掉，而是改写成不点名宿主内部
结构的版本，保留"为什么这么设计"，去掉"宿主哪个文件哪一行"。逐文件改动：
`wake.py`（改动最大）、`catalog.py`、`fields.py`、`prompts.py`、`history.py`。

这不是"任务外顺手改了别的东西"——这批文件被判定为"标准做法就是照抄测试
规范，然后让测试盯着"，而写完的 `test_no_host_leakage.py` 本该、也确实
应该抓到这个问题；不修就没法让 `pytest` 通过，也违背了整个任务"证明这个
包能独立发布"的前提。

## 8. 运行方式

用 `uv`（`/tmp/mg-peek` 的 `[tool.uv.sources]` 暗示这个团队的约定，且本机
已装 `uv 0.11.7`）：

```
$ uv sync --extra dev
Resolved 10 packages in 484ms
Building sensegate @ file:///Users/hx/Projects/sensegate
Installed 6 packages: iniconfig, packaging, pluggy, pygments, pytest, sensegate

$ uv run python3 -m pytest -q
........................................................................ [ 67%]
..................................                                       [100%]
106 passed in 0.06s
```

也验证了不带 `uv run`、只激活 `.venv` 后跑纯 `python3 -m pytest` 同样通过
（`uv sync` 把包装成 editable install，`sensegate` 在 sys.path 上）：

```
$ source .venv/bin/activate
$ python3 -m pytest -q
106 passed in 0.06s
```

（不激活任何环境、直接用系统 `python3 -m pytest` 会因为 `sensegate` 没装
而 `ModuleNotFoundError`——这是预期的：测试文件里的手写 `sys.path` bootstrap
已按任务要求删除，proper packaging 意味着"先装包，再测"，不是"从任意
目录躲开安装步骤"。）

## 9. 其他留意点

- `examples/` 目录在任务开始前就已存在但是空的，未处理（任务没要求写
  quickstart 示例；memgarden 有一个 `examples/quickstart.py`，这个包目前
  没有）。
- README.md 是本次新写的（任务清单没明确列出，但 `pyproject.toml` 的
  `readme = "README.md"` 字段引用它，`uv sync` 会因为它存在而不报错；
  没写的话仍能跑 pytest，但 `hatchling` 打包会失败）。内容参照 memgarden
  README 的结构（做什么/不做什么 -> 最小代码 -> 目录），但篇幅大幅压缩。
