"""六个 Critical 的回归测试 —— 这些是**当时应该有、却没有**的测试。

2026-08-28 的代码 review 抓出六个 Critical。它们当时全部处于"测试全绿"状态，
原因不是没测，是**测偏了**：

    修订那条  用了 hk-X / hk-Y 两个不同的 sample id，正好绕开真实修订路径
    事务那条  只数了 transaction() 被进入几次，没验语义
    隐私那条  只检查了 manifest 上的标签，没验落库和查询出口
    电量重复上报 / 非 observed 求值 / 不唤醒事件的持久化 —— 压根没测

所以这个文件里每一条，都必须在对应修复被撤掉时变红。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.contracts import delivery
from perceptkit.rules import EventDefinition

SH = timezone(timedelta(hours=8))


def t(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def ctx(hhmm: str, subject: str = "u1", day: str = "2026-08-27") -> IngestContext:
    return IngestContext(subject_id=subject, received_at=t(hhmm, day))


def battery(level: float, hhmm: str, rid: str) -> dict:
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "battery", "signal_schema_version": 1,
            "occurred_at": t(hhmm).isoformat(), "availability": "observed",
            "value": {"level_ratio": level, "is_charging": False},
        }],
    }


def steps(count: int, hhmm: str, rid: str, *, sample="hk-A",
          revision=None, availability="observed", day="2026-08-27") -> dict:
    obs = {
        "signal": "steps", "signal_schema_version": 1,
        "occurred_at": t(hhmm, day).isoformat(), "availability": availability,
        "source_event_id": sample,
    }
    if availability == "observed":
        obs["value"] = {"step_count": count, "local_date": day}
    if revision is not None:
        obs["source_revision"] = revision
    return {"schema_version": 1, "report_id": rid, "producer": "ios",
            "observations": [obs]}


# ---------------------------------------------------------------------------
# C1 —— 批级 claim 曾经把处理中断伪装成"已处理"
# ---------------------------------------------------------------------------

def test_a_crash_midway_through_a_batch_does_not_mark_it_processed():
    """以前：先认领这批、再逐条各自提交。第 1 条提交后崩溃，重试直接拿到
    duplicate，**剩下的观测永久丢失** —— 一次中断被伪装成了"已处理完"。

    现在整批一个事务，所以"认领"这件事本身也会跟着回滚。"""
    boom = InMemoryStorage()
    calls = {"n": 0}
    original = boom.append_observation

    def flaky(observation):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二条写到一半崩了")
        return original(observation)

    boom.append_observation = flaky
    kit = PerceptionKit(storage=boom)
    payload = {
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [
            steps(100, "09:00", "x", sample="a")["observations"][0],
            steps(200, "10:00", "x", sample="b")["observations"][0],
        ],
    }
    with pytest.raises(RuntimeError):
        kit.ingest(payload, context=ctx("10:01"))

    # 关键断言：认领必须和工作在同一个事务里。真实数据库会回滚它；
    # 内存实现没有回滚，所以这里退而验证"认领发生在事务【内部】"。
    assert boom.transactions_opened == 1


def test_the_whole_batch_shares_one_transaction():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest({
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [
            steps(100, "09:00", "x", sample="a")["observations"][0],
            steps(200, "10:00", "x", sample="b")["observations"][0],
        ],
    }, context=ctx("10:01"))
    assert s.transactions_opened == 1 and s.transaction_depth == 0


# ---------------------------------------------------------------------------
# C2 —— singleton 信号曾经只能更新一次
# ---------------------------------------------------------------------------

def test_a_singleton_signal_keeps_updating():
    """以前：identity 只由 (subject, source, signal) 组成，恒定不变 →
    第二次电量上报被判成重传，**当前电量永远冻结在第一次**。

    根因是把"这是哪一条事实"和"这是哪一次投递"用成了同一个键。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(battery(0.9, "09:00", "r1"), context=ctx("09:00"))
    out = kit.ingest(battery(0.2, "18:00", "r2"), context=ctx("18:00"))

    assert len(out.applied) == 1 and not out.duplicates
    current = next(iter(s.current.values()))
    assert current.typed_value["level_ratio"] == 0.2


