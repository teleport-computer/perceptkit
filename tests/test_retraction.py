"""来源撤回：记下、当前值重选、重算排除。

外部审查（seven §5.3/§5.4）要求：来源删掉一条事实之后，具体数值不再可用，
Current 重选下一条仍有效的，受影响日期重算；而 last known **不能来自
tombstone**。

这个文件测的是**行为**，尤其是撤回和 `unavailable` 那两条**方向相反**的路：
把它们混成一个状态位，被删掉的数值会作为 last_known 继续显示 ——
正好是撤回最不该发生的事。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.retraction import Retraction
from perceptkit.manifest import MINIMAL_SIGNALS

T0 = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
DAY = date(2026, 9, 6)


def _kit(storage):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)


def _weigh(kit, storage, kg, *, at, eid):
    out = kit.ingest({
        "schema_version": 1, "report_id": f"r-{eid}", "producer": "ios",
        "observations": [{
            "signal": "health_weight", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "source_event_id": eid,
            "value": {"weight_kg": kg},
        }],
    }, context=IngestContext("u", at))
    assert not out.rejected, list(out.rejected)


def _current(storage):
    rows = storage.get_current(subject_id="u", signals=["health_weight"])["health_weight"]
    return (rows[0].typed_value, rows[0].availability) if rows else (None, None)


def _retract(eid, at=T0 + timedelta(hours=5)):
    return Retraction("u", "health_weight", eid, "ios", at)


# ---------------------------------------------------------------------------
# 当前值重选 —— 和 unavailable 方向相反的那一半
# ---------------------------------------------------------------------------

def test_retracting_the_current_value_reselects_the_previous_one():
    """用户在健康 app 里删掉今天那次称重 → 当前值回到上一次，不是留着被删的。"""
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    _weigh(kit, s, 69.8, at=T0 + timedelta(hours=2), eid="hk-B")
    assert _current(s)[0]["weight_kg"] == 69.8

    kit.apply_retractions([_retract("hk-B")], now=T0 + timedelta(hours=5))
    value, availability = _current(s)
    assert value["weight_kg"] == 70.5, f"当前值没重选，还是 {value}"
    assert availability == "observed"


def test_the_retracted_number_does_not_survive_as_last_known():
    """🔴 这条是撤回和 unavailable 的分水岭。

    unavailable 的既有行为是保留上一个可靠值当 last_known。撤回如果复用
    那条路，用户删掉的那个数字会继续被 agent 说出来。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=5))

    value, availability = _current(s)
    assert value is None, f"被撤回的数值作为 last_known 留下来了：{value}"
    assert availability == "no_data"


def test_an_unavailable_report_still_keeps_last_known():
    """反过来验：传感器读不到时**要**保留上一个值。两条路不能混。"""
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    kit.ingest({
        "schema_version": 1, "report_id": "r-off", "producer": "ios",
        "observations": [{
            "signal": "health_weight", "signal_schema_version": 1,
            "occurred_at": (T0 + timedelta(hours=1)).isoformat(),
            "availability": "unavailable", "timezone": "Asia/Shanghai",
            "source_event_id": "hk-A",
        }],
    }, context=IngestContext("u", T0 + timedelta(hours=1)))
    value, availability = _current(s)
    assert value["weight_kg"] == 70.5, "读不到时把 last_known 也抹掉了"
    assert availability == "unavailable"


def test_retracting_something_that_is_not_current_leaves_current_alone():
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    _weigh(kit, s, 69.8, at=T0 + timedelta(hours=2), eid="hk-B")
    kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=5))
    assert _current(s)[0]["weight_kg"] == 69.8


# ---------------------------------------------------------------------------
# 幂等 —— 重传/崩溃重放不能算两次
# ---------------------------------------------------------------------------

def test_the_same_retraction_twice_counts_once():
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    first = kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=5))
    again = kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=6))
    assert first["recorded"] == 1
    assert again["recorded"] == 0, "重传被算成了第二次撤回"


# ---------------------------------------------------------------------------
# 观测留着 —— 不是就地删除
# ---------------------------------------------------------------------------

def test_the_observation_survives_so_the_gap_stays_explainable():
    """抹掉观测的话，「这天为什么少一块」就再也答不出来。

    agent 要能说「那天曾经有条记录，后来被来源删了」，而不是那天凭空没有。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    before = len(s.observations)
    kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=5))
    assert len(s.observations) == before, "撤回把观测就地删了"
    assert s.list_retractions(subject_id="u", signal="health_weight")


# ---------------------------------------------------------------------------
# 重算排除
# ---------------------------------------------------------------------------

def test_a_retracted_fact_is_left_out_of_the_recomputed_day():
    from perceptkit.processing.recompute import recompute_day
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    _weigh(kit, s, 90.0, at=T0 + timedelta(hours=2), eid="hk-bogus")

    agg = recompute_day(s, MINIMAL_SIGNALS["health_weight"], subject_id="u",
                        day=DAY, version=1, updated_at=T0)
    assert agg.source_coverage["observations"] == 2

    kit.apply_retractions([_retract("hk-bogus")], now=T0 + timedelta(hours=5))
    agg = recompute_day(s, MINIMAL_SIGNALS["health_weight"], subject_id="u",
                        day=DAY, version=1, updated_at=T0)
    assert agg.source_coverage["observations"] == 1, "被撤回的还在参与折聚合"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_a_retraction_needs_a_target():
    with pytest.raises(ValueError, match="source_event_id"):
        Retraction("u", "health_weight", "", "ios", T0)


def test_purge_takes_retractions_too():
    """漏一类就是删不干净，而「删除我的数据」没有部分成功。"""
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, s, 70.5, at=T0, eid="hk-A")
    kit.apply_retractions([_retract("hk-A")], now=T0 + timedelta(hours=5))
    counts = s.purge_subject(subject_id="u")
    assert counts.get("retractions") == 1
    assert not s.list_retractions(subject_id="u", signal="health_weight")
