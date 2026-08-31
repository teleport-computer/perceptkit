"""Report adapter 的一致性检查 —— 宿主用它证明自己的上报适配器产出的信封是对的。

产品规范 §20 并列的第三种 conformance。适配器指的是把 producer 的原始载荷
（iOS 的一份快照、某个手环的一批样本）转成 ``ReportEnvelope`` 的那段代码。

用法（在宿主自己的测试里）::

    from perceptkit.conformance import run_report_conformance

    def test_my_ios_adapter_is_conformant():
        problems = run_report_conformance(
            lambda payload: my_adapter.to_envelope(payload),
            samples=[
                (REAL_IOS_SNAPSHOT, "observed"),
                (EMPTY_SNAPSHOT, "no_data"),
                (PERMISSION_DENIED_SNAPSHOT, "unavailable"),
            ],
        )
        assert not problems, "\\n".join(problems)

---

## 为什么 report 也要单独一套

上报适配器是**唯一**能凭空造出事实的地方。它错了，后面每一层都在忠实地
处理一份假数据 —— 管线不会报错，规则会照常触发，agent 会照常开口。

三种最容易犯的错：

    把"没测到"变成 0      "今天 0 步"和"今天没戴表"对 agent 是两句完全不同的话
    把"没权限"变成"没数据" 前者要引导用户去开权限,后者只该闭嘴
    补一个 occurred_at    源头没给时间就不该编一个"现在" —— 编出来的时间
                          会让这条数据被归到错误的一天,而且永远查不出来
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from ..contracts.availability import AVAILABILITY_STATES
from ..contracts.errors import ContractError
from ..contracts.report import ReportEnvelope

AdapterFn = Callable[[Any], Any]

REPORT_GUARANTEES: tuple[str, ...] = (
    "R1 产出的东西必须能被 ReportEnvelope 解析",
    "R2 每条观测的 availability 必须是三态之一",
    "R3 `no_data` / `unavailable` 不许带值 —— 带了就是在编事实",
    "R4 `observed` 必须带值，哪怕值是 0",
    "R5 同一份载荷转两次要得到同样的信封（不许掺进当前时间、随机数）",
    "R6 report_id 在一份载荷内稳定 —— 它是幂等的钥匙",
    "R7 样本标了预期状态的话，产出的状态必须对得上",
)

REPORT_NOT_PROVABLE: tuple[str, ...] = (
    "字段语义对不对 —— 我们能查出「这里是个数字」，查不出「这个数字是步数不是心率」",
    "真机采集的完整性 —— 要拿真实设备抓下来的载荷跑，手写的样本证明不了覆盖面",
    "🔴 不标预期状态的话，**查不出零填充** —— 光看信封，「真的走了 0 步」和"
    "「没戴表被写成 0 步」长得一模一样。这正是 R7 存在的理由：只有你知道"
    "这份载荷应该产出什么",
)


def _split(sample: Any) -> tuple[Any, str | None]:
    """样本可以是裸载荷，也可以是 ``(载荷, 预期状态)``。"""
    if (isinstance(sample, tuple) and len(sample) == 2
            and sample[1] in AVAILABILITY_STATES):
        return sample[0], sample[1]
    return sample, None


def _check_one(envelope: ReportEnvelope, label: str, problems: list[str]) -> None:
    if not envelope.report_id:
        problems.append(f"R6 {label}：report_id 是空的。它是幂等的钥匙，"
                        "空的话同一份上报重传会被当成两份")
    for i, obs in enumerate(envelope.observations):
        where = f"{label} 第 {i + 1} 条观测（{obs.signal}）"
        if obs.availability not in AVAILABILITY_STATES:
            problems.append(
                f"R2 {where}：availability={obs.availability!r} 不是三态之一"
                f"（{sorted(AVAILABILITY_STATES)}）"
            )
            continue
        has_value = obs.value is not None
        if obs.availability != "observed" and has_value:
            problems.append(
                f"R3 {where}：availability={obs.availability!r} 却带了值 {obs.value!r}。"
                "没测到就是没测到 —— 带上一个值等于在编事实"
            )
        if obs.availability == "observed" and not has_value:
            # 走 parse 的路径上，信封契约自己就会拦掉这种（报成 R1）。
            # 这里兜的是适配器直接返回 ReportEnvelope 对象的情况。
            problems.append(
                f"R4 {where}：说是 observed 却没有值。"
                "如果真的测到 0，就把 0 写进来 —— 零是一个有效的观测，不是缺失"
            )
        if obs.occurred_at is None:
            problems.append(
                f"R1 {where}：没有 occurred_at。源头给不了时间的话，"
                "这条观测就不该被造出来 —— 补一个「现在」会让它被归到错误的一天"
            )


def run_report_conformance(adapter: AdapterFn,
                           samples: Sequence[Any] | Iterable[Any]) -> list[str]:
    """拿几份真实载荷跑一遍适配器，返回问题清单（空 = 通过）。

    ``samples`` 至少应该包含：一份正常的、一份**什么都没测到**的、
    一份**权限被拒**的。后两种正是最容易被写成「返回 0」的。

    每份样本可以是裸载荷，也可以是 ``(载荷, 预期状态)``。**强烈建议带上预期**：
    不带的话这套检查查不出零填充 —— 光看信封，「真的走了 0 步」和「没戴表被
    写成 0 步」长得一模一样，只有你知道这份载荷应该产出什么。
    """
    problems: list[str] = []
    samples = list(samples)
    if not samples:
        problems.append(
            "一份样本都没给。至少要三份：正常 / 什么都没测到 / 权限被拒 —— "
            "后两种正是最容易被写成「返回 0」的"
        )
        return problems

    for n, sample in enumerate(samples, 1):
        payload, expected = _split(sample)
        label = f"样本 {n}" + (f"（预期 {expected}）" if expected else "")
        try:
            raw = adapter(payload)
        except Exception as exc:                   # noqa: BLE001
            problems.append(f"R1 {label}：适配器抛了 {type(exc).__name__}({exc})")
            continue
        try:
            envelope = (raw if isinstance(raw, ReportEnvelope)
                        else ReportEnvelope.parse(raw))
        except (ContractError, Exception) as exc:  # noqa: BLE001
            problems.append(f"R1 {label}：产出的东西解析不了 —— {exc}")
            continue

        _check_one(envelope, label, problems)

        if expected is not None:
            got = {o.availability for o in envelope.observations}
            if got != {expected}:
                problems.append(
                    f"R7 {label}：产出的状态是 {sorted(got)}，预期 {expected!r}。"
                    + ("把「没测到」写成 observed 就是零填充 —— "
                       "「今天 0 步」和「今天没戴表」对 agent 是两句完全不同的话"
                       if expected != "observed" and "observed" in got else "")
                )

        # R5：同一份载荷转两次要一样。掺进 now() 或随机 id 的适配器会在这里露馅，
        # 而那种适配器会让每次重传都变成一份"新"上报，幂等彻底失效。
        try:
            again = adapter(payload)
            again = (again if isinstance(again, ReportEnvelope)
                     else ReportEnvelope.parse(again))
        except Exception:                          # noqa: BLE001
            continue
        if again.report_id != envelope.report_id:
            problems.append(
                f"R5 {label}：同一份载荷转两次得到了两个 report_id"
                f"（{envelope.report_id!r} / {again.report_id!r}）。"
                "适配器里掺了当前时间或随机数 —— 这会让每次重传都变成一份新上报，"
                "幂等彻底失效"
            )

    return problems


__all__ = ["REPORT_GUARANTEES", "REPORT_NOT_PROVABLE", "run_report_conformance"]
