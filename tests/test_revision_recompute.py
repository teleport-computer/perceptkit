"""修订过的那天，重算不能把错值和改正值一起折进去。

外部审查（2026-09-02，P0-6）："当前 source_revision 可以帮助较高 revision
替换 Current，但不足以正确处理历史和聚合"。核下来成立：``source_revision``
原来**只**用在当前值上，历史和聚合完全没读它。于是

    体重 70.5kg（revision 1）→ 用户在健康 app 里改成 68.5（revision 2）
    两条都在明细里 → 重算那天 → 69.5，一个从来没发生过的数字

而且这个错**只有重算时才现形**：当前值那条路是对的，所以"改完之后当前显示
对了"会让人以为整条链路都对了。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit.contracts.records import StoredObservation
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.processing.recompute import recompute_day

DAY = date(2026, 9, 2)
T = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)


def _obs(storage, oid, *, value, event_id, revision=None, minutes=0,
         availability="observed"):
    storage.append_observation(StoredObservation(
        subject_id="u", observation_id=oid, signal="health_vitals",
        signal_schema_version=1, source="ios",
        occurred_at=T + timedelta(minutes=minutes), received_at=T,
        availability=availability, effective_local_date=DAY,
        typed_value=value, timezone="Asia/Shanghai",
        source_event_id=event_id, source_revision=revision, created_at=T,
    ))


def _rhr(storage):
    agg = recompute_day(storage, MINIMAL_SIGNALS["health_vitals"],
                        subject_id="u", day=DAY, version=1, updated_at=T)
    return (agg.typed_aggregate.get("resting_heart_rate"),
            agg.source_coverage["observations"])


def test_a_corrected_sample_replaces_the_wrong_one_instead_of_averaging_with_it():
    s = InMemoryStorage()
    _obs(s, "o1", value={"resting_heart_rate": 90}, event_id="hk-sample-A", revision=1)
    _obs(s, "o2", value={"resting_heart_rate": 60}, event_id="hk-sample-A", revision=2,
         minutes=5)
    stats, n = _rhr(s)
    assert n == 1, "错值和改正值被一起折进去了"
    assert stats["min"] == stats["max"] == 60, (
        f"折出来是 {stats} —— 90 和 60 一起折成了平均值 75，一个从来没发生过的心率")


def test_revisions_are_compared_as_numbers_not_as_text():
    """全按字符串比的话 "10" < "9"，第 10 版会被第 9 版盖掉。"""
    s = InMemoryStorage()
    _obs(s, "o1", value={"resting_heart_rate": 90}, event_id="hk-A", revision=9)
    _obs(s, "o2", value={"resting_heart_rate": 60}, event_id="hk-A", revision=10,
         minutes=5)
    stats, n = _rhr(s)
    assert n == 1 and stats["max"] == 60


def test_different_source_facts_are_never_merged():
    """两个不同的样本是两件事，不是一件事的两个版本。"""
    s = InMemoryStorage()
    _obs(s, "o1", value={"resting_heart_rate": 70}, event_id="hk-A", revision=1)
    _obs(s, "o2", value={"resting_heart_rate": 80}, event_id="hk-B", revision=1,
         minutes=5)
    _, n = _rhr(s)
    assert n == 2


def test_a_group_with_no_revisions_at_all_is_left_alone():
    """没有修订号 = 没有「哪版更新」的信息。

    这时候合并就等于替这些观测编一个"后来的覆盖先来的"顺序 —— 而那恰好
    **不是** cumulative 现在采用的规则（它取 max）。编了的话，同一份数据
    重算和增量折叠会给出两个不同的数。
    """
    s = InMemoryStorage()
    _obs(s, "o1", value={"resting_heart_rate": 70}, event_id="hk-A")
    _obs(s, "o2", value={"resting_heart_rate": 80}, event_id="hk-A", minutes=5)
    _, n = _rhr(s)
    assert n == 2


def test_a_revision_marked_unavailable_removes_the_fact_from_the_day():
    """来源侧撤回：最高修订说"读不到了"，那天就不该再有这条事实。

    ⚠️ 这条只覆盖「修订带着 availability 一起来」的形态。iOS 真正的删除走的是
    HealthKit 的 deletedObjects，**现在根本没接**，见 P0-6 的交付说明。
    """
    s = InMemoryStorage()
    _obs(s, "o1", value={"resting_heart_rate": 90}, event_id="hk-A", revision=1)
    _obs(s, "o2", value=None, event_id="hk-A", revision=2, minutes=5,
         availability="unavailable")
    _, n = _rhr(s)
    assert n == 0
