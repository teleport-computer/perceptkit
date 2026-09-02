"""统一的清理入口 —— 规则只有一份。

外部审查（2026-09-02，P0-1）："Retention 现在是声明，不是完整执行链路"。
成立：包里只有保留期表和查询函数，没有任何东西真的执行；于是每个宿主自己
照 manifest 推导一遍。io 那边写了一百多行 —— 下一个宿主还得再写一遍，
而这条路上的每个坑**错了都不报错**：

    明细和聚合当成一个保留期   「8月1日新增了 5 张照片」一年后被扫掉
    忘了跳过 PERMANENT        永久聚合被删，且不可逆
    没声明保留期就默认一个     拿真实数据去赌一个猜出来的天数
    去重身份跟着明细一起删     重放的上报把永久聚合数两遍，无法回滚

所以这个文件测的是**规则**，不是"函数能跑"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.manifest.types import PERMANENT
from perceptkit.retention import plan_retention

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _plan():
    return plan_retention(MINIMAL_SIGNALS, now=NOW)


def test_details_and_aggregates_get_their_own_cutoffs():
    """两个保留期是两条截止线，不是一条。

    典型形态是「明细短、聚合永久」——合成一条的话，要么把该留的永久聚合
    删了，要么把该清的明细一直留着。
    """
    plan = _plan()
    photo = [a for a in plan.actions if a.signal == "photo_library_added"]
    # 明细 7 天要清，聚合永久不许出现在动作里。
    assert [a.kind for a in photo] == ["observations"]
    assert photo[0].before == (NOW - timedelta(days=7)).date()
    assert ("photo_library_added", "日聚合永久保存") in plan.skipped


def test_permanent_never_appears_as_something_to_delete():
    plan = _plan()
    doomed = {(a.signal, a.kind) for a in plan.actions}
    for key, sig in MINIMAL_SIGNALS.items():
        if sig.history_retention_days == PERMANENT:
            assert (key, "observations") not in doomed, f"{key} 的明细是永久的"
        if sig.effective_aggregate_retention_days == PERMANENT:
            assert (key, "aggregates") not in doomed, f"{key} 的聚合是永久的"


def test_a_signal_with_no_declared_retention_is_skipped_not_defaulted():
    """没声明就跳过。默认一个天数 = 拿真实数据赌一个猜出来的数字。"""
    from dataclasses import replace
    sig = replace(MINIMAL_SIGNALS["audio_route"],
                  history_retention_days=None, aggregate_retention_days=None)
    plan = plan_retention({"audio_route": sig}, now=NOW)
    assert plan.actions == []
    assert all("不替它猜" in why for _, why in plan.skipped)


def test_the_plan_says_why_it_skipped_things():
    """只说"删了 0 条"的报告，读不出「是没到期，还是规则写错了」。"""
    plan = _plan()
    assert plan.skipped
    assert all(why.strip() for _, why in plan.skipped)


def test_a_dry_run_removes_nothing():
    """这是包里唯一会永久删用户数据的动作。默认不删。"""
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    at = NOW - timedelta(days=400)
    kit.ingest({
        "schema_version": 1, "report_id": "r1", "producer": "ios",
        "observations": [{
            "signal": "audio_route", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai",
            "value": {"output_type": "bluetooth_a2dp", "is_bluetooth": True},
        }],
    }, context=IngestContext("u", at))
    before = len(storage.observations)
    assert before

    out = kit.run_retention(subject_id="u", now=NOW)          # 默认 dry_run
    assert out["applied"] is False and out["removed"] == {}
    assert len(storage.observations) == before, "试跑不该删任何东西"

    out = kit.run_retention(subject_id="u", now=NOW, dry_run=False)
    assert out["applied"] is True and out["removed"]
    assert len(storage.observations) < before


def test_the_sweep_does_not_touch_dedupe_identities():
    """明细没了之后，去重身份是「重放的上报会不会把永久聚合数两遍」
    之间唯一的东西 —— 而那个错不可逆。"""
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    at = NOW - timedelta(days=400)
    kit.ingest({
        "schema_version": 1, "report_id": "p1", "producer": "ios",
        "observations": [{
            "signal": "photo_library_added", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "source_event_id": "asset-1",
            "value": {"count": 1, "added_at": at.isoformat()},
        }],
    }, context=IngestContext("u", at))
    identities = set(storage.identities)
    assert identities

    kit.run_retention(subject_id="u", now=NOW, dry_run=False)
    assert set(storage.identities) == identities, "去重身份被清理带走了"

    # 明细被清掉之后重放同一条上报：永久新增数**不能**变成 2。
    def daily():
        rows = storage.get_aggregate(subject_id="u", signal="photo_library_added",
                                     start_date=at.date(), end_date=at.date())
        return rows[0].typed_aggregate["count"]["total"] if rows else 0
    was = daily()
    kit.ingest({
        "schema_version": 1, "report_id": "p1-replay", "producer": "ios",
        "observations": [{
            "signal": "photo_library_added", "signal_schema_version": 1,
            "occurred_at": at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "source_event_id": "asset-1",
            "value": {"count": 1, "added_at": at.isoformat()},
        }],
    }, context=IngestContext("u", NOW))
    assert daily() == was, "清理之后重放把永久聚合数了两遍"


def test_the_sweep_stays_inside_one_subject():
    """跨用户的一条 DELETE 少写一个 WHERE 就会删掉别人的数据。"""
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
    at = NOW - timedelta(days=400)
    for who in ("u1", "u2"):
        kit.ingest({
            "schema_version": 1, "report_id": f"r-{who}", "producer": "ios",
            "observations": [{
                "signal": "audio_route", "signal_schema_version": 1,
                "occurred_at": at.isoformat(), "availability": "observed",
                "timezone": "Asia/Shanghai",
                "value": {"output_type": "bluetooth_a2dp", "is_bluetooth": True},
            }],
        }, context=IngestContext(who, at))
    others = [o for o in storage.observations.values() if o.subject_id == "u2"]
    assert others

    kit.run_retention(subject_id="u1", now=NOW, dry_run=False)
    still = [o for o in storage.observations.values() if o.subject_id == "u2"]
    assert len(still) == len(others), "清 u1 把 u2 的数据也删了"
