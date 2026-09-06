"""一份 iOS 形状的上报，经标准 adapter 走进 kit。

产品规范 §22-2 点名要的那条：「iOS fixture 可以通过标准 ReportAdapter 进入
PerceptKit」。在这之前测试里的 iOS 上报都是**手写的 dict** —— 手写的 dict
只能证明"我按管线期望的样子造了一份数据"，证明不了"producer 真的长这样"。

⚠️ 样本是**按 iOS 源码的结构手工构造的，不是真机抓的**（见 fixtures/README.md）。
它们能证明结构对得上，证明不了真机会发出这些值。
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import ios_adapter  # noqa: E402

from perceptkit.conformance import InMemoryStorage, run_report_conformance  # noqa: E402
from perceptkit.contracts import IngestContext  # noqa: E402
from perceptkit.kit import PerceptionKit  # noqa: E402

SH = timezone(timedelta(hours=8))
AT = "2026-08-28T09:00:00+08:00"
NOW = datetime.fromisoformat(AT)


def fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / f"ios_snapshot_{name}.json")
                      .read_text(encoding="utf-8"))


def convert(payload: dict, at: str = AT) -> dict:
    return ios_adapter.to_envelope(payload, occurred_at=at)


def states(env: dict) -> dict[str, str]:
    return {o["signal"]: o["availability"] for o in env["observations"]}


# ---------------------------------------------------------------------------
# 三态 —— 这一层唯一真正重要的事
# ---------------------------------------------------------------------------

def test_a_real_shaped_snapshot_becomes_observations():
    env = convert(fixture("normal"))
    assert env["producer"] == "ios"
    assert states(env)["battery"] == "observed"
    battery = next(o for o in env["observations"] if o["signal"] == "battery")
    assert battery["value"]["level_ratio"] == 0.62
    assert battery["value"]["is_low_power_mode_enabled"] is True


def test_an_empty_reading_becomes_no_data_not_zero():
    """iOS 用空字符串表示「有权限但这轮没读到」。

    写成 0 的话，下游每一层都会忠实地处理一份编造的事实：
    规则照常触发、趋势照常计算、agent 照常说「你今天一步都没走」。
    """
    env = convert(fixture("no_data"))
    assert states(env)["motion_state"] == "no_data"
    for obs in env["observations"]:
        if obs["availability"] == "no_data":
            assert "value" not in obs


def test_a_denied_permission_becomes_unavailable_not_no_data():
    """「没权限」和「没数据」要分开：前者该引导用户去开权限，后者只该闭嘴。"""
    env = convert(fixture("unauthorized"))
    assert states(env)["health_resting_hr"] == "unavailable"
    assert states(env)["motion_state"] == "unavailable"


def test_permission_written_inside_the_payload_is_still_unavailable():
    """focus 不是用 `data: null` 表示未授权，而是在 data 里写
    `authorization_status: denied` + `focused: null`。

    不认这一条的话，它会被当成 observed，然后因为必填字段是 null 被管线拒收 ——
    报出来是「数据格式不对」，实际是「用户没给权限」，排查方向整个跑偏。
    """
    env = convert(fixture("unauthorized"))
    assert states(env)["focus_state"] == "unavailable"


def test_the_keys_ios_says_it_cannot_get_never_become_observations():
    """iOS 显式声明「这些第三方拿不到」。把它们当成信号会造出一堆恒为 null 的观测。"""
    env = convert(fixture("normal"))
    assert "unsupported" not in states(env)


def test_calendar_and_reminders_do_not_come_in_as_signals():
    """它们走来源镜像那条路 —— 规范 §7.13 自己也是这么定的。"""
    env = convert(fixture("normal"))
    assert not any(s.startswith("calendar") or s == "reminders" for s in states(env))


def test_an_unknown_key_is_skipped_rather_than_failing_the_batch():
    """iOS 可以先发新字段、后端晚一个版本再认。反过来会让整批上报失败。"""
    payload = fixture("normal")
    payload["context_snapshot"].append(
        {"key": "some_future_signal", "data": {"x": 1}, "message": "还没接"})
    env = convert(payload)
    assert "some_future_signal" not in states(env)
    assert states(env)["battery"] == "observed"


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_the_same_snapshot_converts_to_the_same_report_id():
    """掺了当前时间或随机数的话，每次重传都变成一份「新」上报，幂等彻底失效 ——
    而重传是常态：网络抖一下、app 被挂起，客户端就会重来一次。"""
    p = fixture("normal")
    assert convert(p)["report_id"] == convert(p)["report_id"]


def test_a_different_snapshot_gets_a_different_report_id():
    a = convert(fixture("normal"))["report_id"]
    b = convert(fixture("no_data"))["report_id"]
    assert a != b


# ---------------------------------------------------------------------------
# 过一遍标准 conformance
# ---------------------------------------------------------------------------

def test_the_reference_adapter_passes_report_conformance():
    """样本带上「这份应该产出什么」—— 不带的话查不出零填充。"""
    problems = run_report_conformance(
        lambda p: convert(p),
        samples=[
            (fixture("normal"), "observed"),
            # 这两份里 time 项仍然是有值的，所以整份不是单一状态；
            # 用单信号的样本来断言状态映射，避免 R7 误报。
        ],
    )
    assert problems == [], "\n".join(problems)


def test_conformance_catches_this_adapter_if_it_starts_zero_filling():
    """把参考实现改坏，检查必须抓住 —— 否则这份模板就是在教错的做法。"""
    def zero_filling(payload):
        env = convert(payload)
        for obs in env["observations"]:
            if obs["availability"] != "observed":
                obs["availability"] = "observed"
                obs["value"] = {"motion_state": "still"}
        return env

    problems = run_report_conformance(
        zero_filling, samples=[(fixture("unauthorized"), "unavailable")])
    assert any("R7" in p or "零填充" in p for p in problems)


# ---------------------------------------------------------------------------
# 真的走进管线
# ---------------------------------------------------------------------------

def test_the_converted_report_goes_all_the_way_into_the_kit():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.ingest(convert(fixture("normal")),
                     context=IngestContext("u1", NOW))
    assert out.applied and not out.rejected

    view = kit.get_current(subject_id="u1", signals=["battery"], now=NOW)["battery"]
    assert view.state == "fresh"
    assert view.value["level_ratio"] == 0.62


def test_a_denied_snapshot_lands_as_unavailable_all_the_way_through():
    """端到端验证撤权限那条链：iOS 的 null → unavailable → 查询能表达出来。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    kit.ingest(convert(fixture("normal")), context=IngestContext("u1", NOW))
    # 撤权限是**更晚的一次快照** —— 同一时刻的观测顶不掉当前值，那是对的。
    later = NOW + timedelta(minutes=1)
    kit.ingest(convert(fixture("unauthorized"), at=later.isoformat()),
               context=IngestContext("u1", later))

    view = kit.get_current(subject_id="u1", signals=["health_resting_hr"],
                           now=NOW + timedelta(minutes=2))["health_resting_hr"]
    assert view.state == "unavailable"
    assert view.last_known is not None      # 上次读到的还在


