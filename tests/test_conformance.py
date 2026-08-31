"""一致性套件本身有没有牙。

只测"正确的实现能通过"是没有意义的守卫 —— 条件写反、路径写错，它照样全绿。
这里每条保证都配一个**故意写坏的 adapter**，验证套件确实会因为那个缺口报错。

坏法都是真实会犯的错，不是为了凑测试瞎改：
忘了去重、清理时连去重身份一起删、全量同步删过了头、租约不生效……
"""
from __future__ import annotations

from perceptkit.conformance import (
    GUARANTEES,
    NOT_PROVABLE_IN_MEMORY,
    InMemoryStorage,
    run_storage_conformance,
)


def broken(**overrides):
    """造一个某处写坏了的 adapter 工厂。"""
    def factory():
        s = InMemoryStorage()
        for name, fn in overrides.items():
            setattr(s, name, fn.__get__(s, InMemoryStorage))
        return s
    return factory


def hits(problems, keyword):
    return [p for p in problems if keyword in p]


# ---------------------------------------------------------------------------

def test_a_correct_adapter_passes_everything():
    assert run_storage_conformance(InMemoryStorage) == []


def test_the_suite_covers_every_guarantee():
    assert len(GUARANTEES) == 11


# ---------------------------------------------------------------------------
# 每条保证配一个真实会犯的错
# ---------------------------------------------------------------------------

def test_catches_an_adapter_that_forgets_report_idempotency():
    """最常见的写法：直接 upsert，不管这批是不是处理过。"""
    def claim_report(self, *, subject_id, producer, report_id, payload_digest,
                     received_at):
        from perceptkit.contracts.receipt import INGEST_ACCEPTED, IngestReceipt
        return IngestReceipt(subject_id=subject_id, producer=producer,
                             report_id=report_id, payload_digest=payload_digest,
                             received_at=received_at, status=INGEST_ACCEPTED)
    problems = run_storage_conformance(broken(claim_report=claim_report))
    assert hits(problems, "①") and hits(problems, "③")


def test_catches_an_adapter_that_ignores_the_expected_version():
    """写成简单 upsert 的话，两条并发上报谁后写完谁赢 —— 旧数据可能赢。"""
    def compare_and_put_current(self, projection, *, expected_version):
        key = (projection.subject_id, projection.signal, projection.dimension_key)
        self.current[key] = projection
        return True
    problems = run_storage_conformance(broken(
        compare_and_put_current=compare_and_put_current))
    assert hits(problems, "②")


def test_catches_an_adapter_whose_dedupe_memory_does_not_survive_cleanup():
    """🔴 最危险的一种：清理明细时把去重身份一起删了。
    症状是第 8 天的一次重放把永久聚合的数字加两遍，而且无法回滚。"""
    def delete_observations(self, *, subject_id, signal=None, before=None):
        doomed = [k for k, o in self.observations.items()
                  if o.subject_id == subject_id]
        for k in doomed:
            del self.observations[k]
        self.identities = {i for i in self.identities if i[0] != subject_id}
        return len(doomed)
    problems = run_storage_conformance(broken(delete_observations=delete_observations))
    assert hits(problems, "④") and hits(problems, "⑨")


def test_catches_an_adapter_with_no_transaction_handle():
    def transaction(self):
        raise NotImplementedError("我这边没有事务")
    problems = run_storage_conformance(broken(transaction=transaction))
    assert hits(problems, "⑤")


def test_catches_an_adapter_that_loses_events_before_dispatch():
    """先投递再落地的宿主会长这样：入队是个空操作。"""
    def enqueue_event(self, entry):
        return True
    problems = run_storage_conformance(broken(enqueue_event=enqueue_event))
    assert hits(problems, "⑥")


def test_catches_an_adapter_whose_lease_does_not_actually_lock():
    """两个 worker 同时领到同一个事件 → 用户被同一件事提醒两次。"""
    def claim_pending_event(self, *, worker_id, now, lease_seconds):
        for entry in self.outbox.values():
            if not entry.is_terminal:
                return entry
        return None
    problems = run_storage_conformance(broken(
        claim_pending_event=claim_pending_event))
    assert hits(problems, "⑦")


