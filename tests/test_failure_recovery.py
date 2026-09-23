"""写一半失败之后，重试要能把事情收完（外部审查 F3 / F4，2026-09-14）。

两处都是同一个形状：**先提交了"已经做过了"的标记，再去做真正的事**。
中间失败的话，下一次重试看到标记就以为上一轮成功了，于是永远补不回来：

    撤回      撤回记下了（事务已提交）→ 重算聚合时崩了
              重试同一条撤回 → "这条记过了" → 直接跳过 → 日聚合永远停在旧数字
    定时规则  规则状态标成"今天已触发"→ 写待发事件时崩了
              重试 → "今天已经触发过了" → 不再出事件 → 那次提醒永远不会发

内存存储**没有真正的回滚**，所以这里不冒称验过了原子性 —— 它验的是
库这一侧的义务：该包在同一个事务里的确实包了，以及重试能收尾。
真正的回滚由宿主的数据库保证，要用真 PostgreSQL 两连接注入失败来验。
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
    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-A", "ios", T0 + timedelta(hours=2))],
        now=T0 + timedelta(hours=2))

    assert s.depth_at.get("record_retraction", 0) > 0, "记撤回没包在事务里"
    assert s.depth_at.get("put_aggregate", 0) > 0, \
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
