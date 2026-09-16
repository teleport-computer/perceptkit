"""同一个跳变【再发生一次】是一件新事；同一份上报【重放一次】不是。

0.6.0 之前，值变化型规则（changed / enters / leaves / threshold_crossing /
delta / equals）的事件 id 只由 ``"旧值->新值"`` 这段文字决定。于是：

    第一天 家 -> 公司   evt_X 进发件箱
    第二天 家 -> 公司   还是 evt_X —— 发件箱 ON CONFLICT DO NOTHING，静默丢掉

引擎每次都说 ``fired=True``，丢的只是身份。此前的测试只验过"同一组参数
算两遍是同一个 id"（重放那半），**从没让同一个跳变真的发生两次**，
所以两件事被同一个 id 混成了一件。

另一半是前置条件：一条规则只能看一个字段。盯 ``anchor_id`` 的规则没法同时
要求 ``is_connected=True``，一条迟到的"家里 Wi-Fi 断开"会被讲成"到家了"。
两个缺陷必须一起修 —— 只修身份的话，每一次迟到的断开都会真的叫醒用户
（现在它们有一部分被身份碰撞"意外地"吞掉了）。

这个文件里每一条，都必须在对应修复被撤掉时变红。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import ContractError
from perceptkit.rules import EventDefinition, Lifecycle

SH = timezone(timedelta(hours=8))

#: io 线上那几条唤醒规则的生命周期形状：永远一个范围、每次都算、60 秒冷却。
EVERY_TIME = Lifecycle(scope="forever", fire="every", rearm="cooldown",
                       cooldown_seconds=60.0)


def t(hhmm: str, day: str = "2026-09-01") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def ctx(hhmm: str, day: str = "2026-09-01") -> IngestContext:
    return IngestContext(subject_id="u1", received_at=t(hhmm, day))


def anchor(anchor_id: str, hhmm: str, *, day: str = "2026-09-01",
           connected: bool = True, rid: str | None = None) -> dict:
    return {
        "schema_version": 1, "report_id": rid or f"a-{day}-{hhmm}-{anchor_id}",
        "producer": "ios",
        "observations": [{
            "signal": "proximity_anchor", "signal_schema_version": 1,
            "occurred_at": t(hhmm, day).isoformat(), "availability": "observed",
            "value": {"anchor_id": anchor_id, "anchor_type": "wifi",
                      "is_connected": connected},
        }],
    }


def broadcast(active: bool, hhmm: str, day: str = "2026-09-01") -> dict:
    return {
        "schema_version": 1, "report_id": f"b-{day}-{hhmm}", "producer": "ios",
        "observations": [{
            "signal": "broadcast", "signal_schema_version": 1,
            "occurred_at": t(hhmm, day).isoformat(), "availability": "observed",
            "value": {"is_active": active},
        }],
    }


ANCHOR_CHANGED = EventDefinition(
    definition_id="anchor_changed", version=1, signal="proximity_anchor",
    condition_type="changed", field_name="anchor_id",
    event_type="arrived_at_anchor", lifecycle=EVERY_TIME,
)

BROADCAST_OPENED = EventDefinition(
    definition_id="broadcast_opened", version=1, signal="broadcast",
    condition_type="enters", field_name="is_active", value=True,
    event_type="broadcast_opened", lifecycle=EVERY_TIME,
)
BROADCAST_CLOSED = EventDefinition(
    definition_id="broadcast_closed", version=1, signal="broadcast",
    condition_type="leaves", field_name="is_active", value=True,
    event_type="broadcast_closed", lifecycle=EVERY_TIME,
)


def by_type(storage: InMemoryStorage) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in storage.outbox.values():
        counts[entry.event_type] = counts.get(entry.event_type, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 缺陷一：同一个跳变第二次发生，被当成重放丢掉
# ---------------------------------------------------------------------------

def test_the_same_commute_on_two_days_is_two_arrivals():
    """家 -> 公司 -> 家 -> 公司。第二次"家 -> 公司"是新的一天、新的一次到达。

    只走 家 -> 公司 -> 家 的测试是抓不到这个的：两跳的文字不一样。
    """
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[ANCHOR_CHANGED])
    kit.ingest(anchor("home", "08:00"), context=ctx("08:00"))
    kit.ingest(anchor("office", "09:00"), context=ctx("09:00"))
    kit.ingest(anchor("home", "19:00"), context=ctx("19:00"))
    out = kit.ingest(anchor("office", "09:00", day="2026-09-02"),
                     context=ctx("09:00", day="2026-09-02"))

    assert [e.type for e in out.events] == ["arrived_at_anchor"]
    assert ("anchor_changed", "事件已在发件箱中") not in out.rule_misses
    assert len(s.outbox) == 3


def test_a_broadcast_toggled_three_times_is_five_events_not_two():
    """off -> on -> off -> on -> off -> on：开了三次、关了两次。

    修之前是 1 + 1：每种跳变文字只有一种（False->True / True->False）。
    """
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[BROADCAST_OPENED, BROADCAST_CLOSED])
    for i, active in enumerate([False, True, False, True, False, True]):
        hhmm = f"{10 + i:02d}:00"
        kit.ingest(broadcast(active, hhmm), context=ctx(hhmm))

    assert by_type(s) == {"broadcast_opened": 3, "broadcast_closed": 2}


def test_a_day_scoped_rule_repeating_within_the_day_is_not_collapsed():
    """把 scope 换成 local_day 救不了：同一天内的第二次同样跳变照样撞 id。"""
    rule = EventDefinition(
        definition_id="anchor_changed_daily", version=1, signal="proximity_anchor",
        condition_type="changed", field_name="anchor_id",
        event_type="arrived_at_anchor",
        lifecycle=Lifecycle(scope="local_day", fire="every", rearm="next_scope"),
    )
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[rule])
    for hhmm, where in [("08:00", "home"), ("09:00", "office"),
                        ("12:00", "home"), ("13:00", "office")]:
        kit.ingest(anchor(where, hhmm), context=ctx(hhmm))

    assert len(s.outbox) == 3


def test_a_threshold_crossed_again_after_dropping_back_is_a_new_event():
    """threshold_crossing 用的是同一条身份路径：跌回去再跨过来，是第二次。"""
    rule = EventDefinition(
        definition_id="battery_low", version=1, signal="battery",
        condition_type="threshold_crossing", field_name="level_ratio",
        operator="lte", value=0.2, event_type="device.battery_low",
        lifecycle=Lifecycle(scope="forever", fire="every", rearm="never"),
    )

    def level(v: float, hhmm: str) -> dict:
        return {"schema_version": 1, "report_id": f"bat-{hhmm}", "producer": "ios",
                "observations": [{
                    "signal": "battery", "signal_schema_version": 1,
                    "occurred_at": t(hhmm).isoformat(), "availability": "observed",
                    "value": {"level_ratio": v, "is_charging": False}}]}

    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[rule])
    for v, hhmm in [(0.5, "08:00"), (0.1, "09:00"), (0.5, "10:00"), (0.1, "11:00")]:
        kit.ingest(level(v, hhmm), context=ctx(hhmm))

    assert len(s.outbox) == 2


# ---------------------------------------------------------------------------
# 重放那半必须还在：同一份上报再来一次，不是新事件
# ---------------------------------------------------------------------------

def test_a_client_retrying_the_same_report_does_not_make_a_second_event():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[ANCHOR_CHANGED])
    kit.ingest(anchor("home", "08:00"), context=ctx("08:00"))
    kit.ingest(anchor("office", "09:00"), context=ctx("09:00"))
    # 同一条观测，客户端换了个 report_id 重传（没收到上一次的应答）
    again = kit.ingest(anchor("office", "09:00", rid="retry-1"), context=ctx("09:01"))

    assert not again.events
    assert again.duplicates
    assert len(s.outbox) == 1


def test_re_evaluating_the_same_observation_from_the_same_state_gives_the_same_id():
    """发件箱写进去了、其余没提交（非原子的 adapter、崩溃后重放）——
    同一条观测从同一个规则状态再求值一遍，必须还是同一个 id，被发件箱挡下。"""
    from perceptkit.processing.dispatch import evaluate_and_enqueue
    from perceptkit.processing.normalize import normalize_observations
    from perceptkit.contracts.report import ReportEnvelope
    from perceptkit.manifest import MINIMAL_SIGNALS

    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[ANCHOR_CHANGED])
    kit.ingest(anchor("home", "08:00"), context=ctx("08:00"))
    state_before = dict(s.rule_state)

    envelope = ReportEnvelope.parse(anchor("office", "09:00"))
    item = normalize_observations(
        envelope.observations, context=ctx("09:00"), signals=MINIMAL_SIGNALS,
        source="ios",
    ).normalized[0]

    first = evaluate_and_enqueue(item, context=ctx("09:00"), storage=s,
                                 definitions=[ANCHOR_CHANGED])
    s.rule_state = dict(state_before)          # 规则状态没提交上
    second = evaluate_and_enqueue(item, context=ctx("09:00"), storage=s,
                                  definitions=[ANCHOR_CHANGED])

    assert len(first.events) == 1
    assert not second.events
    assert ("anchor_changed", "事件已在发件箱中") in second.misses
    assert len(s.outbox) == 1


def test_occurrence_rule_ids_are_byte_for_byte_what_they_were():
    """occurrence 从来就按上游事件 id 算，没有这个缺陷 —— 它的 id 不许跟着变。"""
    from perceptkit.processing import event_id_for
    rule = EventDefinition(
        definition_id="presence", version=1, signal="presence_recovery",
        condition_type="occurrence", event_type="unlock_after_absence",
    )
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[rule])
    out = kit.ingest({
        "schema_version": 1, "report_id": "p1", "producer": "ios",
        "observations": [{
            "signal": "presence_recovery", "signal_schema_version": 1,
            "occurred_at": t("09:00").isoformat(), "availability": "observed",
            "source_event_id": "pr-1",
            "value": {"recovered_at": t("09:00").isoformat(),
                      "absence_seconds": 3600, "absence_quality": "measured",
                      "evidence": "app_became_active"},
        }],
    }, context=ctx("09:00"))
    assert [e.event_id for e in out.events] == [event_id_for(
        subject_id="u1", definition=rule, scope="2026-09-01@v1", trigger="pr-1")]


# ---------------------------------------------------------------------------
# 缺陷二：前置条件 —— 规则要能看同一条观测里的别的字段
# ---------------------------------------------------------------------------

def arrived_while_connected() -> EventDefinition:
    return EventDefinition(
        definition_id="anchor_arrived", version=1, signal="proximity_anchor",
        condition_type="changed", field_name="anchor_id",
        when={"is_connected": True},
        event_type="arrived_at_anchor", lifecycle=EVERY_TIME,
    )


def test_a_late_disconnect_from_home_is_not_an_arrival_and_does_not_eat_the_real_one():
    """家(连着) -> 公司(连着) -> 家(断开，迟到的上报) -> 家(连着)。

    断开那条不能叫醒人。**而且它不能推进规则状态** —— 推进了的话前值变成
    home，真正回到家那一次就成了"没变"，该叫的那一次反而被吞掉。
    """
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s, definitions=[arrived_while_connected()])
    kit.ingest(anchor("home", "08:00"), context=ctx("08:00"))
    kit.ingest(anchor("office", "09:00"), context=ctx("09:00"))

    late = kit.ingest(anchor("home", "09:30", connected=False), context=ctx("09:30"))
    assert not late.events
    assert any(rid == "anchor_arrived" and "前置条件" in (why or "")
               for rid, why in late.rule_misses)

    back = kit.ingest(anchor("home", "19:00"), context=ctx("19:00"))
    assert [(e.previous, e.current) for e in back.events] == [("office", "home")]
    assert len(s.outbox) == 2


def test_the_precondition_is_part_of_the_dict_rule_language():
    rule = EventDefinition.parse({
        "id": "anchor_arrived", "version": 1,
        "source": {"signal": "proximity_anchor", "field": "anchor_id",
                   "when": {"is_connected": True}},
        "condition": {"type": "changed"},
        "event": {"type": "arrived_at_anchor"},
    })
    assert dict(rule.when) == {"is_connected": True}


def test_a_rule_without_a_precondition_has_an_empty_one():
    assert dict(ANCHOR_CHANGED.when) == {}


def test_a_precondition_is_compared_strictly_not_by_python_truthiness():
    """``1 == True`` 在 Python 里成立。前置条件要的是"就是这个值"。"""
    from perceptkit.rules import precondition_met
    rule = EventDefinition(
        definition_id="r", version=1, signal="s", condition_type="changed",
        event_type="t", field_name="x", when={"flag": True},
    )
    assert precondition_met(rule, {"flag": True})[0]
    assert not precondition_met(rule, {"flag": 1})[0]
    assert not precondition_met(rule, {})[0]          # 没有这个字段 = 不满足


@pytest.mark.parametrize("bad", [
    ["is_connected"],                     # 不是对象
    {"": True},                           # 空字段名
    {"nested": {"a": 1}},                 # 只比标量 —— 这不是表达式 DSL
])
def test_a_malformed_precondition_is_refused_at_construction(bad):
    with pytest.raises(ContractError):
        EventDefinition(definition_id="r", version=1, signal="s",
                        condition_type="changed", event_type="t",
                        field_name="x", when=bad)


@pytest.mark.parametrize("kind", ["streak", "absence"])
def test_a_precondition_on_a_clock_driven_rule_is_refused(kind):
    """streak / absence 由时钟驱动，没有"这条观测"可以看 —— 配上前置条件
    就永远不满足，永远不响，也不报错。配不出来比配出来不生效好。"""
    with pytest.raises(ContractError):
        EventDefinition(definition_id="r", version=1, signal="s",
                        condition_type=kind, event_type="t",
                        when={"is_connected": True})
