"""「把我的全部数据给我」得真的是全部（外部审查 F9，2026-09-14）。

导出原来只取每一类的**第一页**，上限 500，而且**不告诉你被截断了**：
第 501 条起直接消失，返回里没有任何"还有更多"的痕迹。日聚合干脆不在
导出里 —— 明细过了保留期会被清掉、而日统计是永久的，于是用户拿到的
"全部数据"恰好缺了留存最久的那一份。

这不是性能问题，是**给用户的数据少给了**，而且没人会发现。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS

T0 = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)


def _kit(storage):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)


def _weigh_many(kit, n):
    for i in range(n):
        at = T0 + timedelta(minutes=i)
        out = kit.ingest({
            "schema_version": 1, "report_id": f"r-{i}", "producer": "ios",
            "observations": [{
                "signal": "health_weight", "signal_schema_version": 1,
                "occurred_at": at.isoformat(), "availability": "observed",
                "timezone": "Asia/Shanghai", "source_event_id": f"hk-{i}",
                "value": {"weight_kg": 70.0 + i * 0.01},
            }],
        }, context=IngestContext("u", at))
        assert not out.rejected, list(out.rejected)


def test_export_does_not_silently_drop_everything_past_the_first_page():
    """写 501 条，导出必须给到 501 条 —— 或者至少明说还有下一页。

    静默截断是这里最坏的形态：用户以为拿到了全部，实际少了一截，
    而且返回里没有任何线索。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh_many(kit, 501)

    dump = kit.export_subject(subject_id="u")
    rows = dump["observations"].get("health_weight", [])
    if len(rows) != 501:
        assert dump.get("truncated") or dump.get("next_cursor"), (
            f"只导出了 {len(rows)} 条（实际 501），而且没有任何"
            "「被截断了 / 还有下一页」的说明"
        )


def test_export_includes_the_daily_aggregates():
    """日聚合必须在导出里。

    明细有保留期、日统计是永久的 —— 明细清掉之后，日统计就是用户那段
    历史**仅剩**的东西。导出里不给，等于把留存最久的那份数据漏掉了。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh_many(kit, 3)

    dump = kit.export_subject(subject_id="u")
    assert "daily_aggregates" in dump, "导出里没有日聚合"
    assert dump["daily_aggregates"], "日聚合是空的 —— 实际有数据"
