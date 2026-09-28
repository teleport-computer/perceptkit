"""撤回之后重选出来的当前值，本身也得是个合格的当前值（外部审查 F10）。

重选那条路是后补的，它写出来的 ``CurrentProjection`` 有三处和正常写入不一致：

    过期时间  写死 None → 一条三年前的旧体重被重选上来之后**永不过期**，
              agent 会把它当成"你现在的体重"说出口
    内容指纹  照抄了**被删掉那条**的指纹 → 指纹和它描述的值对不上，
              靠指纹判"变没变"的地方从此判错
    候选排序  只按时间挑，没有先把同一条事实的多个修订收敛成最新那版 ——
              用户改过的旧值可能被选回来
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.retraction import Retraction
from perceptkit.manifest import MINIMAL_SIGNALS

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
TTL = MINIMAL_SIGNALS["health_weight"].current_ttl_sec


def _kit(storage):
    return PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)


def _weigh(kit, kg, *, at, eid, rev=None):
    obs = {
        "signal": "health_weight", "signal_schema_version": 1,
        "occurred_at": at.isoformat(), "availability": "observed",
        "timezone": "Asia/Shanghai", "source_event_id": eid,
        "value": {"weight_kg": kg},
    }
    if rev is not None:
        obs["source_revision"] = rev
    out = kit.ingest({
        "schema_version": 1, "report_id": f"r-{eid}-{rev}-{at.isoformat()}",
        "producer": "ios", "observations": [obs],
    }, context=IngestContext("u", at))
    assert not out.rejected, list(out.rejected)


def _current_row(storage):
    rows = storage.get_current(subject_id="u", signals=["health_weight"])["health_weight"]
    return rows[0]


def test_a_reselected_value_still_expires():
    """重选上来的旧值必须照常有过期时间。

    ``expires_at=None`` 等于说"这个值永远算数"：用户 2026 年称过一次，
    2028 年问"我现在多重"，agent 会把那个两年前的数字当成现在的说出来。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, 70.5, at=T0, eid="hk-old")
    _weigh(kit, 72.0, at=T0 + timedelta(days=60), eid="hk-new")
    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-new", "ios", T0 + timedelta(days=60))],
        now=T0 + timedelta(days=60))

    row = _current_row(s)
    assert row.typed_value and row.typed_value["weight_kg"] == 70.5, "没重选上来"
    assert row.expires_at is not None, "重选出来的当前值没有过期时间 —— 它永远不会过期"
    assert row.expires_at == row.observed_at + timedelta(seconds=TTL), \
        "过期时间不是按这个值自己的观测时刻算的"

    view = kit.get_current(subject_id="u", signals=["health_weight"],
                           now=T0 + timedelta(days=600))["health_weight"]
    assert view.state != "fresh", "600 天前的旧体重还在冒充「现在」"


def test_a_reselected_value_carries_its_own_fingerprint():
    """内容指纹要描述**它自己**，不能照抄被删掉那条的。

    指纹是用来判"值变没变"的。带着别人的指纹，等于给下游一个错的判据。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, 70.5, at=T0, eid="hk-old")
    _weigh(kit, 72.0, at=T0 + timedelta(hours=1), eid="hk-new")
    stale = _current_row(s).content_digest        # 被删掉那条的指纹

    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-new", "ios", T0 + timedelta(hours=2))],
        now=T0 + timedelta(hours=2))

    assert _current_row(s).content_digest != stale, \
        "重选之后还带着被删掉那条的指纹"


def test_reselection_picks_the_newest_revision_of_a_fact():
    """同一条事实有多个修订时，重选要挑**最新那版**，不是时间上最后写的那条。

    用户把 70.5 改成 68.0（修订 2），后来又删掉了另一条。重选如果只按
    时间挑，会把已经被用户改掉的 70.5 选回来当当前值。
    """
    s = InMemoryStorage(); kit = _kit(s)
    _weigh(kit, 68.0, at=T0 + timedelta(hours=1), eid="hk-A", rev=2)   # 新版本，时间靠前
    # Legacy stores can already contain the stale revision. New D02 ingestion
    # rejects it before projection, so seed that old state directly to keep
    # testing the independent reselect/canonicalization path.
    from dataclasses import replace
    existing = next(iter(s.observations.values()))
    s.append_observation(replace(existing, observation_id="legacy-stale-revision",
                                 typed_value={"weight_kg": 70.5}, source_revision=1,
                                 occurred_at=T0 + timedelta(hours=2)))
    _weigh(kit, 72.0, at=T0 + timedelta(hours=3), eid="hk-B")
    kit.apply_retractions(
        [Retraction("u", "health_weight", "hk-B", "ios", T0 + timedelta(hours=4))],
        now=T0 + timedelta(hours=4))

    value = _current_row(s).typed_value or {}
    assert value.get("weight_kg") == 68.0, \
        f"重选把用户已经改掉的旧版本选回来了：{value}"
