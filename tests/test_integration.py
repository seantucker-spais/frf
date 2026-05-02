#!/usr/bin/env python3
"""HTTP integration tests — verify the v1.3 protocol mapping.

The registry's protocol correctness is covered by test_registry.py.
These tests exercise the HTTP layer's translation of resolver outcomes
into HTTP responses:
  - 200 OK (path resolves)
  - 200 OK + Warning header + nameWarning body (backup resolves)
  - 308 Permanent Redirect (name-history fallback)
  - 410 Gone (tombstone)
  - 404 Not Found (nothing resolves)
"""

import sys

import frf_mcp as engine
import frf_registry
from frf_http import app


def assert_true(c, msg):
    if not c:
        print(f"  FAIL: {msg}")
        return False
    print(f"  PASS: {msg}")
    return True


def assert_eq(a, b, msg):
    if a != b:
        print(f"  FAIL: {msg}\n     expected {b!r}\n     actual   {a!r}")
        return False
    print(f"  PASS: {msg}")
    return True


def setup():
    """Clean slate: wipe registry file and reset module singleton."""
    from pathlib import Path
    p = Path(__file__).parent / "views" / "_registry.yaml"
    if p.exists():
        p.unlink()
    frf_registry.reset_registry_for_tests()


def test_path_resolves_normally():
    """Step 1: name resolves → 200 OK, no Warning header."""
    print("\n[H1] step 1: path name resolves → 200 OK, no warnings")
    setup()
    client = app.test_client()
    rv = client.get("/collections/stations")
    body = rv.get_json()
    ok = assert_eq(rv.status_code, 200, "200 OK")
    ok &= assert_true("item" in body, "response includes item (server UUID)")
    ok &= assert_true("uuid" in body, "response includes uuid (global UUID)")
    ok &= assert_true(rv.headers.get("Warning") is None,
                       "no Warning header on happy path")
    ok &= assert_true("RestReference:nameWarning" not in body,
                       "no nameWarning body field on happy path")
    return ok


def test_legacy_client_after_rename_gets_308():
    """Brief §2.4: stale name with no backups → 308 redirect."""
    print("\n[H2] §2.4: legacy client + stale name → 308 redirect")
    setup()
    client = app.test_client()

    # Discover and capture identifiers
    rv = client.get("/collections/stations")
    body = rv.get_json()
    item = body["item"]

    # Rename
    idreg = frf_registry.get_registry()
    idreg.rename(item, "mta_lirr_stations")

    # Legacy client requests the stale name with no backups
    rv = client.get("/collections/stations", follow_redirects=False)
    ok = assert_eq(rv.status_code, 308, "308 Permanent Redirect")
    location = rv.headers.get("Location", "")
    ok &= assert_true("mta_lirr_stations" in location,
                       f"Location points to canonical: {location}")
    link = rv.headers.get("Link", "")
    ok &= assert_true('rel="canonical"' in link,
                       f"Link: rel=\"canonical\" present: {link}")
    return ok


def test_optin_client_with_history_disabled_gets_drift():
    """Brief §2: opt-in client + history-disabled server → 200 + drift signal."""
    print("\n[H3] §2: history disabled, opt-in client → 200 + drift")
    setup()
    client = app.test_client()

    # Bootstrap with default config to populate
    rv = client.get("/collections/stations")
    body = rv.get_json()
    item = body["item"]
    uid = body["uuid"]

    # Now swap to a history-disabled registry pointed at the same file
    from pathlib import Path
    p = Path(__file__).parent / "views" / "_registry.yaml"
    frf_registry._global_registry = frf_registry.Registry(
        path=p, name_history_enabled=False)

    # Rename via the new (disabled-history) registry
    idreg = frf_registry.get_registry()
    idreg.rename(item, "mta_lirr_stations_v2")

    # Opt-in client: stale name + backups
    rv = client.get(f"/collections/stations?item={item}",
                     follow_redirects=False)
    ok = assert_eq(rv.status_code, 200,
                    "200 OK (no history → step 2 fallback)")
    warning = rv.headers.get("Warning", "")
    ok &= assert_true("299" in warning, f"Warning: 299 emitted: {warning!r}")
    body = rv.get_json()
    ok &= assert_true("RestReference:nameWarning" in body,
                       "body carries nameWarning")
    if "RestReference:nameWarning" in body:
        nw = body["RestReference:nameWarning"]
        ok &= assert_eq(nw["served_via"], "item",
                         "served_via reports item (path failed)")
        ok &= assert_eq(nw["current_canonical_name"], "mta_lirr_stations_v2",
                         "nameWarning advertises the new canonical name")
        ok &= assert_eq(nw["current_item"], item,
                         "nameWarning carries refreshed item")

    # Reset to default for subsequent tests
    frf_registry._global_registry = None
    return ok


