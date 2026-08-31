"""wake / report 两套 adapter conformance 自己的测试。

一套「检查别人有没有做对」的工具，最危险的失败模式是**它自己永远说 OK**。
所以这里的每一条都是先写一个故意做错的 adapter，再断言检查确实抓到了它。

产品规范 §20 把 storage / wake / report 三种 adapter conformance 并列为
最低交付物。storage 那套早就有，这两套是后补的。
"""
from __future__ import annotations

from datetime import datetime, timezone

from perceptkit.conformance import run_report_conformance, run_wake_conformance
from perceptkit.contracts.receipt import WakeReceipt

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# wake
# ---------------------------------------------------------------------------

class GoodWake:
    """一个正确的 wake 适配器 —— 宿主要写的就是这么多。"""

    def __init__(self):
        self.seen: set[str] = set()

    def wake(self, event, attempt):
        status = "duplicate" if event.event_id in self.seen else "accepted"
        self.seen.add(event.event_id)
        return WakeReceipt(event_id=event.event_id, attempt_id=attempt.attempt_id,
                           status=status, received_at=T0)


def test_a_correct_wake_adapter_passes():
    assert run_wake_conformance(GoodWake) == []


def test_it_catches_an_adapter_that_is_not_idempotent():
    """崩溃重投是常态。不幂等的话，用户会被同一件事提醒两次。"""

    class Forgetful(GoodWake):
        def wake(self, event, attempt):
            return WakeReceipt(event_id=event.event_id, attempt_id=attempt.attempt_id,
                               status="accepted", received_at=T0)

    problems = run_wake_conformance(Forgetful)
    assert any("W3" in p for p in problems)


def test_it_catches_an_adapter_that_raises_instead_of_refusing():
    """"runtime 拒绝"是正常应答。抛异常会被当成投递失败，于是无限重试。"""

    class Angry:
        def wake(self, event, attempt):
            raise RuntimeError("我不想处理这个事件")

    problems = run_wake_conformance(Angry)
    assert any("W4" in p for p in problems)


def test_it_catches_a_receipt_that_does_not_match_what_was_delivered():
    class Mismatched(GoodWake):
        def wake(self, event, attempt):
            return WakeReceipt(event_id="别的事件", attempt_id="别的尝试",
                               status="accepted", received_at=T0)

    problems = run_wake_conformance(Mismatched)
    assert any("W2" in p for p in problems)


def test_it_catches_a_naive_timestamp_on_the_receipt():
    """裸时间在跨时区宿主上会被解释错，而且错得不会报错。"""

    class Naive(GoodWake):
        def wake(self, event, attempt):
            r = super().wake(event, attempt)
            object.__setattr__(r, "received_at", datetime(2026, 8, 27, 10, 0))
            return r

    problems = run_wake_conformance(Naive)
    assert any("W6" in p for p in problems)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

NORMAL = {"steps": 8000, "measured": True, "authorized": True}
NOTHING = {"steps": None, "measured": False, "authorized": True}
DENIED = {"steps": None, "measured": False, "authorized": False}

#: 每份样本都标上"这份载荷应该产出什么"。不标的话查不出零填充 ——
#: 光看信封，「真的走了 0 步」和「没戴表被写成 0 步」长得一模一样。
SAMPLES = [(NORMAL, "observed"), (NOTHING, "no_data"), (DENIED, "unavailable")]


def good_adapter(payload) -> dict:
    """一个正确的上报适配器：三态分得清，不编时间，不编 id。"""
    if not payload["authorized"]:
        availability, value = "unavailable", None
    elif not payload["measured"]:
        availability, value = "no_data", None
    else:
        availability, value = "observed", {"step_count": payload["steps"]}
    obs = {"signal": "steps", "signal_schema_version": 1,
           "occurred_at": "2026-08-27T10:00:00+00:00",
           "availability": availability}
    if value is not None:
        obs["value"] = value
    return {"schema_version": 1, "report_id": f"r-{availability}",
            "producer": "ios", "observations": [obs]}


def test_a_correct_report_adapter_passes():
    assert run_report_conformance(good_adapter, SAMPLES) == []


def test_it_catches_an_adapter_that_turns_no_data_into_zero():
    """「今天 0 步」和「今天没戴表」对 agent 是两句完全不同的话。"""

    def zero_filling(payload):
        out = good_adapter(payload)
        obs = out["observations"][0]
        obs["availability"] = "observed"
        obs["value"] = {"step_count": payload["steps"] or 0}
        return out

    problems = run_report_conformance(zero_filling, SAMPLES)
    assert any("R7" in p for p in problems)
    assert any("零填充" in p for p in problems)


def test_zero_filling_slips_through_when_the_samples_carry_no_expectation():
    """这条测试是在钉住这套检查【做不到什么】。

    不标预期的话，零填充是查不出来的 —— 一份全是 `observed 0` 的信封，
    形状上完全合法。写下来，免得以后有人以为"跑过 conformance 了"就等于验过。
    """

    def zero_filling(payload):
        out = good_adapter(payload)
        obs = out["observations"][0]
        obs["availability"] = "observed"
        obs["value"] = {"step_count": payload["steps"] or 0}
        return out

    bare = [NORMAL, NOTHING, DENIED]        # 故意不带预期
    assert run_report_conformance(zero_filling, bare) == []


def test_it_catches_an_adapter_that_attaches_a_value_to_a_missing_reading():
    def contradictory(payload):
        out = good_adapter(payload)
        out["observations"][0]["value"] = {"step_count": 0}
        return out

    problems = run_report_conformance(contradictory, SAMPLES)
    assert any("R3" in p for p in problems)


def test_it_catches_an_adapter_that_says_observed_with_nothing_in_hand():
    def empty_claim(payload):
        out = good_adapter(payload)
        out["observations"][0]["availability"] = "observed"
        out["observations"][0].pop("value", None)
        return out

    problems = run_report_conformance(empty_claim, SAMPLES)
    # 信封契约自己就拦住了（报成 R1）—— R4 是给"直接返回 ReportEnvelope
    # 对象、绕过 parse"的适配器兜底的。这里只要确认它没被放过去。
    assert problems and all("required when availability" in p for p in problems)


def test_it_catches_an_adapter_that_invents_a_fresh_id_every_time():
    """掺了随机数或当前时间的适配器，会让每次重传都变成一份"新"上报。"""
    counter = {"n": 0}

    def unstable(payload):
        counter["n"] += 1
        out = good_adapter(payload)
        out["report_id"] = f"r-{counter['n']}"
        return out

    problems = run_report_conformance(unstable, SAMPLES)
    assert any("R5" in p for p in problems)


def test_it_catches_an_adapter_that_blows_up():
    def broken(payload):
        raise ValueError("字段名拼错了")

    problems = run_report_conformance(broken, SAMPLES)
    assert any("R1" in p for p in problems)


def test_running_with_no_samples_is_itself_reported():
    """没有样本就跑一遍然后说"通过"，是这类工具最坏的失败方式。"""
    assert run_report_conformance(good_adapter, []) != []