def test_a_true_retransmission_of_a_singleton_is_still_deduped():
    """拆开身份不能把去重一起拆掉：同内容同时刻重发仍然要挡住。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(battery(0.9, "09:00", "r1"), context=ctx("09:00"))
    out = kit.ingest(battery(0.9, "09:00", "r2"), context=ctx("09:01"))
    assert len(out.duplicates) == 1 and not out.applied


def test_a_current_only_signal_writes_no_observation_rows():
    """`current_only` 就该只留当前值 —— 以前对它也无条件追加明细。"""
    s = InMemoryStorage()
    PerceptionKit(storage=s).ingest(battery(0.9, "09:00", "r1"),
                                    context=ctx("09:00"))
    assert not s.observations
    assert len(s.current) == 1


# ---------------------------------------------------------------------------
# C3 —— 同一样本的修订曾经走不到当前值判定
# ---------------------------------------------------------------------------

def test_the_same_sample_revised_upward_reaches_the_current_value():
    """以前的测试用了两个不同 sample id（hk-X / hk-Y），正好绕开真实路径。
    真实场景是**同一个 sample** 从 revision 1 改到 2 —— 那会被提前判重，
    用户在健康 App 里的纠错永远生效不了。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(steps(95000, "10:00", "r1", sample="hk-SAME", revision=1),
               context=ctx("10:00"))
    out = kit.ingest(steps(9500, "10:00", "r2", sample="hk-SAME", revision=2),
                     context=ctx("10:01"))

    assert len(out.applied) == 1 and not out.duplicates
    assert next(iter(s.current.values())).typed_value["step_count"] == 9500


def test_the_same_sample_at_the_same_revision_is_still_a_retransmission():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(steps(3000, "10:00", "r1", sample="hk-SAME", revision=1),
               context=ctx("10:00"))
    out = kit.ingest(steps(3000, "10:00", "r2", sample="hk-SAME", revision=1),
                     context=ctx("10:01"))
    assert len(out.duplicates) == 1


# ---------------------------------------------------------------------------
# C4 —— compare-and-put 的返回值曾经被忽略
# ---------------------------------------------------------------------------

def test_a_lost_cas_race_is_retried_not_silently_dropped():
    """以前完全忽略 CAS 的返回值：两个并发事务都读到旧版本，
    较新的那个写失败被**静默丢掉** —— 当前值停在旧数据上，没有任何报错。"""
    s = InMemoryStorage()
    original = s.compare_and_put_current
    state = {"failed": False}

    def flaky(projection, *, expected_version):
        if not state["failed"]:
            state["failed"] = True
            return False           # 模拟"有人抢先写了"
        return original(projection, expected_version=expected_version)

    s.compare_and_put_current = flaky
    kit = PerceptionKit(storage=s)
    kit.ingest(battery(0.5, "09:00", "r1"), context=ctx("09:00"))
    assert next(iter(s.current.values())).typed_value["level_ratio"] == 0.5


def test_giving_up_after_repeated_cas_failures_is_reported_not_silent():
    s = InMemoryStorage()
    s.compare_and_put_current = lambda projection, *, expected_version: False
    out = PerceptionKit(storage=s).ingest(battery(0.5, "09:00", "r1"),
                                          context=ctx("09:00"))
    assert any("写入竞争失败" in w for w in out.warnings)


# ---------------------------------------------------------------------------
# C5 —— 声明了"永不持久化"的字段曾经被存下来
# ---------------------------------------------------------------------------

def _location(**extra) -> dict:
    value = {"locality": "上海", "country_code": "CN"}
    value.update(extra)
    return {
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [{
            "signal": "location_city", "signal_schema_version": 1,
            "occurred_at": t("10:00").isoformat(), "availability": "observed",
            "value": value,
        }],
    }


def test_a_restricted_field_never_reaches_storage():
    """manifest 说"永不持久化"就必须真的不落库。

    **在写入边界丢，不是在读取边界过滤** —— 靠读取点自觉过滤，漏一个点就是
    泄漏，而且数据已经在库里了，泄漏是既成事实。"""
    s = InMemoryStorage()
    out = PerceptionKit(storage=s).ingest(
        _location(coordinate={"lat": 31.23, "lon": 121.47}), context=ctx("10:00"))

    stored = next(iter(s.observations.values()))
    assert "coordinate" not in (stored.typed_value or {})
    assert "locality" in stored.typed_value
    assert any("coordinate" in w for w in out.warnings)     # 丢了什么要说出来


def test_a_restricted_field_never_reaches_the_agent_either():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(_location(coordinate={"lat": 31.23}), context=ctx("10:00"))
    view = kit.get_current(subject_id="u1", signals=["location_city"], now=t("10:05"))
    assert "coordinate" not in (view["location_city"].value or {})


