"""A dimension key is public identity, never a route around field visibility."""
from dataclasses import replace

import pytest

from perceptkit import PerceptionKit
from perceptkit.conformance import InMemoryStorage
from perceptkit.manifest import MINIMAL_SIGNALS, validate_manifest
from perceptkit.queries import api

from test_current_export_v010 import T, anchor, exported_rows, seed


def anchor_manifest(*, visibility="always", privacy="personal", dimension="anchor_id"):
    sig = MINIMAL_SIGNALS["proximity_anchor"]
    fields = tuple(replace(f, query_visibility=visibility, privacy_class=privacy,
                           wake_eligible=False, comparison_strategy="none")
                   if f.key == "anchor_id" else f for f in sig.fields)
    changed = replace(sig, fields=fields, dimension_fields=(dimension,))
    return {changed.key: changed}


@pytest.mark.parametrize("visibility,privacy", [
    ("never", "personal"), ("on_demand", "personal"),
    ("always", "restricted"), ("never", "restricted"),
])
def test_manifest_rejects_private_dimension_identity(visibility, privacy):
    manifest = anchor_manifest(visibility=visibility, privacy=privacy)
    problems = validate_manifest(manifest)
    assert any("dimension_fields" in p and "anchor_id" in p for p in problems), problems


@pytest.mark.parametrize("visibility,privacy", [
    ("never", "personal"), ("on_demand", "personal"), ("always", "restricted"),
])
def test_kit_custom_manifest_fails_at_construction(visibility, privacy):
    with pytest.raises(ValueError, match="dimension_fields.*anchor_id"):
        PerceptionKit(InMemoryStorage(), signals=anchor_manifest(visibility=visibility, privacy=privacy))


@pytest.mark.parametrize("entry", ["current", "last_known", "export"])
def test_direct_query_entry_cannot_skip_dimension_capability_validation(entry):
    s = InMemoryStorage()
    anchor(s, "proximity_anchor\x1fprivate-office")
    manifest = anchor_manifest(visibility="never")
    with pytest.raises(ValueError, match="dimension_fields.*anchor_id"):
        if entry == "current":
            api.get_current(s, subject_id="u", signals=["proximity_anchor"], manifest=manifest, now=T, on_demand=False)
        elif entry == "last_known":
            api.get_last_known(s, subject_id="u", signal="proximity_anchor", manifest=manifest, on_demand=False)
        else:
            api.export_subject(s, subject_id="u", manifest=manifest)


def test_replacing_kit_manifest_cannot_bypass_public_dimension_guard():
    kit = PerceptionKit(InMemoryStorage())
    kit.signals = anchor_manifest(visibility="on_demand")
    with pytest.raises(ValueError, match="dimension_fields.*anchor_id"):
        kit.get_current(subject_id="u", signals=["proximity_anchor"], now=T)


def test_missing_dimension_field_rejected_before_storage_identity_is_exposed():
    manifest = anchor_manifest(dimension="missing_field")
    assert any("dimension_fields" in p and "missing_field" in p for p in validate_manifest(manifest))
    with pytest.raises(ValueError, match="dimension_fields.*missing_field"):
        PerceptionKit(InMemoryStorage(), signals=manifest)


def test_default_manifest_keeps_stable_public_dimension_keys_and_both_anchors():
    assert validate_manifest(MINIMAL_SIGNALS) == []
    s = InMemoryStorage()
    sig = MINIMAL_SIGNALS["proximity_anchor"]
    for name in ["office", "home"]:
        key = sig.dimension_key_for({"anchor_id": name})
        anchor(s, key)
    kit = PerceptionKit(s)
    expected = [sig.dimension_key_for({"anchor_id": n}) for n in ["home", "office"]]
    first = kit.get_current(subject_id="u", signals=[sig.key], now=T)[sig.key]
    assert [r.dimension_key for r in first] == expected
    last = kit.get_last_known(subject_id="u", signal=sig.key)
    assert [r.dimension_key for r in last] == expected
    dump = kit.export_subject(subject_id="u")
    assert [r["dimension_key"] for r in dump["current"][sig.key]] == expected


@pytest.mark.parametrize("visibility", ["never", "on_demand"])
def test_internal_aggregate_dimensions_are_not_public_and_policy_change_revalidates(visibility):
    manifest = anchor_manifest(visibility=visibility)
    internal = replace(manifest["proximity_anchor"], current_policy="none")
    internal_manifest = {internal.key: internal}
    assert validate_manifest(internal_manifest) == []
    s = InMemoryStorage()
    anchor(s, "proximity_anchor\x1fprivate-office")
    kit = PerceptionKit(s, signals=internal_manifest)
    assert kit.get_current(subject_id="u", signals=[internal.key], now=T) == {internal.key: []}
    assert kit.get_last_known(subject_id="u", signal=internal.key) == []
    assert kit.export_subject(subject_id="u")["current"] == {}
    exposed = {internal.key: replace(internal, current_policy="latest")}
    assert any("dimension_fields" in p for p in validate_manifest(exposed))
    with pytest.raises(ValueError, match="dimension_fields"):
        PerceptionKit(s, signals=exposed)
    kit.signals = exposed
    with pytest.raises(ValueError, match="dimension_fields"):
        kit.get_current(subject_id="u", signals=[internal.key], now=T)


@pytest.mark.parametrize("window", ["start_only", "end_only"])
@pytest.mark.parametrize("collection", ["health_weight", "daily_aggregates:health_weight", "events", "conflicts"])
def test_export_one_sided_window_matrix(window, collection):
    from datetime import timedelta
    s = InMemoryStorage()
    for offset in [-1, 0, 1]:
        seed(s, collection, 1, prefix=f"day{offset}-", at=T + timedelta(days=offset))
    kwargs = {"start": T} if window == "start_only" else {"end": T}
    rows = exported_rows(PerceptionKit(s).export_subject(subject_id="u", **kwargs), collection)
    assert len(rows) == 2
    field = {"health_weight": "occurred_at", "daily_aggregates:health_weight": "date",
             "events": "occurred_at", "conflicts": "created_at"}[collection]
    expected = [T + timedelta(days=i) for i in ([0, 1] if window == "start_only" else [-1, 0])]
    assert sorted(r[field] for r in rows) == [v.date().isoformat() if field == "date" else v.isoformat() for v in expected]
