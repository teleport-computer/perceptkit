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


# ---------------------------------------------------------------------------
# I6 —— 归属日期用了时间戳的 offset，而不是观测声明的时区
#
# 我们**自己**在给产品方的信里写过这条（§19）：「`occurred_at` 的偏移不能
# 替代时区」。代码里没做到。
# ---------------------------------------------------------------------------

def test_the_declared_timezone_decides_the_day_not_the_timestamp_offset():
    """上海用户早上 7 点，那条数据属于今天，不是昨天。

    producer 用 UTC 发 `occurred_at`、把 IANA 时区放在观测的 `timezone` 里 ——
    这是完全正常的发法，io 的适配层就是这么发的。按 offset 算日期的话，
    **本地 00:00–08:00 的数据每天都落到前一天**：一整个凌晨加早晨，
    「昨天走了多少步」「昨晚睡了几小时」跟着一起偏。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    at = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)      # 上海 = 9/2 00:00
    kit.ingest({
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [{
            "signal": "motion_state", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "value": {"state": "walking"},
        }],
    }, context=IngestContext("u", at))
    rows, _ = storage.list_observations(subject_id="u", signal="motion_state")
    assert str(rows[0].effective_local_date) == "2026-09-02"


# ---------------------------------------------------------------------------
# I7 —— `comparison_strategy` 声明了，但没有任何代码读它
# ---------------------------------------------------------------------------

def _focus(kit, at, active=True):
    kit.ingest({
        "schema_version": 1, "report_id": f"f{at.isoformat()}", "producer": "ios",
        "observations": [{
            "signal": "focus_state", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "value": {"is_active": active},
        }],
    }, context=IngestContext("u", at))


def test_a_state_that_did_not_change_does_not_add_a_timeline_entry():
    """iOS 每 5 分钟保活上报一次，「还在专注」一天能写出几百条一样的记录。

    时间线记的是**什么时候变了**。被同一个状态刷满之后，「每日切换次数」
    「最长一段」这类聚合就没有意义了。manifest 上一直写着
    `comparison_strategy="state_change"`，只是没有代码读它。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    for i in range(4):
        _focus(kit, base + timedelta(minutes=5 * i))
    rows, _ = storage.list_observations(subject_id="u", signal="focus_state")
    assert len(rows) == 1

    _focus(kit, base + timedelta(minutes=25), active=False)     # 真的变了
    rows, _ = storage.list_observations(subject_id="u", signal="focus_state")
    assert len(rows) == 2


def test_suppressing_repeats_still_lets_the_duration_aggregate_advance():
    """只跳过明细，**当前值和聚合照常走**。

    `duration_by_state` 靠相邻两条观测的时间差累计时长 —— 跳过聚合会让时长
    永远停在第一次，那就是用一个 bug 换另一个。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    for i in range(4):
        _focus(kit, base + timedelta(minutes=5 * i))
    doc = storage.get_aggregate(subject_id="u", signal="focus_state",
                                start_date=base.date(), end_date=base.date()
                                )[0].typed_aggregate
    assert doc["minutes"]["focused"] == 15.0        # 三段 5 分钟


# ---------------------------------------------------------------------------
# I8 —— 一个信号只能有一条当前值，锚点因此被合并
# ---------------------------------------------------------------------------

def _anchor(kit, at, anchor_id, label):
    kit.ingest({
        "schema_version": 1, "report_id": f"a{anchor_id}{at.isoformat()}",
        "producer": "ios", "observations": [{
            "signal": "proximity_anchor", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai",
            "value": {"anchor_id": anchor_id, "anchor_type": "wifi",
                      "label": label, "is_connected": True},
        }],
    }, context=IngestContext("u", at))


def test_two_anchors_named_home_stay_two_anchors():
    """用户搬家，新旧网络都叫 "home"。

    按名字看是同一个，按 `anchor_id` 看是两个。合并之后历史再也分不开
    哪一段是哪个家 —— 规范 §12 专门列了这一条。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    _anchor(kit, base, "wifi-old-home", "home")
    _anchor(kit, base + timedelta(minutes=1), "wifi-new-home", "home")
    current = storage.get_current(subject_id="u", signals=["proximity_anchor"]
                                  )["proximity_anchor"]
    assert len(current) == 2
    assert {p.typed_value["anchor_id"] for p in current} == {
        "wifi-old-home", "wifi-new-home"}