def test_catches_a_full_sync_that_deletes_beyond_its_coverage_window():
    """拿局部窗口去删窗口外的数据 —— 用户会发现自己去年的日程凭空消失。"""
    def apply_source_snapshot(self, *, subject_id, source, collection_kind, sync_id,
                              coverage_start, coverage_end, snapshot_kind):
        store = self.calendar if collection_kind == "calendar" else self.reminders
        doomed = [k for k, v in store.items()
                  if k[0] == subject_id and v.last_seen_sync_id != sync_id]
        for k in doomed:
            del store[k]
        return len(doomed)
    problems = run_storage_conformance(broken(
        apply_source_snapshot=apply_source_snapshot))
    assert hits(problems, "⑧")


def test_catches_an_incremental_sync_that_deletes_anything():
    """增量同步只知道"变了什么"，不知道"还剩什么" —— 它没有资格删。"""
    def apply_source_snapshot(self, *, subject_id, source, collection_kind, sync_id,
                              coverage_start, coverage_end, snapshot_kind):
        store = self.calendar if collection_kind == "calendar" else self.reminders
        doomed = [k for k, v in store.items()
                  if k[0] == subject_id and v.last_seen_sync_id != sync_id
                  and coverage_start <= (v.event_fields.get("start_at") or coverage_start)
                  <= coverage_end]
        for k in doomed:
            del store[k]
        return len(doomed)          # 不区分 full / incremental
    problems = run_storage_conformance(broken(
        apply_source_snapshot=apply_source_snapshot))
    assert hits(problems, "⑧")


def test_catches_dedupe_identities_leaking_across_users():
    """串了的话，u2 的新数据会被当成 u1 的重复丢掉 —— 数据凭空少了，还不报错。"""
    def has_seen_identity(self, *, subject_id, signal, source, digest):
        return any(i[3] == digest for i in self.identities)
    problems = run_storage_conformance(broken(has_seen_identity=has_seen_identity))
    assert hits(problems, "⑩")


def test_catches_a_purge_that_leaves_things_behind():
    """"删除我的数据"这件事没有部分成功。"""
    def purge_subject(self, *, subject_id):
        doomed = [k for k, o in self.observations.items()
                  if o.subject_id == subject_id]
        for k in doomed:
            del self.observations[k]
        return {"observations": len(doomed)}        # 忘了删去重身份
    problems = run_storage_conformance(broken(purge_subject=purge_subject))
    assert hits(problems, "⑩")


def test_catches_a_purge_that_deletes_too_much():
    def purge_subject(self, *, subject_id):
        n = len(self.observations)
        self.observations.clear()
        self.identities.clear()
        return {"observations": n}
    problems = run_storage_conformance(broken(purge_subject=purge_subject))
    assert hits(problems, "⑩")


# ---------------------------------------------------------------------------
# 诚实性
# ---------------------------------------------------------------------------

def test_the_suite_says_out_loud_what_it_cannot_prove():
    """内存实现天然原子、天然无并发 —— 把这几条当验过了，
    是这套东西最危险的用法。"""
    assert "⑤提供原子边界" in NOT_PROVABLE_IN_MEMORY
    from perceptkit.conformance import suite
    # 文档必须明写它证明不了什么 —— 否则宿主会拿内存全绿当成原子性验过了
    assert "不能证明" in suite.__doc__
    assert "永远是绿的" in suite.__doc__


def test_a_check_that_blows_up_is_reported_not_silently_skipped():
    """检查本身抛异常时不能当成通过 —— 那是最坏的一种假绿。"""
    def append_observation(self, observation):
        raise RuntimeError("数据库连不上")
    problems = run_storage_conformance(broken(append_observation=append_observation))
    assert any("检查本身抛异常" in p for p in problems)


def test_catches_an_adapter_whose_reminder_mirror_does_not_round_trip():
    """提醒镜像写得进、读不回来 —— 这条是从一个真实现上倒推出来的。

    ⑧ 一直在用日历，所以日历那半有人走；提醒那半一次都没被碰过，
    于是一个整条提醒镜像都不通的 adapter 照样全绿。
    """
    def upsert_reminders(self, *, subject_id, items):
        return None                     # 悄悄什么都不做
    problems = run_storage_conformance(broken(upsert_reminders=upsert_reminders))
    assert hits(problems, "⑪")


def test_catches_an_adapter_that_lists_completed_reminders_by_default():
    def list_reminders(self, *, subject_id, include_completed=False, limit=50):
        return [v for k, v in self.reminders.items() if k[0] == subject_id][:limit]
    problems = run_storage_conformance(broken(list_reminders=list_reminders))
    assert hits(problems, "⑪")
