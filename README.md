# perceptkit

**从设备信号到"值不值得戳一下 agent"的判断力。**

给它当下的信号快照（位置、健康数据、屏幕状态、日历……），它告诉你：
有没有发生什么值得留意的事、这算不算个值得戳一下 agent 的时刻、
以及该怎么把"此刻"这件事讲给模型听。

它**不采集数据、不碰存储、不调模型** —— 那些是宿主的事。
整个包是纯函数，零第三方依赖。

**现状：还没发布到 PyPI**，`pip install perceptkit` 现在装不到东西。
能确认好用的两条路：

```bash
git clone git@github.com:teleport-computer/perceptkit.git
cd perceptkit
uv run pytest                        # 107 条测试
uv run python3 examples/quickstart.py   # 一分钟看它怎么判断
```

想在别的项目里依赖它（这是个私有仓库，需要你本来就有访问权限）：

```toml
# 另一个项目的 pyproject.toml
perceptkit = { git = "ssh://git@github.com/teleport-computer/perceptkit.git" }
```

或者本地路径依赖（两个仓库在同一台机器上时）：

```bash
uv add --editable ../perceptkit
```

---

## 一、它跟这几类东西不是一回事

```
地理围栏 / 系统事件检测 SDK   它们负责"测到了什么"（到没到某个地点、
（Radar、CoreLocation region      解没解锁、屏幕有没有变化）；这个包接手
monitoring、系统健康/屏幕时间框架）  的是下一步——"测到的这个变化，够不够
                                  资格戳一下 agent"，它自己不做检测。

推送时机优化 / 用户参与度引擎     它们的目标函数是"提高打开率/参与度"；
（send-time optimization、          这个包没有目标函数，只回答一道是非题：
engagement 引擎）                    值不值得，不掺杂"怎么让人多点开"。

健康看板 / Quantified-Self App    它们的核心产品就是把数字摆给你看
（Apple 健康、Oura、WHOOP 这类）    （心率曲线、睡眠时长）；这个包的 glance
                                  刻意反过来——从不吐出一个数字。

主动性 agent 运行时 / 编排框架    "戳醒之后该不该开口、开口说什么"，这些
                                  是宿主运行时的职责；这个包只回答到
                                  "值不值得戳"为止，一步都不往前多走。
```

它是**要不要打扰的判断力**，不是感知系统、不是增长引擎、不是健康 App、
也不是对话运行时。

---

## 二、一分钟跑通

```bash
uv run python3 examples/quickstart.py
```

不联网、不需要 API key、不碰任何存储。走一个具体场景：老王最近几晚睡得
不好，此刻手机检测到他到了常去的"公司"——从一条原始测量，一路走到
"该怎么把此刻讲给模型听"。

> 注意：直接 `python3 examples/quickstart.py`（不经过 `uv run`）会报
> `ModuleNotFoundError: No module named 'perceptkit'`——这是预期的，本包
> 是 src-layout，没有发布安装包时只能通过 `uv run`（或先 `uv sync`/
> `pip install -e .`）让解释器找到它。

最小代码：

```python
from perceptkit.observation import classify
from perceptkit.wake import should_wake
from perceptkit.glance import build_perception_glance

# 一条测量：有值、有观测到零值、没测到、还是不可用？四态分开判断，
# 不能拿"没测到"直接当"测到是 0"，两者会喂出截然不同的错误结论。
state = classify(180.0)   # -> "observed"

# 这次到达值不值得戳一下 agent（30 秒内的第二次到达会被防抖拦下）
ok, reason = should_wake(
    "arrival", enabled_sources=("arrival",),
    last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0,
)

# 此刻的状况压成一份摘要——只有 True/False，没有一个具体数字
glance = build_perception_glance({"location": {"place_label": "公司"}})
```

---

## 三、两个真正的关键判断

### 1. 戳醒 ≠ 该开口了

戳醒之后，agent 继续睡 / 只看一眼 / 开口说话，是三个平行且同等合法的
结局。这个包只回答"值不值得戳一下"，不参与"戳完之后该怎么办"，
也不产出任何"该说话了"式的措辞。

```
之前（如果 wake 直接等于开口）
  用户到公司 → 库判定"该说话" → agent 不管场合硬接一句"你到公司啦"
  → 用户在开会，手机弹了句废话

之后（这个包的实际语义）
  用户到公司 → should_wake() 只回答"这次到达值得关注"
  → 具体开不开口、说不说话，完全交给宿主运行时按当下场合自己判断
  （库的返回值里连"建议怎么说"这种字段都没有——不是没写全，是故意不给）
```

### 2. 摘要只出布尔值，从不出数字

```
之前（把数字直接喂给模型）
  "你昨晚睡了 5 小时 12 分钟"
  → 模型像念体检报告一样把这句话复述给用户，而不是自己判断要不要在意

之后（build_perception_glance 的实际产出）
  {"health": {"available": True, "notable_change": True}}
  → 模型自己决定"睡眠有显著变化"值不值得多看一眼；
    值得的话，它主动去查工具拿具体数字——这时候数字才是"被用"，
    而不是"被念出来"
```

四个信号态（`observation.classify`）、防抖与开关（`wake.should_wake`）、
连续偏离的迟滞判断（`streaks.should_trigger`）、按指标类型选对趋势读法
（`trend_models.model_for`）——都是在为这份摘要和这次判断兜底，细节见
下面目录里对应模块的模块级文档字符串。

---

## 四、库替你做的判断，与不替你做的判断

```
在库里    这个信号有哪些字段、多久算新鲜(catalog) · agent 能看哪些字段、
          该不该给(fields) · 此刻是否有事发生，只出 bool(glance) ·
          按天汇总、看出什么趋势变化(history) · 该怎么把这些讲给模型(prompts) ·
          这次变化值不值得叫醒一次 agent(wake) · 一条测量到底是"有观测到零值"
          还是"压根没测到"(observation) · 一条测量该记在本地日历的哪一天
          (attribution) · "连续 N 天"怎么断续、怎么只在跨入异常时触发一次
          (streaks) · 三种趋势判读方式：波动/漂移/周期性(trend_models) ·
          每类信号的历史该留多久(retention) · 一条测量的去重键怎么造
          (identity)

不在库里  采集数据 · 存储 · 加解密 · 账号身份与鉴权 · 定时器/调度 ·
          真正调模型 · 叫醒之后该说什么话
```

---

## 五、目录

```
src/perceptkit/
  catalog.py       信号有哪些字段、多久算新鲜
  fields.py        agent 能看哪些字段 + 权限判断
  glance.py        此刻的纯 bool 摘要
  history.py       按天汇总、显著变化
  prompts.py       该怎么把这些讲给模型
  wake.py          值不值得叫醒一次 agent
  observation.py   四态：observed / observed-zero / not-observed / unavailable
  attribution.py   一条测量归本地日历的哪一天
  streaks.py       "连续 N 天"的可执行定义
  trend_models.py  三种趋势判读方式：波动/漂移/周期性
  retention.py     每类信号的历史留多久
  identity.py      一条测量的去重键
```

## 六、现状

107 条测试全绿（含 `test_purity.py` 的 AST 扫描：本包只依赖标准库，
以及 `test_no_host_leakage.py`：不带宿主内部痕迹）。`examples/quickstart.py`
本身也有一条测试盯着（`tests/test_examples.py`）——它必须一直能跑，
不能悄悄烂掉。

## 许可

Apache-2.0
