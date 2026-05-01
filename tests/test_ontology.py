#!/usr/bin/env python3
"""Test suite for the FRF ontology engine, exercised against the LIRR
stations.csv dataset and the views in views/.

Run from the project root:
    python3 test_ontology.py
"""

import json
import sys
import frf_mcp as engine
import frf_ont as ont


def assert_eq(actual, expected, msg):
    if actual != expected:
        print(f"  FAIL: {msg}\n     expected: {expected!r}\n     actual:   {actual!r}")
        return False
    print(f"  PASS: {msg}")
    return True


def assert_true(cond, msg):
    if not cond:
        print(f"  FAIL: {msg}")
        return False
    print(f"  PASS: {msg}")
    return True


def test_self_describing():
    """A CSV with eesr_type column auto-promotes to a view."""
    print("\n[1] self-describing source auto-promotes")
    views = ont.discover_views()
    ok = assert_true("stations" in views, "stations view exists")
    v = views["stations"]
    # YAML override is loaded, but it preserves the auto-detected category
    ok &= assert_eq(v.category, "Entity", "category=Entity")
    ok &= assert_eq(v.domain_type, "Station", "domain_type=Station")
    return ok


def test_virtual_view():
    """branches.yaml synthesizes a collection from stations.branch_primary."""
    print("\n[2] virtual view derives branches from a column")
    views = ont.discover_views()
    registry = engine.discover_sources()
    ok = assert_true("branches" in views, "branches view exists")
    branches = list(ont.materialize(views["branches"], registry))
    ok &= assert_true(len(branches) >= 5, f"derived multiple branches (got {len(branches)})")
    # check shape of one row
    first = branches[0]
    ok &= assert_true("branch_id" in first and "branch_name" in first,
                       "branches have id + name")
    ok &= assert_true("member_count" in first, "branches have member_count")
    ok &= assert_true("counties" in first and isinstance(first["counties"], list),
                       "branches aggregate distinct counties")
    return ok


def test_anomaly_detection():
    """Constraint flagging on stations.csv: rows where station_name endswith 'Branch'."""
    print("\n[3] anomaly detection finds suspect rows")
    views = ont.discover_views()
    registry = engine.discover_sources()
    anomalies = ont.evaluate_constraints(views["stations"], registry)

    # We expect at least the rows whose station_name ends with "Branch".
    name_anoms = [a for a in anomalies if any(
        v["name"] == "station_name_not_branch_name" for v in a["_violations"])]
    ok = assert_true(len(name_anoms) >= 3,
                      f"flagged {len(name_anoms)} rows with branchy station_name")
    flagged_names = {a.get("station_name") for a in name_anoms}
    ok &= assert_true(any("Babylon" in n for n in flagged_names),
                       "Babylon Branch flagged")
    ok &= assert_true(any("Oyster Bay" in n for n in flagged_names),
                       "Oyster Bay Branch flagged")
    ok &= assert_true(any("Montauk" in n for n in flagged_names),
                       "Montauk Branch flagged")
    return ok


def test_provenance_view():
    """Artifact view exposes source/url/coordinate_basis."""
    print("\n[4] Artifact view exposes provenance")
    views = ont.discover_views()
    registry = engine.discover_sources()
    ok = assert_true("station_provenance" in views, "station_provenance view exists")
    v = views["station_provenance"]
    ok &= assert_eq(v.category, "Artifact", "category=Artifact")
    rows = list(ont.materialize(v, registry))
    ok &= assert_true(len(rows) > 100, f"projects all stations (got {len(rows)})")
    first = rows[0]
    ok &= assert_true("source_url" in first, "aliased field source_url present")
    ok &= assert_true("coordinate_basis" in first, "coordinate_basis present")
    ok &= assert_true("represents" in first, "aliased field represents present")
    return ok


