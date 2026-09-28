"""写一半失败之后，重试要能把事情收完（外部审查 F3 / F4，2026-09-14）。

两处都是同一个形状：**先提交了"已经做过了"的标记，再去做真正的事**。
中间失败的话，下一次重试看到标记就以为上一轮成功了，于是永远补不回来：

    撤回      撤回记下了（事务已提交）→ 重算聚合时崩了
              重试同一条撤回 → "这条记过了" → 直接跳过 → 日聚合永远停在旧数字
    定时规则  规则状态标成"今天已触发"→ 写待发事件时崩了
              重试 → "今天已经触发过了" → 不再出事件 → 那次提醒永远不会发

内存 reference 使用快照回滚；这里仍不冒称验过数据库隔离 —— 它验的是
Kit 的事务边界与重试义务。宿主仍需真 PostgreSQL 两连接注入失败来验。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.retraction import Retraction
from perceptkit.manifest import MINIMAL_SIGNALS

T0 = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
DAY = date(2026, 9, 6)


class _DepthSpy(InMemoryStorage):
    """记下每个写操作**发生时**在不在事务里。"""

    def __init__(self):
        super().__init__()
        self.depth_at: dict[str, int] = {}

    def _mark(self, name):
        self.depth_at[name] = self.transaction_depth

    def put_rule_state(self, *a, **kw):
        self._mark("put_rule_state")
        return super().put_rule_state(*a, **kw)

    def enqueue_event(self, *a, **kw):
        self._mark("enqueue_event")
        return super().enqueue_event(*a, **kw)

    def put_aggregate(self, *a, **kw):
        self._mark("put_aggregate")
        return super().put_aggregate(*a, **kw)

    def record_retraction(self, *a, **kw):
        self._mark("record_retraction")
        return super().record_retraction(*a, **kw)


def _kit(storage, **kw):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS, **kw)


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


# ---------------------------------------------------------------------------
# F3：撤回已提交、重算失败之后，重试要能收尾
# ---------------------------------------------------------------------------

def test_retrying_the_same_retraction_still_finishes_the_job():
    """同一条撤回重试一次，受影响的日期必须照样报出来。

    原来是 `记过了 → continue`：重试直接跳过，调用方拿不到要重算哪几天，
    于是"撤回记下了、当前值改了，而日聚合永远停在被删掉的那个数字"。
    重试是常态 —— 第一次就是在重算那一步崩的。
    """
    s = _DepthSpy(); kit = _kit(s)
    _weigh(kit, 70.5, at=T0, eid="hk-A")
    _weigh(kit, 72.0, at=T0 + timedelta(hours=1), eid="hk-B")

    r = Retraction("u", "health_weight", "hk-B", "ios", T0 + timedelta(hours=2))
    first = kit.apply_retractions([r], now=T0 + timedelta(hours=2))
    assert first.affected_days, "第一次就没报出受影响的日期"

    again = kit.apply_retractions([r], now=T0 + timedelta(hours=3))
    assert again.recorded == 0, "重复的撤回不该再算一次 recorded"
    assert again.affected_days == first.affected_days, \
        "重试时受影响日期没报出来 —— 崩在重算那一步就再也补不回来了"


def test_the_recompute_runs_inside_the_same_transaction_as_the_retraction():
    """记撤回和重算聚合必须在同一个事务里。

    分开提交的话会出现"撤回记下了，但聚合还是被删掉的那个数字"，
    而下一轮不会去修 —— 它以为上一轮成功了。
    """
    s = _DepthSpy(); kit = _kit(s)
    _weigh(kit, 70.5, at=T0, eid="hk-A")
    # ⚠️ 收数据那一步自己也会写聚合。不清掉的话，这条测试在"重算被整个
    #    去掉"时照样绿 —— 它量的是别人留下的痕迹（故障注入抓到过）。
    s.depth_at.clear()

    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-A", "ios", T0 + timedelta(hours=2))],
        now=T0 + timedelta(hours=2))

    assert s.depth_at.get("record_retraction", 0) > 0, "记撤回没包在事务里"
    assert "put_aggregate" in s.depth_at, "撤回之后根本没重算聚合"
    assert s.depth_at["put_aggregate"] > 0, \
        "重算聚合在事务外面 —— 撤回提交了、重算崩了，就再也对不上了"


# ---------------------------------------------------------------------------
# F4：定时规则的状态和待发事件必须同生共死
# ---------------------------------------------------------------------------

def _absence_rule():
    from perceptkit.rules import EventDefinition
    return EventDefinition.parse({
        "id": "weight_gone_quiet", "version": 1,
        "source": {"signal": "health_weight", "field": "weight_kg"},
        "condition": {"type": "absence", "value": 172800},   # 2 天
        "lifecycle": {"scope": "local_day", "fire": "once"},
        "event": {"type": "health.weight_not_logged"},
    })


def test_absence_writes_rule_state_and_event_in_one_transaction():
    """"今天已触发"和"待发事件"要么都成、要么都不成。

    先提交状态、再写事件的话：事件写失败之后重试看到"今天已经触发过了"，
    那次提醒永远不会发出去 —— 用户等的那句话再也不来了，而且没有任何报错。
    """
    s = _DepthSpy(); kit = _kit(s, definitions=[_absence_rule()])
    _weigh(kit, 70.5, at=T0 - timedelta(days=3), eid="hk-old")

    out = kit.evaluate_absence(subject_id="u", now=T0)
    assert out.events, f"规则没触发，这条测的东西没发生：{out}"
    assert s.depth_at.get("put_rule_state", 0) > 0, "规则状态写在事务外面"
    assert s.depth_at.get("enqueue_event", 0) > 0, \
        "待发事件写在事务外面 —— 状态提交了、事件没写成，那次提醒就永远丢了"


def test_daily_evaluation_is_wrapped_too():
    """按天判的规则走的是同一条求值路径，同样要包住。"""
    s = _DepthSpy(); kit = _kit(s, definitions=[_absence_rule()])
    _weigh(kit, 70.5, at=T0 - timedelta(days=3), eid="hk-old")

    before = s.transactions_opened          # 前面 ingest 自己也开过事务
    kit.evaluate_daily(subject_id="u", local_date=DAY, now=T0)
    assert s.transactions_opened > before, "evaluate_daily 自己一个事务都没开"


def test_acceptance_A06_exhausted_current_cas_rolls_back_every_write_and_can_retry():
    from contextlib import contextmanager
    from copy import deepcopy
    from test_acceptance_regressions_0_9 import T, ingest, observation, weight_rule

    class RollbackStore(InMemoryStorage):
        """A transactional port double, not a claim of real DB isolation.

        Only inject CAS rejection; keep all other real storage side effects.
        A transaction restores persisted collections if Kit raises an error.
        """
        fail_cas = True
        persisted = ("reports", "observations", "identities", "current", "aggregates",
                     "rule_state", "outbox", "receipts", "retractions")

        @contextmanager
        def transaction(self):
            before = {key: deepcopy(getattr(self, key)) for key in self.persisted}
            with super().transaction():
                try:
                    yield
                except Exception:
                    for key, value in before.items():
                        setattr(self, key, value)
                    raise

        def compare_and_put_current(self, projection, *, expected_version):
            if self.fail_cas:
                return False
            return super().compare_and_put_current(projection, expected_version=expected_version)

    storage = RollbackStore()
    kit = PerceptionKit(storage, definitions=[weight_rule()])
    sample = observation({"weight_kg": 70})
    error = None
    try:
        ingest(kit, [sample])
    except Exception as caught:
        error = caught
    # Combined assertion reports every partial write, even when no error is
    # raised. No exception class is invented ahead of the retryable contract.
    visible = {key: len(getattr(storage, key)) for key in storage.persisted}
    assert (error is not None, visible) == (True, dict.fromkeys(storage.persisted, 0)), {
        "error": repr(error), "visible_after_failed_cas": visible,
    }
    storage.fail_cas = False
    retry = ingest(kit, [sample])
    assert len(retry.applied) == 1 and not retry.duplicates
    assert kit.get_current(subject_id="u", signals=["health_weight"], now=T)["health_weight"].value == {"weight_kg": 70}