def test_tombstone_returns_410():
    """Tombstoned item → 410 Gone."""
    print("\n[H4] tombstone: item lookup → 410 Gone")
    setup()
    client = app.test_client()

    rv = client.get("/collections/stations")
    body = rv.get_json()
    item = body["item"]
    uid = body["uuid"]

    idreg = frf_registry.get_registry()
    idreg.delete(item, when="2026-01-01T00:00:00Z")

    rv = client.get(f"/collections/anything?item={item}")
    ok = assert_eq(rv.status_code, 410, "tombstoned item → 410")
    rv = client.get(f"/collections/anything?uuid={uid}")
    ok &= assert_eq(rv.status_code, 410, "tombstoned uuid → 410")
    return ok


def test_uuid_only_resolution_via_step_3():
    """Brief §2 step 3: uuid resolves when name and item both fail."""
    print("\n[H5] step 3: uuid-only resolution via global UUID")
    setup()
    client = app.test_client()

    rv = client.get("/collections/stations")
    body = rv.get_json()
    uid = body["uuid"]

    # Use a name that doesn't exist and isn't in history; only uuid should resolve
    rv = client.get(f"/collections/totally_unknown?uuid={uid}")
    ok = assert_eq(rv.status_code, 200, "uuid-only resolution → 200")
    body = rv.get_json()
    ok &= assert_true("RestReference:nameWarning" in body,
                       "uuid-only response carries nameWarning")
    if "RestReference:nameWarning" in body:
        nw = body["RestReference:nameWarning"]
        ok &= assert_eq(nw["served_via"], "uuid",
                         "served_via reports uuid (path and item failed)")
    return ok


def test_total_failure_returns_404():
    """Step 4: nothing resolves → 404."""
    print("\n[H6] step 4: nothing resolves → 404")
    setup()
    client = app.test_client()

    fake_uuid_a = "00000000-0000-0000-0000-000000000000"
    fake_uuid_b = "11111111-1111-1111-1111-111111111111"
    rv = client.get(f"/collections/ghost?item={fake_uuid_a}&uuid={fake_uuid_b}")
    ok = assert_eq(rv.status_code, 404, "no resolution → 404")
    return ok


def test_conformance_declares_stable_ids():
    print("\n[H7] conformance declares stable-ids class")
    setup()
    client = app.test_client()
    rv = client.get("/conformance")
    classes = rv.get_json()["conformsTo"]
    ok = assert_true(any("stable-ids" in c for c in classes),
                      "stable-ids class declared")
    ok &= assert_true(any("ogcapi-features-1" in c for c in classes),
                       "OGC core class still declared")
    return ok


def main():
    tests = [
        test_path_resolves_normally,
        test_legacy_client_after_rename_gets_308,
        test_optin_client_with_history_disabled_gets_drift,
        test_tombstone_returns_410,
        test_uuid_only_resolution_via_step_3,
        test_total_failure_returns_404,
        test_conformance_declares_stable_ids,
    ]
    results = [t() for t in tests]
    passed = sum(1 for r in results if r)
    print(f"\n{'='*60}")
    print(f"  {passed}/{len(tests)} tests passed")
    print(f"{'='*60}")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