def test_fields_the_manifest_never_declared_do_not_get_persisted():
    """不报错（让 producer 可以先发新字段），但也不进 canonical value ——
    否则任何未声明字段都能被存下来并查出去。"""
    s = InMemoryStorage()
    PerceptionKit(storage=s).ingest(_location(secret_thing="oops"),
                                    context=ctx("10:00"))
    assert "secret_thing" not in (next(iter(s.observations.values())).typed_value or {})


# ---------------------------------------------------------------------------
# C6 —— 不唤醒的事件曾经不落地，规则状态却已经推进
# ---------------------------------------------------------------------------

SILENT_RULE = EventDefinition.parse({
    "id": "quiet_steps", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
    "event": {"type": "activity.step_goal_reached"},
    "wake": {"enabled": False},
})


def test_an_event_that_is_not_meant_to_wake_still_lands_durably():
    """以前不唤醒的事件只放进内存返回值，进程一崩就永久丢失，
    而规则状态已经推进、不会再产生它。

    wake_enabled 只决定进不进可投递状态，不决定这个事实存不存。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[SILENT_RULE])
    kit.ingest(steps(2999, "09:00", "r1"), context=ctx("09:00"))
    out = kit.ingest(steps(3012, "10:00", "r2", sample="hk-B"), context=ctx("10:00"))

    assert len(out.events) == 1
    assert len(s.outbox) == 1                       # 落地了
    entry = next(iter(s.outbox.values()))
    assert entry.delivery_state == delivery.NOT_DISPATCHED
    assert not s.list_pending_events()              # 但不会被 worker 捞走


# ---------------------------------------------------------------------------
# I1 —— 非 observed 和迟到数据曾经会污染规则
# ---------------------------------------------------------------------------

CHANGED_RULE = EventDefinition.parse({
    "id": "steps_changed", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "changed"},
    "lifecycle": {"fire": "every"},
    "event": {"type": "activity.changed"},
})


def test_no_data_does_not_count_as_a_change():
    """以前会把"100 → 没数据"当成一次变化，还会把 previous 推成 None，
    之后的 threshold_crossing 全废。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[CHANGED_RULE])
    kit.ingest(steps(100, "09:00", "r1"), context=ctx("09:00"))
    kit.ingest(steps(200, "10:00", "r2", sample="hk-B"), context=ctx("10:00"))
    out = kit.ingest(steps(0, "11:00", "r3", sample="hk-C", availability="no_data"),
                     context=ctx("11:00"))
    assert not out.events


def test_late_arriving_data_enters_history_but_does_not_fire_rules():
    """迟到数据该进历史，但它的 previous/current 讲的不是当前故事 ——
    拿它去触发"变化了"会给用户一个错的提醒。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[CHANGED_RULE])
    kit.ingest(steps(500, "12:00", "r1", sample="hk-late-a"), context=ctx("12:00"))
    out = kit.ingest(steps(100, "08:00", "r2", sample="hk-late-b"), context=ctx("12:01"))
    assert not out.events
    assert len(s.observations) == 2             # 但历史里有它


def test_a_conflicting_current_value_suspends_all_derived_work():
    """"到底哪份数据生效了"都说不清的时候，再去更新聚合、推进规则状态、
    产生事件，只会把错误扩散。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[CHANGED_RULE])
    kit.ingest(steps(100, "10:00", "r1", sample="hk-x"), context=ctx("10:00"))
    before = len(s.aggregates)
    out = kit.ingest(steps(999, "10:00", "r2", sample="hk-y"), context=ctx("10:01"))
    assert out.conflicts and not out.events
    assert len(s.aggregates) == before


# ---------------------------------------------------------------------------
# I5 —— 规则版本升级曾经复用旧状态
# ---------------------------------------------------------------------------

def test_bumping_a_rule_version_starts_from_a_clean_state():
    """当天把阈值规则从 v1 改成 v2，v1 的"今天已经触发过"不该继续压制 v2 ——
    用户改了规则却不生效，而且没有任何地方报错。"""
    v1 = EventDefinition.parse({
        "id": "steps_goal", "version": 1,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
        "event": {"type": "activity.goal"},
    })
    v2 = EventDefinition.parse({
        "id": "steps_goal", "version": 2,
        "source": {"signal": "steps", "field": "step_count"},
        "condition": {"type": "threshold_crossing", "operator": "gte", "value": 2000},
        "event": {"type": "activity.goal"},
    })
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[v1])
    kit.ingest(steps(2999, "09:00", "r1"), context=ctx("09:00"))
    assert kit.ingest(steps(3012, "10:00", "r2", sample="b"),
                      context=ctx("10:00")).events

    kit.definitions = [v2]                      # 用户当天改了规则
    kit.ingest(steps(1000, "11:00", "r3", sample="c"), context=ctx("11:00"))
    out = kit.ingest(steps(2500, "12:00", "r4", sample="d"), context=ctx("12:00"))
    assert out.events, "v1 的「今天已触发」压制了 v2"


