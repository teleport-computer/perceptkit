"""处理管线的前七步。

这些测试盯的是**顺序和幂等**，不是算法 —— 算法早就有测试了。上一版缺的
恰恰是这一层：算式都在，但"谁在什么时候调"留在了宿主那边。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts import (
    INGEST_ACCEPTED,
    INGEST_CONFLICT,
    INGEST_DUPLICATE,
    IngestContext,
    ReportEnvelope,
)
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.processing import ingest_report

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 27, 10, 30, 4, tzinfo=SH)


def ctx(subject="user_1", now=NOW, scope=None):
    return IngestContext(subject_id=subject, received_at=now, auth_scope=scope)


def steps_obs(count=3012, at="2026-08-27T10:30:00+08:00", sample="hk-A1", **over):
    obs = {
        "signal": "steps", "signal_schema_version": 1, "occurred_at": at,
        "availability": "observed", "source_event_id": sample,
        "value": {"step_count": count, "local_date": at[:10]},
    }
    obs.update(over)
    return obs


def report(observations=None, report_id="r1", producer="ios"):
    return ReportEnvelope.parse({
        "schema_version": 1, "report_id": report_id, "producer": producer,
        "observations": observations if observations is not None else [steps_obs()],
    })


def run(storage, rep=None, context=None, **kw):
    return ingest_report(
        rep or report(), context=context or ctx(), storage=storage,
        signals=MINIMAL_SIGNALS, **kw,
    )


# ---------------------------------------------------------------------------
# 走通一遍
# ---------------------------------------------------------------------------

def test_a_report_lands_as_observation_current_and_aggregate():
    s = InMemoryStorage()
    out = run(s)
    assert out.receipt.status == INGEST_ACCEPTED
    assert out.ok and len(out.applied) == 1
    assert len(s.observations) == 1
    assert len(s.current) == 1
    assert len(s.aggregates) == 1


def test_the_aggregate_actually_carries_the_number():
    s = InMemoryStorage()
    run(s)
    doc = next(iter(s.aggregates.values())).typed_aggregate
    # cumulative 的形状是 {字段: {"total": 日内最大值}} —— 日内单调累加,
    # 当天的代表值取 max 而不是求和(否则每次上报都会被再加一遍)。
    assert doc["step_count"]["total"] == 3012


def test_signals_that_keep_no_history_write_no_aggregate():
    s = InMemoryStorage()
    run(s, report([{
        "signal": "battery", "signal_schema_version": 1,
        "occurred_at": "2026-08-27T10:30:00+08:00", "availability": "observed",
        "value": {"level_ratio": 0.42, "is_charging": False},
    }]))
    assert len(s.current) == 1
    assert len(s.aggregates) == 0


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_replaying_the_same_report_is_a_no_op():
    """重传是常态不是异常：网络抖一下客户端就会重发。"""
    s = InMemoryStorage()
    run(s)
    again = run(s)
    assert again.receipt.status == INGEST_DUPLICATE
    assert len(s.observations) == 1
    assert next(iter(s.aggregates.values())).typed_aggregate["step_count"]["total"] == 3012


def test_the_same_report_id_with_different_content_is_a_conflict():
    """静默覆盖会让"到底哪份数据生效了"永远说不清。"""
    s = InMemoryStorage()
    run(s)
    out = run(s, report([steps_obs(count=9999)]))
    assert out.receipt.status == INGEST_CONFLICT
    assert len(s.observations) == 1


def test_the_same_observation_in_a_new_report_is_deduped_not_double_counted():
    """批级幂等挡不住这种：客户端换了 report_id 重发同一条观测。
    靠观测级的去重身份挡。"""
    s = InMemoryStorage()
    run(s)
    out = run(s, report([steps_obs()], report_id="r2"))
    assert len(out.duplicates) == 1 and not out.applied
    assert len(s.observations) == 1


def test_dedupe_still_works_after_the_detail_rows_are_gone():
    """明细按保留期删掉之后，去重身份是唯一还能回答"这条处理过没有"的东西。
    没有它，第 8 天的一次重放就会把永久聚合的数字加两遍。"""
    s = InMemoryStorage()
    run(s)
    s.observations.clear()                      # 模拟保留期清理
    out = run(s, report([steps_obs()], report_id="r3"))
    assert len(out.duplicates) == 1
    assert not s.observations                   # 没有被当成新数据重新写入


# ---------------------------------------------------------------------------
# 乱序与迟到
# ---------------------------------------------------------------------------

def test_late_arriving_old_data_enters_history_but_does_not_move_current():
    s = InMemoryStorage()
    run(s, report([steps_obs(count=5000, at="2026-08-27T12:00:00+08:00", sample="hk-B")]))
    run(s, report([steps_obs(count=100, at="2026-08-27T08:00:00+08:00", sample="hk-A")],
                  report_id="r2"))
    assert len(s.observations) == 2                       # 两条都进了历史
    current = next(iter(s.current.values()))
    assert current.typed_value["step_count"] == 5000      # 当前值没被拉回去


def test_a_higher_revision_at_the_same_instant_corrects_the_current_value():
    """产品规范的缺口（OPEN-QUESTIONS B15）：只比 occurred_at 的话，
    用户在健康 App 里改掉的误录值永远覆盖不了当前值。"""
    s = InMemoryStorage()
    at = "2026-08-27T10:30:00+08:00"
    run(s, report([steps_obs(count=95000, at=at, sample="hk-X", source_revision=1)]))
    run(s, report([steps_obs(count=9500, at=at, sample="hk-Y", source_revision=2)],
                  report_id="r2"))
    assert next(iter(s.current.values())).typed_value["step_count"] == 9500


def test_same_instant_same_revision_different_content_is_surfaced_not_swallowed():
    s = InMemoryStorage()
    at = "2026-08-27T10:30:00+08:00"
    run(s, report([steps_obs(count=1, at=at, sample="hk-X")]))
    out = run(s, report([steps_obs(count=2, at=at, sample="hk-Y")], report_id="r2"))
    assert len(out.conflicts) == 1
    assert not out.ok


# ---------------------------------------------------------------------------
# 三态
# ---------------------------------------------------------------------------

def test_no_data_does_not_overwrite_the_last_reliable_value():
    """没戴表 ≠ 睡了 0 分钟。"""
    s = InMemoryStorage()
    run(s)
    run(s, report([{
        "signal": "steps", "signal_schema_version": 1,
        "occurred_at": "2026-08-27T23:00:00+08:00",
        "availability": "no_data", "source_event_id": "hk-none",
    }], report_id="r2"))
    assert next(iter(s.current.values())).typed_value["step_count"] == 3012
    assert len(s.aggregates) == 1               # 也没进聚合


# ---------------------------------------------------------------------------
# 授权与上限
# ---------------------------------------------------------------------------

def test_a_signal_outside_the_auth_scope_is_refused():
    """用户关掉了权限，设备却还在发 —— 挡在这里，不能等写库才发现。"""
    s = InMemoryStorage()
    out = run(s, context=ctx(scope=frozenset({"battery"})))
    assert out.rejected and not s.observations


def test_an_oversized_batch_is_refused_rather_than_truncated():
    """截断会静默丢数据，比拒收难查得多。"""
    s = InMemoryStorage()
    many = [steps_obs(sample=f"hk-{i}") for i in range(5)]
    out = run(s, report(many), max_observations=3)
    assert out.receipt.error_code == "too_many_observations"
    assert not s.observations


def test_one_bad_observation_does_not_take_down_the_whole_batch():
    """一批十条里两条有问题，因为它们把另外八条一起丢掉是最容易让人骂街的设计。"""
    s = InMemoryStorage()
    out = run(s, report([
        steps_obs(sample="ok-1"),
        steps_obs(sample="bad", value={"step_count": -5, "local_date": "2026-08-27"}),
        steps_obs(sample="ok-2", at="2026-08-27T11:00:00+08:00"),
    ]))
    assert len(out.applied) == 2 and len(out.rejected) == 1
    assert "小于下限" in out.rejected[0][1][0]


def test_an_unknown_signal_is_rejected_without_touching_the_others():
    s = InMemoryStorage()
    out = run(s, report([
        steps_obs(sample="ok"),
        {"signal": "telepathy", "signal_schema_version": 1,
         "occurred_at": "2026-08-27T10:30:00+08:00",
         "availability": "observed", "value": {}},
    ]))
    assert len(out.applied) == 1 and len(out.rejected) == 1


# ---------------------------------------------------------------------------
# 顺序与事务边界
# ---------------------------------------------------------------------------

def test_the_whole_batch_lands_inside_one_transaction():
    """🔴 整批一个事务，不是每条观测一个。

    以前是"先认领这批、再逐条各自提交"。第 1 条提交后崩溃，重试会直接拿到
    duplicate（批级幂等认为处理过了），**剩下的观测永久丢失** —— 一次中断被
    伪装成了"已处理完"。代价是单个事务变长，所以 max_observations 是必须的。
    """
    s = InMemoryStorage()
    run(s, report([steps_obs(sample="a"), steps_obs(sample="b",
                                                    at="2026-08-27T11:00:00+08:00")]))
    assert s.transactions_opened == 1
    assert s.transaction_depth == 0


def test_subjects_are_isolated_from_each_other():
    s = InMemoryStorage()
    run(s, context=ctx("user_1"))
    run(s, report(report_id="r2"), context=ctx("user_2"))
    assert len({o.subject_id for o in s.observations.values()}) == 2
    got = s.get_current(subject_id="user_1", signals=["steps"])
    assert len(got["steps"]) == 1


def test_purging_a_subject_removes_every_kind_of_record():
    """"删除我的数据"这件事没有部分成功。"""
    s = InMemoryStorage()
    run(s, context=ctx("user_1"))
    run(s, report(report_id="r2"), context=ctx("user_2"))
    counts = s.purge_subject(subject_id="user_1")
    assert counts["observations"] == 1 and counts["identities"] == 1
    assert all(o.subject_id == "user_2" for o in s.observations.values())
    assert all(k[0] == "user_2" for k in s.current)


# ---------------------------------------------------------------------------
# 归属日期
# ---------------------------------------------------------------------------

def test_the_day_comes_from_the_local_date_the_source_declared():
    s = InMemoryStorage()
    run(s, report([steps_obs(at="2026-08-27T23:30:00+08:00")]))
    assert next(iter(s.aggregates.values())).local_date.isoformat() == "2026-08-27"


def test_a_missing_local_date_falls_back_loudly_not_silently():
    """静默换算法是最难查的一类问题 —— 数字对不上，但没有任何地方报错。"""
    s = InMemoryStorage()
    out = run(s, report([steps_obs(value={"step_count": 100})]))
    assert out.applied
    assert any("local_date" in w for w in out.warnings)
