# sensegate

**从设备信号到"值不值得叫醒一次 agent"的判断力。**

给它当下的信号快照(位置、健康数据、屏幕状态、日历……),它告诉你:
有没有发生什么值得留意的事、这算不算个值得戳一下 agent 的时刻、
以及该怎么把"此刻"这件事讲给模型听。

它**不采集数据、不碰存储、不调模型** —— 那些是宿主的事。
整个包是纯函数,零第三方依赖。

```bash
pip install sensegate
```

---

## 一、库替你做的判断,与不替你做的判断

```
在库里    这个信号有哪些字段、多久算新鲜(catalog) · agent 能看哪些字段、
          该不该给(fields) · 此刻是否有事发生,只出 bool(glance) ·
          按天汇总、看出什么趋势变化(history) · 该怎么把这些讲给模型(prompts) ·
          这次变化值不值得叫醒一次 agent(wake) · 一条测量到底是"有观测到零值"
          还是"压根没测到"(observation) · 一条测量该记在本地日历的哪一天
          (attribution) · "连续 N 天"怎么断续、怎么只在跨入异常时触发一次
          (streaks) · 三种趋势判读方式:波动/漂移/周期性(trend_models) ·
          每类信号的历史该留多久(retention) · 一条测量的去重键怎么造
          (identity)

不在库里  采集数据 · 存储 · 加解密 · 账号身份与鉴权 · 定时器/调度 ·
          真正调模型 · 叫醒之后该说什么话
```

**wake ≠ 该开口了。** 戳醒之后继续睡 / 只看一眼 / 开口说话是三个平行选项,
这个包只回答"值不值得戳一下",不参与那个决定,也不产出任何"该说话了"式的措辞。

---

## 二、最小代码

```python
from sensegate.observation import classify, OBSERVED_ZERO
from sensegate.wake import is_wake_worthy_signal, should_wake
from sensegate.glance import build_perception_glance

# 一条测量:有值、有观测到零值、没测到、还是不可用?四态分开判断
state = classify(0.0, source_reported_zero=True)   # -> OBSERVED_ZERO

# 这个信号的变化值不值得叫醒一次 agent
if is_wake_worthy_signal("unlock_after_absence"):
    ...

# 此刻的状况,压成一份只含 bool 的摘要 —— 不泄露具体数值
glance = build_perception_glance({"location": {"place_label": {"v": "office"}}})
```

---

## 三、目录

```
src/sensegate/
  catalog.py       信号有哪些字段、多久算新鲜
  fields.py        agent 能看哪些字段 + 权限判断
  glance.py        此刻的纯 bool 摘要
  history.py       按天汇总、显著变化
  prompts.py       该怎么把这些讲给模型
  wake.py          值不值得叫醒一次 agent
  observation.py   四态:observed / observed-zero / not-observed / unavailable
  attribution.py   一条测量归本地日历的哪一天
  streaks.py       "连续 N 天"的可执行定义
  trend_models.py  三种趋势判读方式
  retention.py     每类信号的历史留多久
  identity.py       一条测量的去重键
```

## 许可

Apache-2.0
