"""同一个样本再报一次 ≠ 又发生了一件事（外部审查 F1 / F5，2026-09-14）。

来源给了稳定样本 id 的信号（睡眠、运动、体重这些 HealthKit 样本），有两件
长得像、后果相反的事：

    同一晚的两段 core     两条**不同**的样本，各有 id、各有起止 —— 都得留下
    同一段 core 重传一次  同一条样本又投递了一遍 —— 只能算一次

现在两件都判错了，而且错的方向相反：前者被当成"状态没变"丢掉明细（那一晚
只剩第一段），后者被当成"新投递"重复累加（30 分钟变 60）。用户看到的是
"昨晚睡了 30 分钟"和"昨晚睡了 60 分钟"，同一晚两个都不对。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS

T0 = datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc)


def _kit(storage):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)


def _sleep_segment(kit, *, eid, stage, minutes, start, report_at):
    """一段睡眠。``report_at`` 是这次上报把它标成什么时刻。"""
    out = kit.ingest({
        "schema_version": 1, "report_id": f"r-{eid}-{report_at.isoformat()}",
        "producer": "ios",
        "observations": [{
            "signal": "health_sleep", "signal_schema_version": 1,
            "occurred_at": report_at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "source_event_id": eid,
            "value": {
                "stage": stage, "duration_minutes": minutes,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(minutes=minutes)).isoformat(),
            },
        }],
    }, context=IngestContext("u", report_at))
    assert not out.rejected, list(out.rejected)
    return out


def _slept(storage, kit):
    rows, _ = kit.list_timeline(subject_id="u", signal="health_sleep", limit=100)
    return rows


def _daily(storage):
    from perceptkit.processing.recompute import recompute_day
    agg = recompute_day(storage, MINIMAL_SIGNALS["health_sleep"], subject_id="u",
                        day=(T0 + timedelta(hours=8)).date(), version=2, updated_at=T0)
    return agg.typed_aggregate


# ---------------------------------------------------------------------------
# F1：同一晚的两段同阶段睡眠，两条都得留下
# ---------------------------------------------------------------------------

def test_two_segments_of_the_same_stage_are_two_facts():
    """30 分钟一段、45 分钟一段，都是 core —— 那一晚是 75 分钟，不是 30。

    ``stage`` 声明了 ``state_change``（本意是"状态没变就不写明细"，防的是
    iOS 每 5 分钟一次的保活上报）。但睡眠每一段都带着自己的样本 id 和起止
    时间，是**独立的事实**，不是同一个状态的重复播报 —— 判成重复就把那一晚
    的一半直接丢了，而且不报错。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _sleep_segment(kit, eid="seg-1", stage="core", minutes=30,
                   start=T0, report_at=T0 + timedelta(hours=8))
    _sleep_segment(kit, eid="seg-2", stage="core", minutes=45,
                   start=T0 + timedelta(hours=1), report_at=T0 + timedelta(hours=8, minutes=1))

    rows = _slept(s, kit)
    assert len(rows) == 2, f"第二段被当成「状态没变」丢了，只剩 {len(rows)} 条"
    total = (_daily(s).get("duration_minutes") or {}).get("total")
    assert total == 75, f"那一晚应该是 75 分钟，实际 {total}"


# ---------------------------------------------------------------------------
# F5：同一段睡眠重传一次，不许算两遍
# ---------------------------------------------------------------------------

