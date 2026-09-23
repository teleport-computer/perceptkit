"""用户删掉一条数据之后，引用它的提醒记录里的原值也要抹掉。

hx 2026-09-17 拍板，是「删除不再提供原值」那条决定的延伸：

    早上称了 72kg → 规则"一周涨超 2kg"触发 → io 发消息"这周涨了 2kg 哦"
                  → 系统里记下一条事件：「体重 72kg 触发了涨重提醒」
    下午发现秤没放平，在健康 app 里把 72kg 删了
    之后          体重数据里的 72 没了，**事件记录里也不许再有 72**，
                  只留"一条已被删除的数据触发过这条提醒"

io 在聊天里已经说出口的那句话不动 —— 那是已经发生的事，改不了也不该改。

⚠️ 这里抹的是**读出来的东西**。要把库里那一行也真抹掉，得宿主配合
（加一个写回的端口方法），那是另一件事，没做，见交接说明。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.retraction import Retraction
from perceptkit.manifest import MINIMAL_SIGNALS
from perceptkit.rules import EventDefinition

T0 = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)

HEAVY = EventDefinition.parse({
    "id": "weight_over", "version": 1,
    "source": {"signal": "health_weight", "field": "weight_kg"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 71},
    "event": {"type": "health.weight_over"},
})


def _kit(storage):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS,
                         definitions=[HEAVY])


def _weigh(kit, kg, *, at, eid):
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
    return out


def test_the_event_stops_showing_a_value_the_user_deleted():
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, 70.0, at=T0, eid="hk-A")              # 先给个前值，才会有"跨过去"
    fired = _weigh(kit, 72.0, at=T0 + timedelta(hours=1), eid="hk-B")
    assert fired.events, "规则没触发，这条测的东西没发生"

    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-B", "ios", T0 + timedelta(hours=2))],
        now=T0 + timedelta(hours=2))

    # 留着原值的是**存在库里那条事件记录**（也就是投递给 runtime 的快照），
    # 不是 list_events 的返回 —— 那里本来就不带数值。
    entries = list(s.outbox.values())
    assert entries, "事件记录整条没了 —— 要留下「触发过」这件事"
    dump = repr([e.fact_snapshot for e in entries])
    assert "72.0" not in dump, f"事件记录里还留着用户已经删掉的数值：{dump}"
    assert any(e.fact_snapshot.get("retracted") for e in entries), \
        "没标出来「触发它的那条数据已被删除」—— 那就没法解释这条提醒为什么在"


def test_an_untouched_event_keeps_its_value():
    """反向守卫：没被删的事件照常带着数值，别一刀切抹掉。"""
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, 70.0, at=T0, eid="hk-A")
    _weigh(kit, 72.0, at=T0 + timedelta(hours=1), eid="hk-B")

    snaps = [e.fact_snapshot for e in s.outbox.values()]
    assert "72.0" in repr(snaps), "把没被删的事件也抹了"
    assert not any(x.get("retracted") for x in snaps)