def test_replaying_the_same_snapshot_is_idempotent():
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    env = convert(fixture("normal"))
    first = kit.ingest(env, context=IngestContext("u1", NOW))
    second = kit.ingest(env, context=IngestContext("u1", NOW))
    assert first.applied and not second.applied


# ---------------------------------------------------------------------------
# 拆包 —— iOS 一次报一整包，kit 侧是单指标
#
# 2026-09-06 kit 把健康信号拆成单指标，**iOS 的上报契约没跟着拆**：客户端
# 仍然按 HealthKit 的授权分组一次送 health_vitals / health_body /
# health_metabolic 一整包。适配器不拆包的后果不是报错，是静默丢失 ——
# 兄弟指标跟着主信号一起送出去，manifest 里没声明，被当未声明字段过滤掉。
# 参考适配器漏了这段（评审抓的），所以这里逐条钉住。
# ---------------------------------------------------------------------------

def test_a_packed_health_report_fans_out_to_the_single_metric_signals():
    env = convert(fixture("normal"))
    got = {o["signal"]: o.get("value") for o in env["observations"]}
    assert got["health_resting_hr"] == {"resting_heart_rate": 58}
    assert got["health_hrv"] == {"hrv_sdnn_ms": 42.5}
    assert got["health_oxygen"] == {"oxygen_saturation_pct": 97}
    assert got["health_weight"] == {"weight_kg": 68.2}
    assert got["health_bmi"] == {"bmi": 22.1}
    assert got["health_height"] == {"height_cm": 175}


def test_body_fat_is_converted_to_a_ratio_not_just_renamed():
    """iOS 送 18.4（百分比），manifest 声明的是 0~1 的比率。

    只改名的话存进去是 18.4 —— 区间检查会拒掉它，然后体脂**再也没有到过**，
    一声不吭。名字这么像、差 100 倍，正是手工维护的别名表最容易搞错的地方。
    """
    got = {o["signal"]: o.get("value") for o in convert(fixture("normal"))["observations"]}
    assert got["health_body_fat"] == {"body_fat_ratio": pytest.approx(0.184)}


def test_both_halves_of_a_blood_pressure_reading_arrive_together():
    """收缩压和舒张压在来源侧是**一次**读数（HealthKit correlation）。

    拆成两条观测就丢了「这是同一次量的」，而且后一条会把前一条的当前值
    顶掉 —— 留下一个只有收缩压的半条读数。
    """
    got = {o["signal"]: o.get("value") for o in convert(fixture("normal"))["observations"]}
    assert got["health_blood_pressure"] == {"blood_pressure_systolic_mmhg": 118,
                                            "blood_pressure_diastolic_mmhg": 76}


def test_the_main_signal_does_not_keep_a_copy_of_what_was_split_off():
    """两边都留一份 = 同一个数字被两套聚合规则各写一遍。"""
    got = {o["signal"]: o.get("value") for o in convert(fixture("normal"))["observations"]}
    assert "step_count" not in got["health_resting_hr"]
    assert set(got["health_weight"]) == {"weight_kg"}


def test_a_pack_whose_main_metric_is_missing_makes_no_empty_observation():
    """这份快照里没有血糖，只有血压。

    照旧发一条 health_glucose + 空 value，等于替设备说了句「我看了血糖，
    结果是空」—— 下游会当成一次真实测量。
    """
    got = {o["signal"] for o in convert(fixture("normal"))["observations"]}
    assert "health_glucose" not in got


def test_the_fanned_out_observations_are_accepted_by_the_kit():
    """上面几条只证明适配器**产出**了这些观测。真正的验收是它们能落进 kit ——
    字段名或单位对不上的话，管线会拒收，而拒收在这里才看得见。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.ingest(convert(fixture("normal")), context=IngestContext("u1", NOW))
    assert out.applied and not out.rejected, out.rejected

    wanted = ["health_body_fat", "health_blood_pressure", "health_oxygen", "steps"]
    views = kit.get_current(subject_id="u1", signals=wanted, now=NOW)
    for name in wanted:
        assert views[name].state == "fresh", f"{name} 没落进 kit"
    assert views["health_body_fat"].value["body_fat_ratio"] == pytest.approx(0.184)
