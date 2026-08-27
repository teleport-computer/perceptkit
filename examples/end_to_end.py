"""一分钟跑通整条链路 —— 也是接入模板。

    python3 examples/end_to_end.py

新宿主照着这个文件做两件事就能接上：实现 ``StoragePort``、实现 ``WakePort``。
这里用包自带的内存版存储当例子（真实宿主换成自己的数据库即可），
自己写一个二十行的 runtime。

⚠️ 内存版存储**只是测试工具**。它验不出真实数据库的事务边界和隔离级别 ——
内存天然原子、天然无并发。生产实现必须跑
``perceptkit.conformance.run_storage_conformance``，并另外用真实数据库、
两条独立连接、在关键写操作之间打断点来证明原子性。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from perceptkit import IngestContext, PerceptionKit          # noqa: E402
from perceptkit.conformance import (                          # noqa: E402
    InMemoryStorage,
    run_storage_conformance,
)
from perceptkit.contracts import WAKE_ACCEPTED, WakeReceipt   # noqa: E402
from perceptkit.rules import EventDefinition                  # noqa: E402

SH = timezone(timedelta(hours=8))


def moment(hhmm: str, day: str = "2026-08-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


# ---------------------------------------------------------------------------
# 1. 宿主要写的第二个接口：把事件交给自己的 agent runtime
# ---------------------------------------------------------------------------

class MyRuntime:
    """真实实现里这里会是"写进队列"或"起一个 job"。

    **必须按 event_id 幂等**：崩溃重投是常态不是异常。
    """

    def __init__(self) -> None:
        self.handled: set[str] = set()

    def wake(self, event, attempt) -> WakeReceipt:
        already = event.event_id in self.handled
        self.handled.add(event.event_id)
        print(f"   ⏰ 戳醒 agent：{event.type}"
              f"（{event.previous} → {event.current}）"
              f"{'  ← 之前处理过，不重复' if already else ''}")
        return WakeReceipt(
            event_id=event.event_id, attempt_id=attempt.attempt_id,
            status=WAKE_ACCEPTED, received_at=event.received_at,
            runtime_ref="job_42",
        )


# ---------------------------------------------------------------------------
# 2. 用户配的规则。是数据，不是代码 —— 加一条不用改任何实现。
# ---------------------------------------------------------------------------

STEPS_3000 = EventDefinition.parse({
    "id": "daily_steps_3000", "version": 1,
    "source": {"signal": "steps", "field": "step_count"},
    "condition": {"type": "threshold_crossing", "operator": "gte", "value": 3000},
    "lifecycle": {"scope": "local_day", "fire": "once", "rearm": "next_scope"},
    "event": {"type": "activity.step_goal_reached"},
})


def report(count: int, hhmm: str, rid: str) -> dict:
    """设备上报长这样。"""
    return {
        "schema_version": 1, "report_id": rid, "producer": "ios",
        "observations": [{
            "signal": "steps", "signal_schema_version": 1,
            "occurred_at": f"2026-08-27T{hhmm}:00+08:00",
            "availability": "observed",
            "source_event_id": f"healthkit-{hhmm}",
            "value": {"step_count": count, "local_date": "2026-08-27"},
        }],
    }


def main() -> None:
    storage, runtime = InMemoryStorage(), MyRuntime()
    kit = PerceptionKit(storage=storage, wake=runtime, definitions=[STEPS_3000])

    def ctx(hhmm: str) -> IngestContext:
        # subject_id 从已认证连接来，绝不信客户端自报；received_at 用宿主的钟。
        return IngestContext(subject_id="user_1", received_at=moment(hhmm))

    print("① 早上，走了 2400 步 —— 还没到线")
    kit.ingest(report(2400, "09:00", "r1"), context=ctx("09:00"))

    print("② 中午，走到 3012 步 —— 跨过 3000 了")
    out = kit.ingest(report(3012, "10:30", "r2"), context=ctx("10:30"))
    print(f"   事件已落地：{len(out.events)} 条，还没投")
    print(f"   发件箱里：{len(storage.list_pending_events())} 条")

    print("③ 后台 worker 来投递")
    kit.dispatch_pending(worker_id="worker-1", now=moment("10:31"))

    print("④ 又走到 3500 —— 今天不再重复提醒")
    out = kit.ingest(report(3500, "11:00", "r3"), context=ctx("11:00"))
    print(f"   事件：{len(out.events)} 条")
    for definition_id, reason in out.rule_misses:
        print(f"   没触发的原因：{definition_id} —— {reason}")

    print("⑤ 客户端网络抖了一下，把同一批重发一遍")
    again = kit.ingest(report(3500, "11:00", "r3"), context=ctx("11:00"))
    print(f"   结果：{again.receipt.status}（不重复处理）")

    print("\n⑥ agent 主动来查")
    current = kit.get_current(subject_id="user_1", signals=["steps"],
                              now=moment("11:05"))
    print(f"   现在：{current['steps'].state} {current['steps'].value}")

    stale = kit.get_current(subject_id="user_1", signals=["steps"],
                            now=moment("23:00"))
    print(f"   十二小时后：{stale['steps'].state}"
          f"（不冒充当前，但给 last_known + as_of={stale['steps'].as_of}）")

    daily = kit.get_daily(subject_id="user_1", signal="steps",
                          start=moment("00:00").date(), end=moment("00:00").date())
    print(f"   今天的日聚合：{daily[0].value}")

    print("\n⑦ 换成真实数据库前，先跑一致性套件")
    problems = run_storage_conformance(InMemoryStorage)
    print(f"   {'全部通过' if not problems else problems}")
    print("   ⚠️ 内存实现验不出真正的事务边界和并发 —— 生产实现还要用真实数据库、"
          "两条独立连接、打断点另外证明")


if __name__ == "__main__":
    main()
