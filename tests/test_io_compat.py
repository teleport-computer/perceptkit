"""已上线宿主的兼容性：**今天的上报喂进新版本，结果必须和 0.7.0 一模一样。**

0.8.0 只加定义，不改已有的。这句话光靠读 diff 是验不出来的 —— 加一个字段
就可能让某个已有宿主的整条观测被拒（新字段必填）、让事件 id 换掉（身份算法
动了）、让规则状态键变了（上线后第一条被当基线吞掉）。这些都**不报错**，
只是唤醒悄悄停了。

所以这里拿一个已上线宿主**今天真实会发出的上报形状**跑一整段序列，把每一步的
标准化结果、事件、规则状态、当前值、明细和聚合全部序列化，和 0.7.0 跑同一段
序列时存下的快照逐字节比对。

    fixtures/io_compat/reports.json   上报序列。由宿主适配层**原样代码**生成，
                                      不是手写的（见文件头 generated_from）。
    fixtures/io_compat/golden_v0.7.0.json
                                      同一段序列在 v0.7.0 源码上跑出来的结果。

重新生成快照（只有在**有意**改变已有行为、并写进 CHANGELOG 时才允许）::

    git archive v0.7.0 src | tar -x -C /tmp/pk070
    PYTHONPATH=/tmp/pk070/src python3 tests/test_io_compat.py --capture \\
        > tests/fixtures/io_compat/golden_v0.7.0.json

**这个快照不许为了让测试变绿而重生成。** 它变红说明已有宿主会看到不同的结果。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "io_compat"
ROOT = HERE.parent

#: v0.7.0 的 ``src/perceptkit/catalog.py`` 的 sha256。宿主直接转引这个模块里的
#: 常量（信号表、防抖秒数、照片成簇秒数、离开门槛），**一个字节都不许变**。
CATALOG_V070_SHA256 = "5fc6b1fb282f68a5654b02698d62409980b87cea7e577f0176fa56015ced1a02"


def _wake_definitions():
    """宿主线上那六条唤醒规则的**形状**（id 换成了中性名字，其余照抄）。"""
    from perceptkit.rules import EventDefinition
    from perceptkit.rules.types import Lifecycle

    def every(cooldown: float = 0.0) -> Lifecycle:
        if cooldown > 0:
            return Lifecycle(scope="forever", fire="every", rearm="cooldown",
                             cooldown_seconds=cooldown)
        return Lifecycle(scope="forever", fire="every", rearm="never")

    return (
        EventDefinition(definition_id="host.anchor_changed", version=2,
                        signal="proximity_anchor", condition_type="changed",
                        field_name="anchor_id", when={"is_connected": True},
                        event_type="arrived_at_anchor", wake_enabled=True,
                        dedupe_field="anchor_id", lifecycle=every(60.0)),
        EventDefinition(definition_id="host.presence_recovered", version=1,
                        signal="presence_recovery", condition_type="occurrence",
                        event_type="unlock_after_absence", wake_enabled=True,
                        lifecycle=every()),
        EventDefinition(definition_id="host.broadcast_opened", version=1,
                        signal="broadcast", condition_type="enters",
                        field_name="is_active", value=True,
                        event_type="broadcast_opened", wake_enabled=True,
                        lifecycle=every(60.0)),
        EventDefinition(definition_id="host.broadcast_closed", version=1,
                        signal="broadcast", condition_type="leaves",
                        field_name="is_active", value=True,
                        event_type="broadcast_closed", wake_enabled=True,
                        lifecycle=every(60.0)),
        EventDefinition(definition_id="host.scene_changed", version=1,
                        signal="screen_change", condition_type="occurrence",
                        event_type="scene_change", wake_enabled=True,
                        lifecycle=every()),
        EventDefinition(definition_id="host.photo_added", version=1,
                        signal="photo_library_added", condition_type="occurrence",
                        event_type="photo_added", wake_enabled=True,
                        lifecycle=every()),
    )


def _plain(obj: Any) -> Any:
    """稳定序列化：dataclass / datetime / set / tuple 键都落成确定的 JSON。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {("\x1f".join(map(str, k)) if isinstance(k, tuple) else str(k)): _plain(v)
                for k, v in sorted(obj.items(), key=lambda kv: repr(kv[0]))}
    if isinstance(obj, (set, frozenset)):
        return sorted((_plain(v) for v in obj), key=repr)
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def run_sequence() -> dict[str, Any]:
    """跑完整段序列，返回可比对的全部结果。只用 0.7.0 已有的 API。"""
    from perceptkit import IngestContext, PerceptionKit
    from perceptkit.conformance import InMemoryStorage
    from perceptkit.manifest import MINIMAL_SIGNALS

    steps = json.loads((FIXTURES / "reports.json").read_text())["steps"]
    storage = InMemoryStorage()
    kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS,
                        definitions=list(_wake_definitions()))
    results = []
    for step in steps:
        outcome = kit.ingest(step["envelope"], context=IngestContext(
            subject_id="u-compat",
            received_at=datetime.fromisoformat(step["received_at"])))
        results.append(_plain(outcome))
    return {
        "steps": results,
        "storage": {
            "reports": _plain(storage.reports),
            "observations": _plain(storage.observations),
            "identities": _plain(storage.identities),
            "current": _plain(storage.current),
            "aggregates": _plain(storage.aggregates),
            "rule_state": _plain(storage.rule_state),
            "outbox": _plain(storage.outbox),
        },
    }


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1, default=str)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_the_fixture_really_exercises_what_the_host_depends_on():
    """快照比对只在序列真的走到宿主依赖的那几条路时才有意义。
    序列被删短了、规则一条都不触发，比对照样全绿 —— 那是假绿。"""
    got = run_sequence()
    event_types = sorted(e["event_type"] for e in got["storage"]["outbox"].values())
    assert event_types.count("broadcast_opened") == 2
    assert event_types.count("broadcast_closed") >= 1
    assert event_types.count("arrived_at_anchor") >= 3
    assert event_types.count("photo_added") == 2
    assert event_types.count("unlock_after_absence") == 2
    assert event_types.count("scene_change") == 2
    assert any(r["rejected"] for r in got["steps"]), "要有一条今天就被拒的观测"
    assert any(r["receipt"]["status"] != "accepted" for r in got["steps"]), "要有一次重传"


