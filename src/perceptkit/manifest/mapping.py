"""Reference storage mapping —— 产品规范 §15 点名要的那份表。

它回答一句话：**每个信号的数据落到哪几个逻辑存储对象里、留多久。**

刻意**从 manifest 生成**，不手写。手写的表和代码之间没有任何东西拦着它们漂开，
而这份表恰恰是给别人照着建库用的 —— 漂了就是让人照着一份过期的图施工。

    from perceptkit.manifest import render_reference_mapping
    print(render_reference_mapping())
"""
from __future__ import annotations

from typing import Mapping

from .types import PERMANENT, SignalDefinition

#: storage_mode → 这个信号实际会写到哪几个逻辑对象。
#: 这是 manifest 里那个枚举的"落地含义"，写在一处免得每个宿主自己推。
MODE_OBJECTS: dict[str, tuple[str, ...]] = {
    "current_only": ("CurrentProjection",),
    "current_short_timeline": ("CurrentProjection", "StoredObservation"),
    "current_timeline_aggregate": (
        "CurrentProjection", "StoredObservation", "DailyAggregate",
    ),
    "source_mirror": ("CalendarEventMirror / ReminderItemMirror", "SourceSyncState"),
}


def _days(n: int | None) -> str:
    if n is None:
        return "同明细"
    if n == PERMANENT:
        return "永久"
    if n == 0:
        return "不存"
    return f"{n} 天"


def reference_mapping(
    signals: Mapping[str, SignalDefinition],
) -> list[dict[str, object]]:
    """每个信号一行，说明它落到哪些对象、各留多久。"""
    rows: list[dict[str, object]] = []
    for key in sorted(signals):
        sig = signals[key]
        rows.append({
            "signal": key,
            "storage_mode": sig.storage_mode,
            "objects": MODE_OBJECTS.get(sig.storage_mode, ()),
            "current_ttl_sec": sig.current_ttl_sec,
            "detail_retention": _days(sig.history_retention_days),
            "aggregate_retention": (
                _days(sig.aggregate_retention_days)
                if sig.stores_history else "不适用"
            ),
            "identity_strategy": sig.identity_strategy,
            "attribution_strategy": sig.attribution_strategy,
            "note": sig.note,
        })
    return rows


def render_reference_mapping(signals: Mapping[str, SignalDefinition]) -> str:
    """渲染成 Markdown 表格。"""
    rows = reference_mapping(signals)
    out = [
        "# Reference storage mapping",
        "",
        "> 由 `perceptkit.manifest.render_reference_mapping()` 从 manifest 生成。",
        "> **不要手改** —— 改 manifest，然后重新生成。",
        "",
        "`Current TTL` 到期之后这个值不再冒充「现在」，但仍可作为 last known 返回。",
        "明细和聚合是**两个保留期**：典型形态是明细短、聚合永久 ——"
        "明细体量大而问题的价值随时间递减，聚合正好相反。",
        "",
        "| 信号 | 落到哪些对象 | Current TTL | 明细 | 聚合 | 身份 | 日期归属 |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for r in rows:
        ttl = f"{int(r['current_ttl_sec'])}s" if r["current_ttl_sec"] else "—"
        objects = " + ".join(r["objects"]) or "—"          # type: ignore[arg-type]
        out.append(
            f"| `{r['signal']}` | {objects} | {ttl} | {r['detail_retention']} | "
            f"{r['aggregate_retention']} | {r['identity_strategy']} | "
            f"{r['attribution_strategy']} |"
        )

    noted = [r for r in rows if r["note"]]
    if noted:
        out += ["", "## 和产品规范有出入的地方", ""]
        for r in noted:
            out += [f"### `{r['signal']}`", "", str(r["note"]), ""]
    return "\n".join(out) + "\n"


__all__ = ["MODE_OBJECTS", "reference_mapping", "render_reference_mapping"]
