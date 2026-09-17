"""0.8.0 新增的定义：播放类型 / 时长、屏幕采集的暂停态、用户命名的区域。

每一条新增都要同时证明两件事：

    带上新字段    收得下、存得住、查得到
    不带新字段    和以前一模一样（已上线的 producer 就是这个形状）

后一半在 ``test_io_compat.py`` 里对 0.7.0 的快照逐字节比过一遍；这里补
「不带新字段的那条观测照样被收下」这种单条的、读得懂的版本，以及新字段自己的边界。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import DECLINED_SIGNALS, MINIMAL_SIGNALS, validate_manifest
from perceptkit.queries import api as queries
from perceptkit.rules import EventDefinition, Lifecycle

SH = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=SH)
EVERY = Lifecycle(scope="forever", fire="every", rearm="never")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def report(rid: str, minutes: float, *observations: dict) -> dict:
    return {"schema_version": 1, "report_id": rid, "producer": "ios",
            "observations": [
                {"signal_schema_version": 1, "occurred_at": at(minutes).isoformat(),
                 "timezone": "Asia/Shanghai", **o}
                for o in observations]}


def obs(signal: str, value: dict | None, availability: str = "observed") -> dict:
    out = {"signal": signal, "availability": availability}
    if value is not None:
        out["value"] = value
    return out


def ingest(kit: PerceptionKit, rid: str, minutes: float, *observations: dict):
    return kit.ingest(report(rid, minutes, *observations),
                      context=IngestContext("u1", at(minutes) + timedelta(seconds=3)))


def fresh_kit(definitions=()) -> tuple[PerceptionKit, InMemoryStorage]:
    storage = InMemoryStorage()
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS,
                         definitions=list(definitions)), storage


def current_value(storage: InMemoryStorage, signal: str) -> dict | None:
    view = queries.get_current(storage, subject_id="u1", signals=[signal],
                               manifest=MINIMAL_SIGNALS, now=at(1000))
    return view[signal].last_known


PLAYING = {"playback_state": "playing", "title": "t", "artist": "a",
           "track_key": "k1", "edge_quality": "measured"}


# ---------------------------------------------------------------------------
# 版本与结构
# ---------------------------------------------------------------------------

def test_the_manifest_with_the_new_definitions_is_still_clean():
    assert validate_manifest(MINIMAL_SIGNALS) == []


def test_new_fields_on_existing_signals_are_optional_and_versions_do_not_move():
    """🔴 已有信号上加必填字段 = 老 producer 的整条观测被拒 = 唤醒静默停掉。"""
    for signal, field in (("music_playback", "media_type"),
                          ("music_playback", "duration_seconds"),
                          ("broadcast", "broadcast_state")):
        fd = MINIMAL_SIGNALS[signal].field_map()[field]
        assert fd.nullable, f"{signal}.{field} 必须可空"
    # 加一个可空字段不是 payload 的破坏性变化；升版本号只会让每个老 producer
    # 每次上报都刷一条「版本不一致」警告。
    assert MINIMAL_SIGNALS["music_playback"].schema_version == 1
    assert MINIMAL_SIGNALS["broadcast"].schema_version == 1
    assert MINIMAL_SIGNALS["place_zone"].schema_version == 1


def test_media_type_vocabulary_is_exactly_what_the_ios_producer_sends():
    """值对不上是最贵的一类错：只有那几个值被拒，躲在 fixture 用了哪个值后面。
    这 13 个是 iOS 端 MPMediaType 映射函数的全部返回值，一个不多一个不少。"""
    assert MINIMAL_SIGNALS["music_playback"].field_map()["media_type"].enum == (
        "music", "podcast", "audio_book", "audio_itunes_u", "audio",
        "movie", "tv_show", "music_video", "video_podcast", "video_itunes_u",
        "home_video", "video", "unknown",
    )


def test_app_presence_is_written_down_as_declined():
    """「不做」也要写下来，否则下一个人会当成漏项捡回来。"""
    assert "app_presence" not in MINIMAL_SIGNALS
    assert DECLINED_SIGNALS["app_presence"].strip()


# ---------------------------------------------------------------------------
# music_playback.media_type / duration_seconds
# ---------------------------------------------------------------------------

def test_playback_without_the_new_fields_is_still_accepted():
    kit, storage = fresh_kit()
    out = ingest(kit, "m0", 0, obs("music_playback", PLAYING))
    assert out.rejected == [] and len(out.applied) == 1
    assert "media_type" not in current_value(storage, "music_playback")


def test_playback_with_the_new_fields_is_stored_and_queryable():
    kit, storage = fresh_kit()
    out = ingest(kit, "m1", 0, obs("music_playback",
                                   {**PLAYING, "media_type": "podcast",
                                    "duration_seconds": 3600.5}))
    assert out.rejected == []
    assert not any("未声明" in w for w in out.warnings), out.warnings
    now = current_value(storage, "music_playback")
    assert now["media_type"] == "podcast"
    assert now["duration_seconds"] == 3600.5
    rows, _ = queries.list_timeline(storage, subject_id="u1", signal="music_playback",
                                    manifest=MINIMAL_SIGNALS)
    assert rows[-1]["value"]["media_type"] == "podcast"


@pytest.mark.parametrize("media_type", MINIMAL_SIGNALS["music_playback"]
                         .field_map()["media_type"].enum)
def test_every_media_type_is_accepted(media_type):
    kit, _ = fresh_kit()
    out = ingest(kit, f"mt-{media_type}", 0,
                 obs("music_playback", {**PLAYING, "media_type": media_type}))
    assert out.rejected == []


def test_an_unlisted_media_type_rejects_that_observation_only():
    """决定：枚举严格，**不**悄悄收下。代价写在字段 note 里 —— 宿主把认不出的
    值落到 ``unknown``。这里钉住代价的边界：只拒那一条，同批其他观测照收。"""
    kit, storage = fresh_kit()
    out = ingest(kit, "mt-bad", 0,
                 obs("music_playback", {**PLAYING, "media_type": "spatial_audio"}),
                 obs("battery", {"level_ratio": 0.5, "is_charging": False}))
    assert [i for i, _ in out.rejected] == [0]
    assert "media_type" in out.rejected[0][1][0]
    assert [a.stored.signal for a in out.applied] == ["battery"]


def test_duration_is_non_negative_and_zero_is_a_plain_number():
    """0 收得下 —— 它是合法数值，所以 kit 分不出「长 0 秒」和「不知道」。
    这正是字段 note 要求适配层遇到 0 就不发的原因。"""
    kit, storage = fresh_kit()
    bad = ingest(kit, "d-neg", 0, obs("music_playback",
                                      {**PLAYING, "duration_seconds": -1}))
    assert bad.rejected
    zero = ingest(kit, "d-zero", 1, obs("music_playback",
                                        {**PLAYING, "duration_seconds": 0}))
    assert zero.rejected == []
    note = MINIMAL_SIGNALS["music_playback"].field_map()["duration_seconds"].note
    assert "position_seconds" in note and "0" in note


# ---------------------------------------------------------------------------
# broadcast.broadcast_state
# ---------------------------------------------------------------------------

OPENED = EventDefinition(definition_id="b.opened", version=1, signal="broadcast",
                         condition_type="enters", field_name="is_active", value=True,
                         event_type="broadcast_opened", wake_enabled=True,
                         lifecycle=EVERY)
CLOSED = EventDefinition(definition_id="b.closed", version=1, signal="broadcast",
                         condition_type="leaves", field_name="is_active", value=True,
                         event_type="broadcast_closed", wake_enabled=True,
                         lifecycle=EVERY)

#: 开 → 暂停 → 恢复 → 关 → 再开。暂停时 is_active 仍为 True（采集会话还在）。
SESSION = [(True, "broadcasting"), (True, "paused"), (True, "broadcasting"),
           (False, "idle"), (True, "broadcasting")]


def _run_session(with_state: bool):
    kit, storage = fresh_kit([OPENED, CLOSED])
    fired = []
    for i, (active, state) in enumerate(SESSION):
        value = {"is_active": active}
        if with_state:
            value["broadcast_state"] = state
        out = ingest(kit, f"b-{with_state}-{i}", i * 5, obs("broadcast", value))
        assert out.rejected == []
        fired.append([e.type for e in out.events])
    return fired, storage


def test_broadcast_rules_fire_identically_with_or_without_broadcast_state():
    """已有的开/关规则盯的是 is_active。新加的那一格不能让它们多叫或少叫一次。"""
    without, _ = _run_session(with_state=False)
    with_state, _ = _run_session(with_state=True)
    assert without == with_state == [
        ["broadcast_opened"], [], [], ["broadcast_closed"], ["broadcast_opened"]]


def test_pause_and_resume_are_kept_in_the_timeline_when_the_producer_sends_them():
    """暂停 / 恢复不改 is_active，但它们是真实的状态变化 —— 发了就留得住。"""
    _, without = _run_session(with_state=False)
    _, with_state = _run_session(with_state=True)
    count = lambda s: len([o for o in s.observations.values()  # noqa: E731
                           if o.signal == "broadcast"])
    # 不发 broadcast_state 的老形状：只有 is_active 真的变了才写明细（和 0.7.0 一样）。
    assert count(without) == 3
    assert count(with_state) == 5
    assert current_value(with_state, "broadcast")["broadcast_state"] == "broadcasting"


def test_host_vocabulary_must_be_translated_before_it_reaches_the_kit():
    """设备事件里常见的 on / off 不是 kit 的词。原样发进来会让整条被拒 ——
    连 is_active 一起丢，所以翻译必须在宿主适配层做。"""
    kit, _ = fresh_kit()
    out = ingest(kit, "b-on", 0, obs("broadcast", {"is_active": True,
                                                   "broadcast_state": "on"}))
    assert out.rejected
    note = MINIMAL_SIGNALS["broadcast"].field_map()["broadcast_state"].note
    assert "on / off / paused" in note


# ---------------------------------------------------------------------------
# place_zone
# ---------------------------------------------------------------------------

def test_place_zone_inside_a_zone_is_stored_with_history():
    kit, storage = fresh_kit()
    ingest(kit, "z1", 0, obs("place_zone", {"is_inside_known_zone": True,
                                            "zone_label": "home"}))
    ingest(kit, "z2", 30, obs("place_zone", {"is_inside_known_zone": True,
                                             "zone_label": "work"}))
    assert current_value(storage, "place_zone") == {
        "is_inside_known_zone": True, "zone_label": "work"}
    rows, _ = queries.list_timeline(storage, subject_id="u1", signal="place_zone",
                                    manifest=MINIMAL_SIGNALS)
    assert [r["value"]["zone_label"] for r in rows] == ["home", "work"]
    sig = MINIMAL_SIGNALS["place_zone"]
    assert sig.history_retention_days == 7 and sig.capability == "location"


def test_outside_every_zone_is_a_fact_not_a_label():
    kit, storage = fresh_kit()
    out = ingest(kit, "z-out", 0, obs("place_zone", {"is_inside_known_zone": False}))
    assert out.rejected == []
    assert current_value(storage, "place_zone") == {"is_inside_known_zone": False}


def test_the_inside_flag_is_required_so_outside_has_exactly_one_spelling():
    kit, _ = fresh_kit()
    out = ingest(kit, "z-bad", 0, obs("place_zone", {"zone_label": "home"}))
    assert out.rejected and "is_inside_known_zone" in out.rejected[0][1][0]


def test_no_location_fix_does_not_overwrite_the_last_known_place():
    """没定位用 availability=no_data。它不覆盖最后一次可靠值 ——
    如果拿一个 "unknown" 标签当 observed 发，家就被覆盖成了一个叫 unknown 的地方。"""
    kit, storage = fresh_kit()
    ingest(kit, "z-home", 0, obs("place_zone", {"is_inside_known_zone": True,
                                                "zone_label": "home"}))
    out = ingest(kit, "z-nofix", 5, obs("place_zone", None, availability="no_data"))
    assert out.rejected == []
    assert current_value(storage, "place_zone") == {
        "is_inside_known_zone": True, "zone_label": "home"}


def test_a_host_can_wake_on_arriving_at_a_named_zone():
    arrived = EventDefinition(
        definition_id="z.arrived", version=1, signal="place_zone",
        condition_type="changed", field_name="zone_label",
        when={"is_inside_known_zone": True}, event_type="arrived_at_zone",
        wake_enabled=True, lifecycle=EVERY)
    kit, _ = fresh_kit([arrived])
    fired = []
    for i, value in enumerate([
        {"is_inside_known_zone": True, "zone_label": "home"},
        {"is_inside_known_zone": False},
        {"is_inside_known_zone": True, "zone_label": "work"},
        {"is_inside_known_zone": True, "zone_label": "home"},
    ]):
        fired += [e.type for e in ingest(kit, f"za-{i}", i * 10,
                                               obs("place_zone", value)).events]
    # 第一条只建立起点；「离开」不满足前置条件，不推进前值、也不叫。
    assert fired == ["arrived_at_zone", "arrived_at_zone"]


def test_place_zone_is_in_the_generated_storage_table_and_retention_plan():
    from perceptkit.manifest import reference_mapping
    from perceptkit.retention import plan_retention
    assert "place_zone" in {r["signal"] for r in reference_mapping(MINIMAL_SIGNALS)}
    plan = plan_retention(MINIMAL_SIGNALS, now=at(0))
    assert "place_zone" in repr(plan)