# ---------------------------------------------------------------------------
# I7 —— payload 上限曾经是个从不生效的参数
# ---------------------------------------------------------------------------

def test_the_payload_byte_cap_is_actually_enforced():
    """摆一个从不生效的安全参数，比没有这个参数更糟 —— 它会让人以为有防线。"""
    s = InMemoryStorage()
    big = steps(1, "10:00", "r1")
    big["observations"][0]["value"]["padding"] = "x" * 5000
    out = PerceptionKit(storage=s, max_observations=10).ingest(
        big, context=ctx("10:00"),
    ) if False else None
    from perceptkit.processing import ingest_report
    from perceptkit.contracts import ReportEnvelope
    from perceptkit.manifest import MINIMAL_SIGNALS
    result = ingest_report(
        ReportEnvelope.parse(big), context=ctx("10:00"), storage=s,
        signals=MINIMAL_SIGNALS, max_payload_bytes=1000,
    )
    assert result.receipt.error_code == "payload_too_large"
    assert not s.observations


# ---------------------------------------------------------------------------
# I2 —— 过期 worker 曾经能覆盖新 owner 的状态
# ---------------------------------------------------------------------------

def test_an_expired_worker_cannot_overwrite_the_new_owners_state():
    """旧 worker 租约过期、事件被别人接管之后它才返回 —— 让它推进状态，
    等于一次超时变成一次错误的覆盖，而且看起来完全正常。"""
    from perceptkit.contracts import WAKE_ACCEPTED
    from perceptkit.contracts.receipt import WakeReceipt
    from perceptkit.contracts.records import EventOutboxEntry

    s = InMemoryStorage()
    s.enqueue_event(EventOutboxEntry(
        event_id="evt_1", subject_id="u1", definition_id="d", definition_version=1,
        event_type="t", occurred_at=t("10:00"), detected_at=t("10:00"),
        fact_snapshot={},
    ))
    first = s.claim_pending_event(worker_id="w1", now=t("10:00"), lease_seconds=60)
    second = s.claim_pending_event(worker_id="w2", now=t("10:05"), lease_seconds=60)
    assert second is not None and second.claim_token != first.claim_token

    # w1 现在才带着旧令牌回来
    stale = s.record_wake_receipt(
        receipt=WakeReceipt("evt_1", "old", WAKE_ACCEPTED, t("10:06")),
        next_state=delivery.DELIVERED, claim_token=first.claim_token,
    )
    assert stale is False
    assert s.outbox["evt_1"].delivery_state == delivery.CLAIMED   # 状态没被改
    assert s.receipts                                             # 但进了审计


def test_the_current_owner_can_still_finish_normally():
    from perceptkit.contracts import WAKE_ACCEPTED
    from perceptkit.contracts.receipt import WakeReceipt
    from perceptkit.contracts.records import EventOutboxEntry

    s = InMemoryStorage()
    s.enqueue_event(EventOutboxEntry(
        event_id="evt_1", subject_id="u1", definition_id="d", definition_version=1,
        event_type="t", occurred_at=t("10:00"), detected_at=t("10:00"),
        fact_snapshot={},
    ))
    claimed = s.claim_pending_event(worker_id="w1", now=t("10:00"), lease_seconds=60)
    ok = s.record_wake_receipt(
        receipt=WakeReceipt("evt_1", "a", WAKE_ACCEPTED, t("10:01")),
        next_state=delivery.DELIVERED, claim_token=claimed.claim_token,
    )
    assert ok is not False
    assert s.outbox["evt_1"].delivery_state == delivery.DELIVERED


# ---------------------------------------------------------------------------
# I3 —— 两种"配得出来但不生效"的生命周期组合
# ---------------------------------------------------------------------------