def test_re_uploading_the_same_sample_does_not_count_twice():
    """同一个样本 id、同一段时间、同样 30 分钟，隔一会儿再报一次。

    投递去重的身份里带了 ``occurred_at``，而 iOS 侧把**这次上报的时刻**
    写进了每一段 —— 于是重传就成了"新投递"，那一晚从 30 分钟变成 60。
    重传是常态：网络抖一下、app 被挂起，客户端就会重来一次。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _sleep_segment(kit, eid="seg-1", stage="core", minutes=30,
                   start=T0, report_at=T0 + timedelta(hours=8))
    _sleep_segment(kit, eid="seg-1", stage="core", minutes=30,
                   start=T0, report_at=T0 + timedelta(hours=9))   # 同一段，换个上报时刻

    total = (_daily(s).get("duration_minutes") or {}).get("total")
    assert total == 30, f"同一段睡眠被算了两遍：{total}"


def test_re_uploading_the_same_workout_does_not_count_twice():
    """运动同理：同一次 30 分钟的跑步重传，不许变成 60 分钟、两次。"""
    s = InMemoryStorage(); kit = _kit(s)
    for report_at in (T0 + timedelta(hours=8), T0 + timedelta(hours=9)):
        out = kit.ingest({
            "schema_version": 1, "report_id": f"r-{report_at.isoformat()}",
            "producer": "ios",
            "observations": [{
                "signal": "health_workout", "signal_schema_version": 1,
                "occurred_at": report_at.isoformat(), "availability": "observed",
                "timezone": "Asia/Shanghai", "source_event_id": "wk-1",
                "value": {"workout_type": "running", "duration_minutes": 30,
                          "active_energy_kcal": 250},
            }],
        }, context=IngestContext("u", report_at))
        assert not out.rejected, list(out.rejected)

    from perceptkit.processing.recompute import recompute_day
    agg = recompute_day(s, MINIMAL_SIGNALS["health_workout"], subject_id="u",
                        day=(T0 + timedelta(hours=8)).date(), version=2, updated_at=T0)
    total = (agg.typed_aggregate.get("duration_minutes") or {}).get("total")
    assert total == 30, f"同一次运动被算了两遍：{agg.typed_aggregate}"


def test_a_real_revision_still_gets_through():
    """反向守卫：来源**修订**了这条样本（带新版本号），必须照常收下。

    去重收紧过头的后果是另一类静默错误：用户在健康 app 里把 30 分钟改成
    45，我们还显示 30，而且没有任何地方报错。
    """
    s = InMemoryStorage(); kit = _kit(s)
    for minutes, rev in ((30, 1), (45, 2)):
        kit.ingest({
            "schema_version": 1, "report_id": f"r-{rev}", "producer": "ios",
            "observations": [{
                "signal": "health_workout", "signal_schema_version": 1,
                "occurred_at": (T0 + timedelta(hours=8)).isoformat(),
                "availability": "observed", "timezone": "Asia/Shanghai",
                "source_event_id": "wk-1", "source_revision": rev,
                "value": {"workout_type": "running", "duration_minutes": minutes},
            }],
        }, context=IngestContext("u", T0 + timedelta(hours=8)))

    from perceptkit.processing.recompute import recompute_day
    agg = recompute_day(s, MINIMAL_SIGNALS["health_workout"], subject_id="u",
                        day=(T0 + timedelta(hours=8)).date(), version=2, updated_at=T0)
    total = (agg.typed_aggregate.get("duration_minutes") or {}).get("total")
    assert total == 45, f"修订没收下，还停在旧值：{agg.typed_aggregate}"


# ---------------------------------------------------------------------------
# 升级兼容：旧数据记的是旧身份，不许因此再加一遍
# ---------------------------------------------------------------------------

def test_a_bare_legacy_digest_blocks_unproven_replay_as_incomplete():
    """只剩不透明旧摘要时，不能用本次上报时间猜原始身份。

    这是旧的 synthetic fixture，不含任何可恢复的 Fact 证据；应明确报告
    incomplete。真实 v0.8 状态存在明细或 Current 的迁移由 A03 验证。
    """
    from perceptkit.contracts.observation import Observation
    from perceptkit.contracts.records import DurableDedupeIdentity
    from perceptkit.processing.normalize import normalize_observations

    s = InMemoryStorage(); kit = _kit(s)
    report_at = T0 + timedelta(hours=8)
    value = {"workout_type": "running", "duration_minutes": 30}
    obs = Observation(
        signal="health_workout", signal_schema_version=1, occurred_at=report_at,
        availability="observed", value=value, source_event_id="wk-1",
        timezone="Asia/Shanghai",
    )
    result = normalize_observations(
        (obs,), context=IngestContext("u", report_at),
        signals=MINIMAL_SIGNALS, source="ios",
    )
    legacy = result.normalized[0].legacy_identity_digest
    assert legacy, "这个信号本来就该有一个不同于新摘要的旧摘要"

    # 升级前那次投递：只留下了旧摘要。
    s.remember_identity(DurableDedupeIdentity(
        subject_id="u", signal="health_workout", source="ios",
        source_event_identity_digest=legacy, first_applied_at=report_at,
    ))

    out = kit.ingest({
        "schema_version": 1, "report_id": "r-again", "producer": "ios",
        "observations": [{
            "signal": "health_workout", "signal_schema_version": 1,
            "occurred_at": report_at.isoformat(), "availability": "observed",
            "timezone": "Asia/Shanghai", "source_event_id": "wk-1",
            "value": value,
        }],
    }, context=IngestContext("u", report_at))
    # D02/I10: this old synthetic fixture has no persisted Fact evidence.
    # It cannot prove a safe migration. Do not guess its timestamp from the
    # incoming payload; report the irrecoverable gap and preserve the aggregate.
    assert out.rejected and "legacy_identity_incomplete" in str(out.rejected)
    assert not out.duplicates
    assert not out.applied