def test_relation_resolution():
    """Given a station, find its branch."""
    print("\n[5] relation resolves station -> branch")
    views = ont.discover_views()
    registry = engine.discover_sources()
    # pick a row from stations
    src = registry["stations"]
    target_row = None
    for row in engine._iter_rows(src):
        if row.get("station_id") == "mineola":
            target_row = row
            break
    ok = assert_true(target_row is not None, "found Mineola station")
    related = ont.find_related("stations", target_row, registry, views)
    ok &= assert_true("branch" in related, "stations->branch relation resolved")
    branches = related["branch"]
    ok &= assert_true(len(branches) >= 1, "Mineola is on at least one branch")
    ok &= assert_true(any("Main Line" in b.get("branch_name", "") for b in branches),
                       "Mineola is on Main Line")
    return ok


def test_explain_trail():
    """Provenance trail describes how each output field maps to a source column."""
    print("\n[6] explain trail shows source.column -> view.field")
    views = ont.discover_views()
    trail = ont.explain_feature(views["station_provenance"], {})
    ok = assert_eq(trail["category"], "Artifact", "trail carries category")
    ok &= assert_eq(trail["primary_source"], "stations", "trail carries primary source")
    fields = trail["fields"]
    represents_entry = next((f for f in fields if f["field"] == "represents"), None)
    ok &= assert_true(represents_entry is not None, "represents field has trail entry")
    ok &= assert_eq(represents_entry["source_column"], "station_name",
                      "represents traces to station_name")
    return ok


def test_mcp_ont_commands():
    """MCP commands -ont, -rel, -anom, -explain dispatch correctly."""
    print("\n[7] MCP ontology commands dispatch")
    ok = True
    out = engine.run("-ont")
    ok &= assert_true("stations" in out and "branches" in out,
                       "-ont lists views")
    ok &= assert_true("Entity" in out and "Artifact" in out,
                       "-ont shows categories")
    out = engine.run("-rel stations")
    ok &= assert_true("branch" in out, "-rel stations shows branch relation")
    out = engine.run("-anom stations")
    ok &= assert_true("station_name_not_branch_name" in out,
                       "-anom stations flags constraint")
    out = engine.run("-explain station_provenance")
    ok &= assert_true("Artifact" in out, "-explain reveals category")
    out = engine.run("-ont -s entity")
    ok &= assert_true("stations" in out and "branches" in out,
                       "-ont -s entity filters by category")
    return ok


def test_view_aware_features():
    """get_view_features produces view-aware FeatureCollections with frf:* metadata."""
    print("\n[8] view-aware feature collection")
    fc = ont.get_view_features("stations", limit=3)
    ok = assert_eq(fc["frf:category"], "Entity", "FeatureCollection carries frf:category")
    ok &= assert_eq(len(fc["features"]), 3, "limit honored")
    feat = fc["features"][0]
    ok &= assert_eq(feat["frf:category"], "Entity", "Feature carries frf:category")
    ok &= assert_eq(feat["frf:domainType"], "Station", "Feature carries frf:domainType")
    ok &= assert_true(feat["geometry"]["type"] == "Point", "geometry is Point")

    # explain=true adds frf:explain
    fc2 = ont.get_view_features("stations", limit=1, explain=True)
    feat2 = fc2["features"][0]
    ok &= assert_true("frf:explain" in feat2, "explain trail added when requested")
    return ok


def test_branches_geometry_is_none():
    """Virtual branches view has no geometry — it's a derived Entity, not a feature."""
    print("\n[9] virtual branches has no geometry (correct ontologically)")
    views = ont.discover_views()
    v = views["branches"]
    ok = assert_eq(v.geom_kind, "none",
                    "branches has no geometry (derived from a column, not a feature)")
    return ok


def main():
    tests = [
        test_self_describing,
        test_virtual_view,
        test_anomaly_detection,
        test_provenance_view,
        test_relation_resolution,
        test_explain_trail,
        test_mcp_ont_commands,
        test_view_aware_features,
        test_branches_geometry_is_none,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    print(f"\n{'='*50}")
    print(f"  {passed}/{len(tests)} test groups passed")
    print(f"{'='*50}")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
