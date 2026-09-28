"""Task 6A review: numeric rejection isolation and canonical fallback identity."""
from datetime import timedelta
from dataclasses import replace

import pytest

from perceptkit import IngestContext, PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.contracts.observation import Observation
from perceptkit.contracts.report import ReportEnvelope
from test_acceptance_regressions_0_9 import T, ingest, observation


@pytest.mark.parametrize("unit", ["kg", "lb", None])
def test_unrepresentable_number_rejects_one_observation_and_finalizes_report(unit):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    extra = {} if unit is None else {"units": {"weight_kg": unit}}
    out = ingest(kit, [observation({"weight_kg": 10**400}, eid="bad", **extra),
                      observation({"weight_kg": 71}, eid="good")])
    assert len(out.rejected) == 1 and "invalid_numeric_value" in str(out.rejected)
    assert [item.stored.source_event_id for item in out.applied] == ["good"]
    assert out.receipt.status == "accepted" and out.receipt.observations_applied == 1
    assert out.receipt.observations_rejected[0].index == 0
    assert storage.reports[("u", "ios", "report")] == out.receipt
    assert len(storage.observations) == len(storage.identities) == 1
    assert not kit.list_conflicts(subject_id="u")
    assert next(iter(storage.observations.values())).source_values == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "negative-inf"])
@pytest.mark.parametrize("unit", ["kg", "lb", None])
def test_python_nonfinite_values_reject_one_observation(value, unit):
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    extensions = {} if unit is None else {"units": {"weight_kg": unit}}
    bad = Observation("health_weight", 1, T, "observed", {"weight_kg": value},
                      source_event_id="bad", timezone="UTC", extensions=extensions)
    good = Observation("health_weight", 1, T, "observed", {"weight_kg": 71},
                       source_event_id="good", timezone="UTC")
    report = ReportEnvelope(1, "nonfinite", "ios", (bad, good))
    out = kit.ingest(report, context=IngestContext("u", T))
    assert len(out.rejected) == 1 and "invalid_numeric_value" in str(out.rejected)
    assert [item.stored.source_event_id for item in out.applied] == ["good"]
    assert out.receipt.status == "accepted" and out.receipt.observations_applied == 1
    assert out.receipt.observations_rejected[0].index == 0
    assert storage.reports[("u", "ios", "nonfinite")] == out.receipt
    retry = kit.ingest(report, context=IngestContext("u", T))
    assert retry.receipt.status == "duplicate" and not retry.applied
    assert retry.receipt.observations_rejected == out.receipt.observations_rejected
    assert len(storage.observations) == 1 and not storage.conflicts


def test_finite_source_that_overflows_conversion_rejects_one_observation():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    out = ingest(kit, [observation({"height_cm": 1e308}, signal="health_height",
                                  eid="bad", units={"height_cm": "m"}),
                      observation({"weight_kg": 71}, eid="good")])
    assert len(out.rejected) == 1 and "invalid_numeric_value" in str(out.rejected)
    assert [item.stored.source_event_id for item in out.applied] == ["good"]
    assert storage.get_current(subject_id="u", signals=["health_height"])["health_height"] == []


def test_conversion_value_error_is_local_but_programming_error_is_not_swallowed(monkeypatch):
    import perceptkit.processing.normalize as normalize
    kit = PerceptionKit(InMemoryStorage())
    wire = [observation({"weight_kg": 70}, units={"weight_kg": "lb"}),
            observation({"weight_kg": 71}, eid="good")]
    def expected_failure(*args, **kwargs):
        raise ValueError("unrepresentable conversion")
    monkeypatch.setattr(normalize, "convert", expected_failure)
    out = ingest(kit, wire)
    assert len(out.rejected) == 1 and len(out.applied) == 1
    def programming_error(*args, **kwargs):
        raise RuntimeError("conversion bug")
    monkeypatch.setattr(normalize, "convert", programming_error)
    with pytest.raises(RuntimeError, match="conversion bug"):
        ingest(kit, wire, report_id="programming-error")


def send_no_id(kit, value, at, report_id):
    wire = observation({"weight_kg": value}, at=at)
    del wire["source_event_id"]
    return ingest(kit, [wire], at=T + timedelta(hours=3), report_id=report_id)


def test_late_no_id_fact_does_not_use_future_current_as_its_own_revision():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    assert send_no_id(kit, 100, T + timedelta(hours=2), "future").applied
    out = send_no_id(kit, 70, T, "late")
    assert len(out.applied) == 1 and not out.conflicts
    assert not kit.list_conflicts(subject_id="u")
    assert len(storage.observations) == 2
    current = storage.get_current(subject_id="u", signals=["health_weight"])["health_weight"][0]
    assert current.typed_value == {"weight_kg": 100}


def test_late_no_id_fact_still_checks_real_chronological_predecessor():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    assert send_no_id(kit, 100, T + timedelta(hours=2), "future").applied
    assert send_no_id(kit, 70, T, "predecessor").applied
    out = send_no_id(kit, 100, T + timedelta(hours=1), "late")
    assert out.conflicts and not out.applied
    assert kit.list_conflicts(subject_id="u")[0].kind == "relative_jump"


def test_source_identified_correction_compares_its_own_applied_fact():
    storage = InMemoryStorage()
    kit = PerceptionKit(storage)
    def send(value, eid, at, revision, report_id):
        return ingest(kit, [observation({"weight_kg": value}, eid=eid, at=at, revision=revision)],
                      at=T + timedelta(hours=3), report_id=report_id)
    assert send(100, "future", T + timedelta(hours=2), 1, "future").applied
    assert send(70, "original", T, 1, "original").applied
    # 100 matches Current, but is an invalid jump from this Fact's own 70.
    out = send(100, "original", T, 2, "correction")
    assert out.conflicts and not out.applied
    assert kit.list_conflicts(subject_id="u")[0].kind == "relative_jump"


def test_optional_source_id_does_not_override_deterministic_fact_strategy():
    from perceptkit.manifest import MINIMAL_SIGNALS
    signal = replace(MINIMAL_SIGNALS["health_weight"], identity_strategy="deterministic_digest")
    kit = PerceptionKit(InMemoryStorage(), signals={signal.key: signal})
    future = observation({"weight_kg": 100}, eid="optional-diagnostic", at=T + timedelta(hours=2))
    past = observation({"weight_kg": 70}, eid="optional-diagnostic", at=T)
    assert ingest(kit, [future], report_id="future", at=T + timedelta(hours=3)).applied
    out = ingest(kit, [past], report_id="past", at=T + timedelta(hours=3))
    assert out.applied and not out.conflicts and not kit.list_conflicts(subject_id="u")


def test_nonfinite_report_fingerprints_are_distinct_and_fact_canonicalization_stays_strict():
    from perceptkit.contracts.report import canonical_semantics
    from perceptkit.contracts.errors import ContractError
    kit = PerceptionKit(InMemoryStorage())
    bad = Observation("health_weight", 1, T, "observed", {"weight_kg": float("nan")},
                      source_event_id="bad", timezone="UTC")
    report = ReportEnvelope(1, "nonfinite", "ios", (bad,))
    first = kit.ingest(report, context=IngestContext("u", T))
    assert first.receipt.status == "accepted"
    assert first.receipt.observations_rejected
    changed = replace(report, observations=(replace(bad, value={"weight_kg": float("inf")}),))
    assert kit.ingest(changed, context=IngestContext("u", T)).receipt.status == "conflict"
    with pytest.raises(ContractError):
        canonical_semantics({"weight_kg": float("nan")})