def test_host_reports_produce_byte_identical_results_to_v070():
    """基线是 v0.7.0 的**行为**。

    2026-09-22 唯一一次允许的改动：``IngestOutcome`` 多了一栏空的
    ``retracted``（来源撤回过的事实被挡在入口外时放这里）。21 处全是
    ``"retracted": []``，其余逐字节不变 —— 纯新增字段，没有行为变化。
    2026-09-22 第二次：带稳定样本 id 的信号（睡眠、运动、经期…）的**投递
    身份**不再把 occurred_at 算进去 —— 同一条样本换个上报时刻重传一次，
    当天就加两遍（外部审查 F5）。连带 ``observation_id`` 变了，所以这次
    golden 的 diff 不是纯新增，是**有意的行为变更**。

    旧数据不受影响：入口同时查新旧两个摘要，见 pipeline ③ 和
    `test_an_identity_remembered_before_the_upgrade_still_blocks_a_re_upload`。

    2026-09-22 第三次：发件箱记录多了 source / source_event_id 两栏（撤回时
    靠它找到"这条提醒是被哪条数据触发的"）。**刻意没进投出去的信封** ——
    信封是宿主接的公开契约，多一个键所有接入方都得改。纯新增，没有行为变化。

    2026-09-28 D01/D03：删除 3 条原始 sleep 片段 Current；aggregate 新增
    独立写入 version（该 fixture 全为增量写，值为 observations - 1）。
    除这两项外保持逐字节不变；不是重算或改变 aggregation_version 算法口径。

    2026-09-28 D02：仅增加 77 个 normalized semantic_digest，替换 40 个
    Report payload_digest 为 v2 全语义摘要。迁移脚本递归拒绝其他任何差异；
    未重录事实/投影/事件/身份，仍逐字节检查原有产品行为。

    D02 review fix：Report finalization 持久化19份最终回执。

    2026-09-28 durable Report outcome：回执新增结构化、脱敏的
    observations_rejected；review 后每项补充闭集机器码 `code`，诊断文本不参与
    恢复决策。1份 mixed report 由错误的 rejected 修正为
    accepted，合法 sibling 保持提交；同 digest 重放返回 duplicate 和
    原失败证据。事实、事件和投影全部仍逐字节一致。

    其余 diff 一律当成回归，别顺手重新生成 golden。
    """
    golden = (FIXTURES / "golden_v0.7.0.json").read_text()
    result = run_sequence()
    # D08/D09 add audit-only metadata. This fixture declares no source units
    # and explicitly supplies Asia/Shanghai. Validate new metadata then strip
    # only those additions; the original golden remains byte-for-byte intact.
    def check_canonical_metadata(value):
        if isinstance(value, dict):
            if "timezone_source" in value:
                assert value.pop("timezone_source") == "observation"
                assert value.pop("source_units") == {}
                assert value.pop("source_values") == {}
                if "dimension_key" in value:
                    assert value.pop("timezone") == "Asia/Shanghai"
            for child in value.values():
                check_canonical_metadata(child)
        elif isinstance(value, list):
            for child in value:
                check_canonical_metadata(child)
    check_canonical_metadata(result)
    # D04-D06 add storage-only provenance/audit metadata. Validate it explicitly
    # then compare ALL pre-existing behavior to the untouched golden.
    for entry in result["storage"]["outbox"].values():
        refs = entry.pop("fact_dependencies")
        assert entry.pop("fact_dependencies_complete") is True
        assert refs and all(ref["fact_key"] and ref["observation_id"] for ref in refs)
        assert any(ref["role"] == "current" for ref in refs)
        for key in ("dispatch_started_at", "invalidated_at", "invalidation_reason"):
            assert entry.pop(key) is None
    for raw in result["storage"]["rule_state"].values():
        assert raw.pop("signal")
        assert raw.pop("previous_fact")["fact_key"]
        assert raw.pop("completeness") == "complete"
        assert raw.pop("incomplete_reason") is None
    # D11 adds explicit generation identity/completeness to aggregate storage.
    # This fixture performs incremental v2 writes only, so strip those verified
    # metadata additions before comparing pre-existing product behavior.
    legacy_aggregates = {}
    for key, raw in result["storage"]["aggregates"].items():
        assert raw.pop("generation_id") == "legacy-v2"
        assert raw.pop("completeness") == "complete"
        assert raw.pop("incomplete_reasons") == []
        suffix = "\x1flegacy-v2"
        assert key.endswith(suffix)
        legacy_aggregates[key[:-len(suffix)]] = raw
    result["storage"]["aggregates"] = legacy_aggregates
    now = _canonical_json(result) + "\n"
    if now != golden:
        import difflib
        diff = "\n".join(list(difflib.unified_diff(
            golden.splitlines(), now.splitlines(),
            "v0.7.0", "this build", lineterm="", n=2))[:80])
        raise AssertionError(
            "已上线宿主今天的上报，在这个版本上跑出了和 0.7.0 不同的结果 —— "
            "新增定义不该改变任何已有行为：\n" + diff)


def test_the_legacy_catalog_is_byte_for_byte_v070():
    """宿主转引 ``perceptkit.catalog`` 的常量，改一个数就改了线上行为。"""
    got = hashlib.sha256((ROOT / "src/perceptkit/catalog.py").read_bytes()).hexdigest()
    assert got == CATALOG_V070_SHA256


if __name__ == "__main__" and "--capture" in sys.argv:
    import perceptkit
    print(f"capturing with {perceptkit.__file__}", file=sys.stderr)
    sys.stdout.write(_canonical_json(run_sequence()) + "\n")