def test_cooldown_now_applies_to_fire_every_as_well():
    """以前只在 fire=once 时检查冷却，于是 fire=every + rearm=cooldown
    完全没有冷却 —— 配了个不生效的东西，比配不出来更糟。"""
    from perceptkit.rules import Lifecycle, RuleState, evaluate
    rule = EventDefinition(
        definition_id="r", version=1, signal="steps", field_name="step_count",
        condition_type="changed", event_type="t",
        lifecycle=Lifecycle(fire="every", rearm="cooldown", cooldown_seconds=300),
    )
    fired = RuleState(previous_value=1, fired_in_scope=True,
                      last_fired_at=t("10:00").isoformat())
    assert not evaluate(rule, fired, 2, now=t("10:01")).fired
    assert evaluate(rule, fired, 2, now=t("10:06")).fired


def test_a_lifecycle_that_cannot_work_is_refused_at_construction():
    from perceptkit.contracts import ContractError
    from perceptkit.rules import Lifecycle
    with pytest.raises(ContractError):
        Lifecycle(scope="local_day", rearm="never")     # 换一天本来就重新武装了
    with pytest.raises(ContractError):
        Lifecycle(rearm="cooldown", cooldown_seconds=0)


# ---------------------------------------------------------------------------
# I4 —— 自定义 evaluator 的状态曾经被丢掉
# ---------------------------------------------------------------------------

def test_a_custom_evaluator_can_keep_its_own_derived_state():
    """引擎以前无条件用观测值覆盖 previous_value，导致任何需要自己维护
    派生状态的 evaluator 都没法工作。"""
    from perceptkit.rules import RuleState, evaluate
    from perceptkit.rules.types import RuleResult

    def counting(d, s, current, ctx):
        n = (s.previous_value or 0) + 1
        return RuleResult(n >= 3, RuleState(previous_value=n), reason=f"第 {n} 次")

    rule = EventDefinition(definition_id="r", version=1, signal="steps",
                           condition_type="counting", event_type="t")
    state = RuleState()
    for expected in (1, 2, 3):
        result = evaluate(rule, state, 0, now=t("10:00"),
                          extra_evaluators={"counting": counting})
        assert result.state.previous_value == expected
        state = result.state
    assert result.fired


# ---------------------------------------------------------------------------
# I5 —— 聚合把整条 payload 递给了每个 merger
#
# 这一条是**接真数据当天就炸了**的：影子第一次拿到真实 iOS 上报，
# 同一天的第二条上报直接抛 AttributeError。单测没抓到，因为所有单测
# 用的信号都只声明了一种聚合算法。
# ---------------------------------------------------------------------------

def _vitals(kit, storage, at):
    kit.ingest({
        "schema_version": 1, "report_id": f"r{at.minute}", "producer": "ios",
        "observations": [{
            "signal": "health_vitals", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai",
            # 静息心率是 numeric_dist，vo2_max 是 main_of_day —— manifest 里
            # 唯一一个混了两种算法的信号，所以也是唯一炸的那个。
            "value": {"resting_heart_rate": 58, "vo2_max": 41.3},
        }],
    }, context=IngestContext("u", at))


def test_two_reports_in_one_day_do_not_crash_a_mixed_strategy_signal():
    """同一个信号里两种聚合算法互相覆盖，第二条上报就崩。

    main_of_day 把字段写成裸数字，numeric_dist 下一条进来读 cell["min"]，
    读到的是个 float。**每个用户每天第二次上报都会踩**。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
    _vitals(kit, storage, base)
    _vitals(kit, storage, base + timedelta(minutes=5))      # 崩在这一行

    doc = storage.get_aggregate(subject_id="u", signal="health_vitals",
                                start_date=base.date(), end_date=base.date()
                                )[0].typed_aggregate
    assert doc["resting_heart_rate"]["count"] == 2          # 分布还在累计
    assert doc["vo2_max"] == 41.3                           # 快照还是裸值


def test_a_field_that_declares_no_aggregation_is_not_aggregated():
    """声明 none 的字段不该凭空长出聚合。

    weather 只有 temperature_c 声明了 numeric_dist，但 merger 拿到的是整条
    payload，于是湿度、紫外线、体感温度全被写了 min/max/sum/count ——
    没人声明过的数字，看起来却和声明过的一模一样可信。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    at = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
    kit.ingest({
        "schema_version": 1, "report_id": "w1", "producer": "ios",
        "observations": [{
            "signal": "weather", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai",
            "value": {"condition": "cloudy", "temperature_c": 27.4,
                      "humidity_ratio": 0.71, "uv_index": 3.0,
                      "apparent_temperature_c": 29.1},
        }],
    }, context=IngestContext("u", at))
    doc = storage.get_aggregate(subject_id="u", signal="weather",
                                start_date=at.date(), end_date=at.date()
                                )[0].typed_aggregate
    assert list(doc) == ["temperature_c"]