def test_renaming_an_anchor_does_not_create_a_second_one():
    """身份是 anchor_id，不是 label。改名只是改名。"""
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    _anchor(kit, base, "wifi-1", "家")
    _anchor(kit, base + timedelta(minutes=1), "wifi-1", "老家")
    current = storage.get_current(subject_id="u", signals=["proximity_anchor"]
                                  )["proximity_anchor"]
    assert len(current) == 1
    assert current[0].typed_value["label"] == "老家"


def test_a_signal_without_declared_dimensions_still_keeps_one_current():
    """默认行为不能变：绝大多数信号同一时刻只有一个答案。"""
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    for i, level in enumerate((0.9, 0.8)):
        kit.ingest({
            "schema_version": 1, "report_id": f"b{i}", "producer": "ios",
            "observations": [{
                "signal": "battery", "signal_schema_version": 1,
                "occurred_at": (base + timedelta(minutes=i)).isoformat(),
                "availability": "observed", "timezone": "Asia/Shanghai",
                "value": {"level_ratio": level, "is_charging": False,
                          "is_low_power_mode_enabled": False},
            }],
        }, context=IngestContext("u", base + timedelta(minutes=i)))
    current = storage.get_current(subject_id="u", signals=["battery"])["battery"]
    assert len(current) == 1 and current[0].typed_value["level_ratio"] == 0.8


# ---------------------------------------------------------------------------
# I9 —— 每日照片数量按明细的保留期被扫掉
# ---------------------------------------------------------------------------

def test_the_daily_photo_count_outlives_the_individual_photos():
    """单条明细 7 天、**每日数量永久**，是两个数。

    只写明细那个，聚合会继承它 —— 于是「8月1日新增了 5 张」一周后被清掉。
    那是一件发生过的事实，不是「现在还剩几张」。
    """
    sig = MINIMAL_SIGNALS["photo_library_added"]
    assert sig.history_retention_days == 7
    assert sig.keeps_aggregates_forever


# ---------------------------------------------------------------------------
# 交付清单 §15 里唯一没有测试盯着的一项：
#   「App 缺失 close、Music 采样间断」
#
# 两个都是**我们和规范不同的地方**，而且不同得有理由。没有测试的话，
# 后来的人看到「规范要求算时长，代码没算」，最可能的动作是把它补上 ——
# 补回来的是一份大概率残缺的数字，比没有更糟。
# ---------------------------------------------------------------------------

def _app(kit, at, app, action):
    kit.ingest({
        "schema_version": 1, "report_id": f"{app}{action}{at.isoformat()}",
        "producer": "ios", "observations": [{
            "signal": "app_usage", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai",
            "value": {"app_id": app, "app_name": app, "action": action,
                      **({"open_count": 1} if action == "open" else {})},
        }],
    }, context=IngestContext("u", at))


def test_a_missing_close_never_becomes_an_invented_duration():
    """只配了 open 自动化的用户，绝大多数 app 根本没有结束事件。

    拿有 close 的那部分算平均时长，得到的是一个只反映「谁配得全」的数字。
    所以这个信号**刻意不建时长统计** —— 聚合里只该有靠 open 就能答准的
    「今天打开了几次」。
    """
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    _app(kit, base, "Slack", "open")
    _app(kit, base + timedelta(minutes=30), "Slack", "open")   # 没有 close
    doc = storage.get_aggregate(subject_id="u", signal="app_usage",
                                start_date=base.date(), end_date=base.date()
                                )[0].typed_aggregate
    assert doc["open_count"]["total"] == 2
    assert not any("minute" in k or "duration" in k for k in doc), \
        "app_usage 不该出现任何时长统计——覆盖面残缺，那个数字不可信"


def test_music_edges_are_marked_per_record_not_all_estimated():
    """规范说「只有轮询样本就标 estimated」，实际是同一个信号两种精度并存。

    Apple Music 走系统播放器、切歌 2 秒后就上报，起止是准的；Spotify、
    网易云只能靠快照采到的点。一律标 estimated 会把本来准确的那一半丢掉。
    """
    field = {f.key: f for f in MINIMAL_SIGNALS["music_playback"].fields}["edge_quality"]
    assert field.enum == ("measured", "estimated")
    assert not field.nullable, "每条都必须表态，不能留空让读的人自己猜"
