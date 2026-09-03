"""来源镜像同步的编排规则。

外部审查（2026-09-02，P0-2）："只有 Storage 原语，没有标准同步工作流"。
成立：写入侧有四个原语但没有和 ingest() 对等的入口，于是每个宿主自己拼
「收数据 → upsert → 写状态 → 全量按范围删 → 处理失败」这一串。

这个文件测的是**规则**，不是"函数能跑"。这条路上的每个坑错了都不报错，
而且大多不可逆 —— 用户发现自己去年的日程凭空消失了，而系统一切正常。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.records import CalendarEventMirror, ReminderItemMirror
from perceptkit.processing.source_sync import (
    FULL, INCREMENTAL, SyncBatch, SyncContractError, sync_source_mirror,
)

T0 = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
CTX = IngestContext("u1", T0)


def _event(eid: str, at: datetime = T0) -> CalendarEventMirror:
    return CalendarEventMirror(
        subject_id="u1", source="ios", source_account_id="a", source_calendar_id="c",
        source_event_id=eid, event_fields={"title": eid, "start_at": at},
        last_seen_sync_id="old",
    )


def _batch(**over) -> SyncBatch:
    base = dict(source="ios", collection_kind="calendar", sync_id="s1",
                snapshot_kind=INCREMENTAL, items=[_event("e1")])
    base.update(over)
    return SyncBatch(**base)


def _state(storage):
    return storage.get_sync_state(subject_id="u1", source="ios",
                                  collection_kind="calendar")


# ---------------------------------------------------------------------------
# 增量绝不许删
# ---------------------------------------------------------------------------

def test_an_incremental_batch_never_deletes_anything():
    """拿一个局部窗口去删窗口外的数据 —— 用户发现自己去年的日程凭空消失了。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[_event("old-one")])
    # ⚠️ 这一批**带着覆盖范围**。增量给范围是合法的（它说明这批数据来自哪一段），
    #    但它仍然不许删。不给范围的话这条测试会因为错误的原因通过：
    #    就算删除逻辑真的跑了，没有范围也删不掉任何东西。
    out = sync_source_mirror(s, _batch(
        items=[_event("e1")],
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)
    assert out.deleted == 0
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert "old-one" in left, "增量同步把范围内的旧条目删了"


def test_a_host_that_forgot_the_incremental_guard_is_still_safe():
    """端口契约要求 ``snapshot_kind != full`` 时一条都不删，内存参考实现也照做了。

    但**这条不能指望每个宿主都实现对** —— 忘了就是删掉用户范围内的全部
    日程，不可逆。所以 kit 这一层压根不调用它。

    这里用一个"忘了那条守卫"的宿主来证明：kit 根本不会走到那个调用。
    """
    class _ForgetfulHost(InMemoryStorage):
        called_with: list = []

        def apply_source_snapshot(self, **kw):
            _ForgetfulHost.called_with.append(kw["snapshot_kind"])
            return super().apply_source_snapshot(**{**kw, "snapshot_kind": FULL})

    _ForgetfulHost.called_with = []
    s = _ForgetfulHost()
    s.upsert_calendar_events(subject_id="u1", events=[_event("old-one")])
    sync_source_mirror(s, _batch(
        items=[_event("e1")],
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)
    assert _ForgetfulHost.called_with == [], (
        "增量批次调到了快照收尾 —— 宿主只要少一条守卫就会删掉真实数据")
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert "old-one" in left


def test_a_full_batch_deletes_inside_its_coverage():
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[_event("gone")])
    out = sync_source_mirror(s, _batch(
        snapshot_kind=FULL, items=[_event("e1")],
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert "gone" not in left and "e1" in left
    assert out.deleted >= 1


def test_a_full_batch_without_coverage_is_refused():
    """「全量」永远是相对于某个范围说的。没有范围的全量删除删的是全部。"""
    s = InMemoryStorage()
    with pytest.raises(SyncContractError, match="coverage"):
        sync_source_mirror(s, _batch(snapshot_kind=FULL, coverage_start=None,
                                     coverage_end=None), context=CTX)


def test_an_unknown_snapshot_kind_is_refused_not_treated_as_incremental():
    """不认识的种类当成增量放过去：万一它的本意是全量，该删的没删，
    镜像会一直留着来源已经删掉的条目。"""
    s = InMemoryStorage()
    with pytest.raises(SyncContractError, match="snapshot_kind"):
        sync_source_mirror(s, _batch(snapshot_kind="partial"), context=CTX)


def test_an_inverted_coverage_window_is_refused():
    s = InMemoryStorage()
    with pytest.raises(SyncContractError, match="coverage_start"):
        sync_source_mirror(s, _batch(
            snapshot_kind=FULL, coverage_start=T0 + timedelta(days=1),
            coverage_end=T0 - timedelta(days=1)), context=CTX)


# ---------------------------------------------------------------------------
# 失败的一批：记下来，但什么都不动
# ---------------------------------------------------------------------------

def test_a_failed_batch_changes_no_data():
    """来源临时不可达 ≠ 来源侧删光了。"""
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[_event("keep")])
    out = sync_source_mirror(s, _batch(
        snapshot_kind=FULL, error_code="transport_timeout",
        items=[_event("e1")],
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)
    assert out.failed and out.upserted == 0 and out.deleted == 0
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert left == {"keep"}, "失败的一批把数据写进去了"


def test_a_failed_batch_does_not_advance_the_cursor():
    """推进了那段数据就**永远**不会再被同步一次，而且没有任何地方记得它缺过。"""
    s = InMemoryStorage()
    sync_source_mirror(s, _batch(cursor="page-1"), context=CTX)
    assert _state(s).sync_cursor == "page-1"

    sync_source_mirror(s, _batch(cursor="page-2", error_code="http_503"),
                       context=CTX)
    assert _state(s).sync_cursor == "page-1", "失败还把游标推到了 page-2"


def test_a_failed_batch_does_not_touch_last_successful_sync_at():
    """否则「日历数据已过期」这个判断永远为假 —— 同步挂了三天，
    界面还在说这是最新完整的数据。"""
    s = InMemoryStorage()
    sync_source_mirror(s, _batch(), context=CTX)
    ok_at = _state(s).last_successful_sync_at
    assert ok_at is not None

    later = IngestContext("u1", T0 + timedelta(days=3))
    sync_source_mirror(s, _batch(error_code="auth_revoked"), context=later)
    st = _state(s)
    assert st.last_successful_sync_at == ok_at
    assert st.last_error_code == "auth_revoked"
    assert st.last_attempted_at is not None


def test_a_success_clears_a_stale_error_code():
    """留着的话，一次早就恢复的故障会一直挂在状态里，看的人分不清
    是历史还是现在。"""
    s = InMemoryStorage()
    sync_source_mirror(s, _batch(error_code="http_503"), context=CTX)
    assert _state(s).last_error_code == "http_503"
    sync_source_mirror(s, _batch(), context=CTX)
    assert _state(s).last_error_code is None


# ---------------------------------------------------------------------------
# 三个动作在同一个事务里
# ---------------------------------------------------------------------------

def test_the_whole_round_is_one_transaction():
    """分开提交的话，「镜像里少一批条目、同步状态却说这轮成功了」就会出现，
    而下一轮增量同步不会去补 —— 它以为上一轮是完整的。"""
    s = InMemoryStorage()
    before = s.transactions_opened
    sync_source_mirror(s, _batch(
        snapshot_kind=FULL, coverage_start=T0 - timedelta(days=1),
        coverage_end=T0 + timedelta(days=1)), context=CTX)
    assert s.transactions_opened == before + 1, "写条目/删/写状态没在同一个事务里"


def test_a_mixed_batch_is_refused():
    """挑一部分写进去、剩下的丢掉，会得到一份"成功了"的半份镜像。"""
    s = InMemoryStorage()
    mixed = [_event("e1"), ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id="r1", reminder_fields={"title": "买牛奶"})]
    with pytest.raises(SyncContractError, match="混"):
        sync_source_mirror(s, _batch(items=mixed), context=CTX)


def test_reminders_go_down_the_reminder_path():
    s = InMemoryStorage()
    item = ReminderItemMirror(
        subject_id="u1", source="ios", source_account_id="a", source_list_id="l",
        source_reminder_id="r1",
        reminder_fields={"title": "买牛奶", "is_completed": False})
    out = sync_source_mirror(s, SyncBatch(
        source="ios", collection_kind="reminders", sync_id="s1", items=[item]),
        context=CTX)
    assert out.upserted == 1
    assert [r.source_reminder_id
            for r in s.list_reminders(subject_id="u1", limit=10)] == ["r1"]


def test_the_kit_exposes_it_next_to_ingest():
    """和 ingest() 对等的入口 —— 这条是审查里点名要的。"""
    s = InMemoryStorage()
    kit = PerceptionKit(storage=s)
    out = kit.sync_source_mirror(_batch(), context=CTX)
    assert out.upserted == 1


def test_a_full_sync_does_not_delete_the_items_it_just_wrote():
    """全量收尾删的是「覆盖范围内、这轮没见到的」，判据是 last_seen_sync_id。

    这批条目不打上这一批的 sync_id，刚写进去的就会在同一个事务里被自己的
    快照收尾删掉 —— 一次"成功"的全量同步，结果是镜像空了。

    这条是写这个文件时真踩到的：宿主传进来的条目带着上一轮的 sync_id，
    orchestration 原样 upsert，然后 apply_source_snapshot 把它们全删了。
    """
    s = InMemoryStorage()
    stale = [_event("e1"), _event("e2")]          # helper 带的是 last_seen_sync_id="old"
    assert all(e.last_seen_sync_id == "old" for e in stale)
    out = sync_source_mirror(s, _batch(
        snapshot_kind=FULL, sync_id="round-7", items=stale,
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert left == {"e1", "e2"}, f"全量同步把自己刚写的条目删了，剩下 {left}"
    assert out.upserted == 2


# ---------------------------------------------------------------------------
# 审查者自己复现的四条（2026-09-03，§8）
#
# 这四条都是 0.2.8 引入或遗留的契约漏洞。用她给的场景逐条钉住。
# ---------------------------------------------------------------------------

def test_one_source_full_sync_does_not_delete_another_source(_ios=None):
    """§8.2 —— 她复现的：一次 source="ios" 的全量同步删掉了 Google 的日程。

    快照收尾删的是「这轮没见到的」，而另一个来源的条目**当然**没在这轮里。
    少了 source 这一维，用户会发现自己另一个日历账户的日程凭空消失，且不可逆。
    """
    s = InMemoryStorage()
    s.upsert_calendar_events(subject_id="u1", events=[CalendarEventMirror(
        subject_id="u1", source="google", source_account_id="g-acct",
        source_calendar_id="g1", source_event_id="来自 Google 的日程",
        event_fields={"start_at": T0}, last_seen_sync_id="google-round-1")])

    sync_source_mirror(s, _batch(
        snapshot_kind=FULL, sync_id="ios-round-1", items=[],
        coverage_start=T0 - timedelta(days=1), coverage_end=T0 + timedelta(days=1),
    ), context=CTX)

    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert "来自 Google 的日程" in left, "ios 的全量同步删掉了另一个来源的数据"


def test_two_sources_with_the_same_event_id_do_not_overwrite_each_other():
    """同一个 event id 在两个来源系统里各有一条，是完全可能的。"""
    s = InMemoryStorage()
    for src in ("ios", "google"):
        sync_source_mirror(s, SyncBatch(
            source=src, collection_kind="calendar", sync_id=f"{src}-1",
            items=[CalendarEventMirror(
                subject_id="u1", source=src, source_account_id="a",
                source_calendar_id="c", source_event_id="同一个 id",
                event_fields={"title": src, "start_at": T0})]),
            context=CTX)
    titles = {e.event_fields["title"]
              for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert titles == {"ios", "google"}, f"两个来源互相覆盖了，剩下 {titles}"


def test_the_declared_collection_kind_must_match_the_item_type():
    """§8.3 —— 她复现的：collection_kind=reminders 塞日历条目被照单全收。

    后果是数据和游标互相矛盾：日历表被写进去了，而**提醒的游标往前推进了**，
    下一轮增量提醒同步会以为上一轮成功了，那段提醒永远补不回来。
    """
    s = InMemoryStorage()
    with pytest.raises(SyncContractError, match="collection_kind"):
        sync_source_mirror(s, SyncBatch(
            source="ios", collection_kind="reminders", sync_id="s1",
            items=[_event("e1")]), context=CTX)
    # 拒绝时整个事务无变化 —— 两张表都没动，游标也没推进。
    assert not s.list_calendar_events(subject_id="u1", limit=10)
    assert s.get_sync_state(subject_id="u1", source="ios",
                            collection_kind="reminders") is None


def test_an_unknown_collection_kind_is_refused():
    s = InMemoryStorage()
    with pytest.raises(SyncContractError, match="不认识的 collection_kind"):
        sync_source_mirror(s, _batch(collection_kind="photos"), context=CTX)


def test_the_subject_comes_from_the_trusted_context_not_the_item():
    """§8.4 —— 条目自带的 subject_id 是宿主从来源数据翻译出来的。

    最好的情况是冗余，最坏的情况是把 A 的日程写进 B 的花园。
    可信 subject 只有一个来源：IngestContext。
    """
    s = InMemoryStorage()
    impostor = CalendarEventMirror(
        subject_id="别人的-uid", source="ios", source_account_id="a",
        source_calendar_id="c", source_event_id="e1",
        event_fields={"start_at": T0})
    sync_source_mirror(s, _batch(items=[impostor]), context=CTX)   # ctx = u1

    assert not s.list_calendar_events(subject_id="别人的-uid", limit=10), \
        "条目自带的 subject 把数据写进了别人的花园"
    mine = s.list_calendar_events(subject_id="u1", limit=10)
    assert [e.source_event_id for e in mine] == ["e1"]
    assert mine[0].subject_id == "u1"


def test_an_incremental_batch_applies_an_explicit_tombstone():
    """§8.1 —— 「这批里没出现」推断不出删除，但「来源说删了」是确定的事实。

    早先把增量定义成「一条都不许删」，防住了「拿局部列表当全量」，
    但同时堵死了这条：用户在手机上删掉的日程，在 agent 眼里永远还在，
    还会一直出现在"接下来有什么安排"里。
    """
    s = InMemoryStorage()
    sync_source_mirror(s, _batch(items=[_event("keep"), _event("gone")]),
                       context=CTX)
    out = sync_source_mirror(s, _batch(items=[], deleted_item_ids=["gone"]),
                             context=CTX)
    assert out.tombstoned == 1 and out.deleted == 0, \
        "tombstone 和范围删除要分开数，否则分不清是范围判断出错还是来源真删了"
    left = {e.source_event_id for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert left == {"keep"}


def test_a_tombstone_does_not_reach_across_sources():
    """来源 A 说删了某个 id，不能顺手删掉来源 B 里碰巧同 id 的条目。"""
    s = InMemoryStorage()
    for src in ("ios", "google"):
        sync_source_mirror(s, SyncBatch(
            source=src, collection_kind="calendar", sync_id=f"{src}-1",
            items=[CalendarEventMirror(
                subject_id="u1", source=src, source_account_id="a",
                source_calendar_id="c", source_event_id="同一个 id",
                event_fields={"title": src, "start_at": T0})]),
            context=CTX)
    sync_source_mirror(s, SyncBatch(
        source="ios", collection_kind="calendar", sync_id="ios-2",
        deleted_item_ids=["同一个 id"]), context=CTX)
    titles = {e.event_fields["title"]
              for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert titles == {"google"}, f"ios 的删除越界了，剩下 {titles}"


def test_a_failed_batch_does_not_apply_tombstones_either():
    """来源临时不可达 ≠ 来源侧删了 —— 这一批的删除清单同样不可信。"""
    s = InMemoryStorage()
    sync_source_mirror(s, _batch(items=[_event("keep")]), context=CTX)
    out = sync_source_mirror(s, _batch(
        items=[], deleted_item_ids=["keep"], error_code="http_503"), context=CTX)
    assert out.failed and out.tombstoned == 0
    assert [e.source_event_id
            for e in s.list_calendar_events(subject_id="u1", limit=10)] == ["keep"]
