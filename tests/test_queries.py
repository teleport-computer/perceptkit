"""读取侧那八个查询函数 —— 之前一条测试都没有。

批 5 在计划里标的是「✅ 已完成（八个函数）」，但那只是**写完了**：
八个里只有 `get_current` 被端到端测试顺带覆盖过，另外七个零测试。

这一层不是薄封装，它自己就有四样容易错的逻辑：
TTL 判定 / 分页硬上限 / 隐私投影 / 缺数据要说出来。
错了都不会崩，只会安静地给出一个看起来合理的错答案。

对应产品规范 §22 的完成定义：
  §22-6  可以查询标准化 observation timeline
  §22-7  可以查询 daily history 和 trend
  §22-16 Location 可以表达成都到上海这样的 city/locality timeline
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import DailyAggregate, StoredObservation
from perceptkit.manifest.minimal import MINIMAL_SIGNALS
from perceptkit.queries import api

SH = timezone(timedelta(hours=8))


def t(day: str, hhmm: str = "09:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def obs(signal: str, day: str, value: dict, *, hhmm: str = "09:00",
        availability: str = "observed", oid: str | None = None) -> StoredObservation:
    when = t(day, hhmm)
    return StoredObservation(
        observation_id=oid or f"{signal}:{day}:{hhmm}",
        subject_id="u1", signal=signal, signal_schema_version=1, source="ios",
        occurred_at=when, received_at=when, availability=availability,
        effective_local_date=date.fromisoformat(day), typed_value=value,
    )


def daily(signal: str, day: str, doc: dict, *, version: int = 1) -> DailyAggregate:
    return DailyAggregate(
        subject_id="u1", signal=signal, local_date=date.fromisoformat(day),
        aggregation_kind="daily", aggregation_version=version, typed_aggregate=doc,
    )


# ---------------------------------------------------------------------------
# §22-6  observation timeline
# ---------------------------------------------------------------------------

def test_timeline_returns_observations_oldest_first():
    s = InMemoryStorage()
    for d in ("2026-08-03", "2026-08-01", "2026-08-02"):
        s.append_observation(obs("steps", d, {"step_count": 100}))

    rows, nxt = api.list_timeline(s, subject_id="u1", signal="steps",
                                  manifest=MINIMAL_SIGNALS)
    assert [r["local_date"] for r in rows] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert nxt is None


def test_timeline_pages_and_the_cursor_continues_where_it_stopped():
    """agent 问「我这个月都去过哪」，不分页就是几千条直接塞进上下文。"""
    s = InMemoryStorage()
    for i in range(7):
        s.append_observation(obs("steps", "2026-08-01", {"step_count": i},
                              hhmm=f"{9 + i:02d}:00"))

    first, nxt = api.list_timeline(s, subject_id="u1", signal="steps",
                                   manifest=MINIMAL_SIGNALS, limit=3)
    assert len(first) == 3 and nxt is not None

    second, nxt2 = api.list_timeline(s, subject_id="u1", signal="steps",
                                     manifest=MINIMAL_SIGNALS, limit=3, cursor=nxt)
    assert len(second) == 3
    # 两页不能有重叠 —— 重叠会让 agent 把同一件事数两遍
    assert {r["occurred_at"] for r in first}.isdisjoint(r["occurred_at"] for r in second)

    third, nxt3 = api.list_timeline(s, subject_id="u1", signal="steps",
                                    manifest=MINIMAL_SIGNALS, limit=3, cursor=nxt2)
    assert len(third) == 1 and nxt3 is None


@pytest.mark.parametrize("asked, expected", [
    (0, api.DEFAULT_LIMIT),        # 0 当成"没指定"
    (10_000, api.MAX_LIMIT),       # 要多少都不给超过硬上限
    (-5, 1),                       # 负数不能变成"倒着取"或整表扫描
])
def test_the_page_size_is_clamped_no_matter_what_the_caller_asks(asked, expected):
    assert api._clamp(asked) == expected


def test_timeline_respects_the_time_window():
    s = InMemoryStorage()
    for d in ("2026-07-31", "2026-08-01", "2026-08-02"):
        s.append_observation(obs("steps", d, {"step_count": 1}))

    rows, _ = api.list_timeline(
        s, subject_id="u1", signal="steps", manifest=MINIMAL_SIGNALS,
        start=t("2026-08-01"), end=t("2026-08-01", "23:59"),
    )
    assert [r["local_date"] for r in rows] == ["2026-08-01"]


def test_timeline_never_hands_out_a_never_visible_field():
    """`coordinate` 在 manifest 里声明的是「永不给 agent」。

    声明成规则而不是口头约定，就是为了让这一条能被测试钉住 ——
    宿主自觉是靠不住的。
    """
    s = InMemoryStorage()
    s.append_observation(obs("location_city", "2026-08-01", {
        "locality": "上海", "country_code": "CN",
        "coordinate": {"lat": 31.23, "lon": 121.47},
    }))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="location_city",
                                manifest=MINIMAL_SIGNALS)
    assert rows[0]["value"]["locality"] == "上海"
    assert "coordinate" not in rows[0]["value"]


def test_timeline_keeps_the_three_states_apart():
    """`no_data`（没测到）和 `unavailable`（没权限）不能在读取侧被抹平成一样。"""
    s = InMemoryStorage()
    s.append_observation(obs("steps", "2026-08-01", {"step_count": 0}, oid="a"))
    s.append_observation(obs("steps", "2026-08-02", None, availability="no_data", oid="b"))
    s.append_observation(obs("steps", "2026-08-03", None, availability="unavailable", oid="c"))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="steps",
                                manifest=MINIMAL_SIGNALS)
    assert [r["availability"] for r in rows] == ["observed", "no_data", "unavailable"]
    # 零步数是【测到了，就是 0】—— 不是缺数据
    assert rows[0]["value"]["step_count"] == 0


# ---------------------------------------------------------------------------
# §22-7  daily history
# ---------------------------------------------------------------------------

def test_daily_does_not_fill_the_missing_days_with_zero():
    """十四天里两天没戴表，补两个 0 进去，平均睡眠立刻被拉垮，而且没人会报错。"""
    s = InMemoryStorage()
    s.put_aggregate(daily("steps", "2026-08-01", {"step_count": {"total": 8000}}))
    s.put_aggregate(daily("steps", "2026-08-04", {"step_count": {"total": 9000}}))

    rows = api.get_daily_aggregates(s, subject_id="u1", signal="steps",
                                    start_date=date(2026, 8, 1), end_date=date(2026, 8, 5))
    assert [r.date for r in rows] == ["2026-08-01", "2026-08-04"]


def test_daily_comes_back_in_date_order_regardless_of_insert_order():
    s = InMemoryStorage()
    for d in ("2026-08-05", "2026-08-01", "2026-08-03"):
        s.put_aggregate(daily("steps", d, {"step_count": {"total": 1}}))

    rows = api.get_daily_aggregates(s, subject_id="u1", signal="steps",
                                    start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    assert [r.date for r in rows] == ["2026-08-01", "2026-08-03", "2026-08-05"]


# ---------------------------------------------------------------------------
# §22-7  trend
# ---------------------------------------------------------------------------

def test_trend_always_says_how_many_days_were_missing():
    """缺了几天必须说出来 —— 否则调用方无从判断这个趋势可不可信。

    "你这个月步数在下降"背后是 30 天里只有 3 天有数据的话，这句话不该说。
    """
    s = InMemoryStorage()
    for d in ("2026-08-01", "2026-08-02"):
        s.put_aggregate(daily("steps", d, {"step_count": {"total": 8000}}))

    out = api.get_trend(s, subject_id="u1", signal="steps", field="step_count",
                        manifest=MINIMAL_SIGNALS,
                        start_date=date(2026, 8, 1), end_date=date(2026, 8, 10))
    assert out["days_with_data"] == 2
    assert out["days_missing"] == 8


def test_trend_picks_the_algorithm_the_manifest_declares():
    """三种模型走三条完全不同的路，选错了结论就是错的。

    体重是单调漂移、步数是围绕平时水平波动、经期是看间隔 ——
    拿看间隔的算法算体重，会得出一个语法正确、意思荒谬的结论。
    """
    s = InMemoryStorage()
    s.put_aggregate(daily("steps", "2026-08-01", {"step_count": {"total": 8000}}))
    s.put_aggregate(daily("health_body", "2026-08-01", {"weight_kg": {"value": 70.0}}))

    steps = api.get_trend(s, subject_id="u1", signal="steps", field="step_count",
                          manifest=MINIMAL_SIGNALS,
                          start_date=date(2026, 8, 1), end_date=date(2026, 8, 1))
    body = api.get_trend(s, subject_id="u1", signal="health_body", field="weight_kg",
                         manifest=MINIMAL_SIGNALS,
                         start_date=date(2026, 8, 1), end_date=date(2026, 8, 1))
    assert steps["model"] == "fluctuating"
    assert body["model"] == "drifting"


def test_trend_refuses_a_field_the_manifest_hides_from_the_agent():
    s = InMemoryStorage()
    out = api.get_trend(s, subject_id="u1", signal="location_city", field="coordinate",
                        manifest=MINIMAL_SIGNALS,
                        start_date=date(2026, 8, 1), end_date=date(2026, 8, 5))
    assert out["model"] == "none" and "不对 agent 开放" in out["reason"]


@pytest.mark.parametrize("signal, field", [
    ("no_such_signal", "step_count"),      # manifest 里没有这个信号
    ("steps", "no_such_field"),            # 信号有，字段没有
])
def test_trend_explains_itself_instead_of_crashing(signal, field):
    """查一个不存在的东西要给出原因，不能抛异常 —— 这是 agent 会直接调的接口。"""
    s = InMemoryStorage()
    out = api.get_trend(s, subject_id="u1", signal=signal, field=field,
                        manifest=MINIMAL_SIGNALS,
                        start_date=date(2026, 8, 1), end_date=date(2026, 8, 5))
    assert out["model"] == "none" and out["reason"]


def test_trend_on_an_empty_range_says_so_rather_than_inventing_a_direction():
    s = InMemoryStorage()
    out = api.get_trend(s, subject_id="u1", signal="steps", field="step_count",
                        manifest=MINIMAL_SIGNALS,
                        start_date=date(2026, 8, 1), end_date=date(2026, 8, 5))
    assert out["days_with_data"] == 0
    assert "一条数据都没有" in out["reason"]


# ---------------------------------------------------------------------------
# §22-16  城市 timeline
# ---------------------------------------------------------------------------

def test_a_move_from_one_city_to_another_reads_as_a_timeline():
    """规范 §22-16 点名要的例子：成都 → 上海。"""
    s = InMemoryStorage()
    s.append_observation(obs("location_city", "2026-08-01",
                          {"locality": "成都", "country_code": "CN"}, oid="a"))
    s.append_observation(obs("location_city", "2026-08-02",
                          {"locality": "上海", "country_code": "CN"}, oid="b"))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="location_city",
                                manifest=MINIMAL_SIGNALS)
    assert [r["value"]["locality"] for r in rows] == ["成都", "上海"]


def test_the_city_signal_only_ever_holds_city_level_information():
    """规范 §22-17 的一半：城市和精细地点不能混成同一个语义。

    两个时期都叫 home，混在一个字段里就看不出搬过家 —— 这正是
    manifest 里 location_city 那条 note 写死的理由。
    """
    sig = MINIMAL_SIGNALS["location_city"]
    visible = set(api.visible_fields(sig))
    # 城市级 + 关于这个城市判断本身的元信息（准不准、哪来的）
    assert visible == {"locality", "country_code", "region",
                       "accuracy_m", "placemark_source"}
    # 精细位置一个都不许在这里：它们属于 proximity_anchor
    for finer in ("place_label", "anchor_id", "wifi_label", "coordinate"):
        assert finer not in visible, finer


# ---------------------------------------------------------------------------
# §22-17  锚点身份稳定，且不和城市混成同一语义
# ---------------------------------------------------------------------------

def test_the_anchor_and_the_city_are_two_signals_not_one_field_at_two_zoom_levels():
    """规范 §22-17。城市回答「在哪座城」，锚点回答「在哪个地方」。

    混进同一个字段的后果很具体：搬家之后，新旧两个「home」就分不出来了。
    """
    s = InMemoryStorage()
    s.append_observation(obs("location_city", "2026-08-01",
                             {"locality": "上海", "country_code": "CN"}, oid="c"))
    s.append_observation(obs("proximity_anchor", "2026-08-01", {
        "anchor_id": "a1b2c3", "anchor_type": "wifi",
        "label": "home", "is_connected": True,
    }, oid="p"))

    city, _ = api.list_timeline(s, subject_id="u1", signal="location_city",
                                manifest=MINIMAL_SIGNALS)
    anchor, _ = api.list_timeline(s, subject_id="u1", signal="proximity_anchor",
                                  manifest=MINIMAL_SIGNALS)

    # 各查各的，互相看不到对方的字段
    assert "anchor_id" not in city[0]["value"] and "label" not in city[0]["value"]
    assert "locality" not in anchor[0]["value"]
    assert anchor[0]["value"]["label"] == "home"


def test_moving_house_leaves_two_distinguishable_anchors_both_called_home():
    """规范 §5.2-6 点名的场景：搬家后新旧都叫 home，靠 anchor_id 区分。"""
    s = InMemoryStorage()
    s.append_observation(obs("proximity_anchor", "2026-08-01", {
        "anchor_id": "old-place", "anchor_type": "wifi",
        "label": "home", "is_connected": True}, oid="p1"))
    s.append_observation(obs("proximity_anchor", "2026-09-01", {
        "anchor_id": "new-place", "anchor_type": "wifi",
        "label": "home", "is_connected": True}, oid="p2"))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="proximity_anchor",
                                manifest=MINIMAL_SIGNALS)
    assert [r["value"]["label"] for r in rows] == ["home", "home"]
    assert len({r["value"]["anchor_id"] for r in rows}) == 2


def test_renaming_an_anchor_does_not_change_its_identity():
    """规范 §5.2-4：anchor_id 是身份，label 只是用户起的名字，改名不影响身份。"""
    sig = MINIMAL_SIGNALS["proximity_anchor"]
    fields = sig.field_map()
    # 身份字段参与比较，标签不参与 —— 改个名不该被当成"换了个地方"
    assert fields["anchor_id"].comparison_strategy == "exact"
    assert fields["label"].comparison_strategy == "none"


def test_the_raw_wifi_identifier_never_reaches_the_agent():
    """BSSID 原文和精确坐标同一个待遇：声明成规则，让它可被测试检查。"""
    s = InMemoryStorage()
    s.append_observation(obs("proximity_anchor", "2026-08-01", {
        "anchor_id": "a1b2c3", "anchor_type": "wifi", "label": "home",
        "is_connected": True, "raw_identifier": "aa:bb:cc:dd:ee:ff",
    }, oid="p"))

    rows, _ = api.list_timeline(s, subject_id="u1", signal="proximity_anchor",
                                manifest=MINIMAL_SIGNALS)
    assert "raw_identifier" not in rows[0]["value"]
    assert rows[0]["value"]["anchor_id"] == "a1b2c3"
