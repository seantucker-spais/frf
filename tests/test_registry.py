#!/usr/bin/env python3
"""Test suite for frf_registry.Registry — brief v1.3 protocol.

Covers:
  - Registration (item is server-instance UUID; uuid is global UUID)
  - Rename (records name-history)
  - Resolve cascade: name → item → uuid (natural-first per §2)
  - Optional name-history fallback (§2.4) returning 308 redirect indicator
  - Tombstone behavior (410 Gone)
  - Backward-compatibility scenarios from §2.2
  - Edge cases from §2.3
  - Persistence
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

# Use a sandboxed registry path so tests don't collide with the real file.
_TMPDIR = Path(tempfile.mkdtemp(prefix="frf_reg_test_"))
import frf_registry
frf_registry.REGISTRY_PATH = _TMPDIR / "_registry.yaml"
frf_registry._global_registry = None  # reset
from frf_registry import Registry, get_registry, TOMBSTONED


def fresh(name_history_enabled: bool = True,
          history_retention_seconds: int | None = None) -> Registry:
    """Fresh in-memory registry backed by a unique temp file."""
    p = Path(tempfile.mktemp(suffix=".yaml", dir=str(_TMPDIR)))
    return Registry(path=p,
                    name_history_enabled=name_history_enabled,
                    history_retention_seconds=history_retention_seconds)


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


def is_uuid_shaped(s) -> bool:
    return isinstance(s, str) and len(s) == 36 and s.count("-") == 4


# ---------- registration ----------

def test_basic_register():
    """Both item and uuid are UUIDs (RFC 4122) per brief v1.3 §2."""
    print("\n[R1] basic registration — item and uuid are both UUIDs")
    r = fresh()
    e = r.register("lirr_stations", kind="source")
    ok = assert_true(is_uuid_shaped(e.item), "item is UUID-shaped")
    ok &= assert_true(is_uuid_shaped(e.uuid), "uuid is UUID-shaped")
    ok &= assert_true(e.item != e.uuid, "item and uuid are different identifiers")
    ok &= assert_eq(e.name, "lirr_stations", "canonical name preserved")
    e2 = r.register("parcels", kind="source")
    ok &= assert_true(e.item != e2.item, "items are unique per resource")
    ok &= assert_true(e.uuid != e2.uuid, "uuids are unique per resource")
    return ok


def test_share_item():
    """Source and Core view share an item (server-instance UUID)."""
    print("\n[R2] source and core view share item, not uuid")
    r = fresh()
    src = r.register("lirr_stations", kind="source")
    core = r.register("lirr_stations_core_view", kind="view",
                       share_item_with=src.item)
    ok = assert_eq(core.item, src.item, "core view shares item with source")
    ok &= assert_true(core.uuid != src.uuid, "core view has its own uuid")
    return ok


def test_rename_records_history():
    """Rename records a name-history entry mapping the old name to the entry."""
    print("\n[R3] rename + name-history")
    r = fresh()
    e = r.register("old_name", kind="view")
    r.rename(e.item, "new_name")
    ok = assert_eq(r.lookup_by_name("new_name").item, e.item,
                    "new name resolves to entry")
    ok &= assert_true(r.lookup_by_name("old_name") is None,
                       "old name no longer resolves via live index")
    h = r.lookup_name_history("old_name")
    ok &= assert_true(h is not None, "name-history entry exists for old name")
    ok &= assert_eq(h.canonical_item, e.item,
                     "history points to the canonical item")
    return ok


def test_collision_rejected():
    print("\n[R4] name collisions rejected")
    r = fresh()
    r.register("foo", kind="view")
    try:
        r.register("foo", kind="view")
        ok = assert_true(False, "duplicate name should raise")
    except ValueError:
        ok = assert_true(True, "duplicate name raises ValueError")
    return ok


# ---------- resolution cascade (§2 of brief v1.3) ----------

def test_resolve_step1_name_succeeds():
    """Step 1: name resolves → serve, no drift signal."""
    print("\n[R5] step 1: name resolves cleanly, no drift")
    r = fresh()
    e = r.register("lirr_stations", kind="source")
    res = r.resolve(name="lirr_stations")
    ok = assert_true(res.entry is not None, "resolved")
    ok &= assert_eq(res.entry.uuid, e.uuid, "right entry")
    ok &= assert_eq(res.served_via, "name", "served via name")
    ok &= assert_true(not res.has_drift, "no drift signal")
    ok &= assert_true(not res.is_redirect, "no redirect")
    return ok


def test_resolve_step2_item_resolves_after_rename():
    """The headline scenario: client cached old name + item; name fails;
    item resolves; drift signaled."""
    print("\n[R6] step 2: bookmark survives rename via item backup")
    # Disable history so we exercise the item fallback, not the 308 path
    r = fresh(name_history_enabled=False)
    e = r.register("lirr_stations", kind="source")
    original_item = e.item
    r.rename(e.item, "mta_lirr_stations")
    res = r.resolve(name="lirr_stations", item=original_item)
    ok = assert_true(res.entry is not None, "resolved via item")
    ok &= assert_eq(res.entry.name, "mta_lirr_stations",
                     "served the renamed entry")
    ok &= assert_eq(res.served_via, "item", "served_via reports item")
    ok &= assert_eq(res.drift_from, "lirr_stations",
                     "drift_from reports the requested name")
    ok &= assert_true(res.has_drift, "drift signaled")
    return ok


def test_resolve_step3_uuid_resolves_after_item_fail():
    """Step 3: name and item both fail (e.g., post-migration), uuid resolves."""
    print("\n[R7] step 3: uuid resolves when name and item both fail")
    r = fresh(name_history_enabled=False)
    e = r.register("res", kind="view")
    saved_uuid = e.uuid
    fake_item = "00000000-0000-0000-0000-000000000000"  # nonexistent
    res = r.resolve(name="missing", item=fake_item, uid=saved_uuid)
    ok = assert_true(res.entry is not None, "resolved via uuid")
    ok &= assert_eq(res.entry.uuid, saved_uuid, "right entry")
    ok &= assert_eq(res.served_via, "uuid", "served_via reports uuid")
    ok &= assert_true(res.has_drift, "drift signaled")
    return ok


def test_resolve_step4_all_fail_returns_404():
    """Step 4: nothing resolves → 404 (entry is None, not tombstoned)."""
    print("\n[R8] step 4: total failure returns 404")
    r = fresh(name_history_enabled=False)
    res = r.resolve(name="ghost",
                     item="00000000-0000-0000-0000-000000000000",
                     uid="11111111-1111-1111-1111-111111111111")
    ok = assert_true(res.entry is None, "entry is None")
    ok &= assert_true(not res.tombstoned, "not tombstoned")
    ok &= assert_true(not res.is_redirect, "not redirect")
    return ok


def test_natural_first_no_short_circuit_on_disagreement():
    """When name resolves to entry A but item points to entry B,
    name wins (natural-first). The brief's §2.3 'name reuse' edge case:
    server cannot detect this; client must verify via uuid match."""
    print("\n[R9] natural-first: name wins when path resolves "
            "(name reuse edge case)")
    r = fresh(name_history_enabled=False)
    a = r.register("alpha", kind="view")
    b = r.register("beta", kind="view")
    # Client passes name="alpha" but item=beta's item — they disagree
    res = r.resolve(name="alpha", item=b.item)
    ok = assert_eq(res.entry.uuid, a.uuid,
                    "served alpha by name (natural-first)")
    ok &= assert_eq(res.served_via, "name", "served_via is name (no fallback)")
    ok &= assert_true(not res.has_drift,
                       "no server-side drift signal for path-resolved disagreement")
    print("  NOTE: this is the §2.3 silent-reassignment case;")
    print("        client-side uuid verification (§2.5) is required to detect.")
    return ok


# ---------- name-history fallback (§2.4) ----------

def test_name_history_returns_redirect():
    """When name lookup fails and history exists, server should return
    308 redirect indicator (no entry served, redirect_to_name set)."""
    print("\n[R10] §2.4: stale name with history → 308 redirect")
    r = fresh(name_history_enabled=True)
    e = r.register("lirr_stations", kind="source")
    r.rename(e.item, "stations")
    # Legacy client: only the old name, no backups
    res = r.resolve(name="lirr_stations")
    ok = assert_true(res.is_redirect, "result indicates redirect")
    ok &= assert_eq(res.redirect_to_name, "stations",
                     "redirect points to canonical name")
    ok &= assert_eq(res.served_via, "name_history",
                     "served_via reports name_history")
    ok &= assert_true(res.entry is not None,
                       "canonical entry attached to redirect")
    return ok


def test_name_history_disabled_returns_404():
    """With history disabled, stale-name + no-backup behaves like §2 alone."""
    print("\n[R11] §2.4: history disabled → legacy 404 behavior")
    r = fresh(name_history_enabled=False)
    e = r.register("lirr_stations", kind="source")
    r.rename(e.item, "stations")
    res = r.resolve(name="lirr_stations")
    ok = assert_true(res.entry is None, "no entry resolved")
    ok &= assert_true(not res.is_redirect, "no redirect (history disabled)")
    return ok


def test_name_history_with_backups_still_redirects():
    """§2.4 'Interaction with opt-in clients': when both history and
    backups would resolve, the brief permits returning 308 (redirect)
    rather than 200+drift. Implementation chooses redirect for simplicity."""
    print("\n[R12] §2.4: history hit with backups present still redirects")
    r = fresh(name_history_enabled=True)
    e = r.register("lirr_stations", kind="source")
    r.rename(e.item, "stations")
    # Opt-in client: stale name AND backups
    res = r.resolve(name="lirr_stations", item=e.item)
    ok = assert_true(res.is_redirect,
                      "redirects to canonical even with backups present")
    return ok


def test_name_history_retention_window():
    """History entries past their retention window are not consulted."""
    print("\n[R13] §2.4: history retention window expires entries")
    # 1 second retention
    r = fresh(name_history_enabled=True, history_retention_seconds=1)
    e = r.register("ephemeral", kind="view")
    r.rename(e.item, "now_named_this")
    # immediately, history hit
    res = r.resolve(name="ephemeral")
    ok = assert_true(res.is_redirect, "history hit before expiry")
    # wait past the retention window
    time.sleep(1.5)
    res2 = r.resolve(name="ephemeral")
    ok &= assert_true(not res2.is_redirect, "history expired, no redirect")
    ok &= assert_true(res2.entry is None,
                       "expired history → 404 for non-opt-in client")
    return ok


# ---------- tombstone behavior ----------

def test_tombstone_returns_gone():
    """Tombstoned entries: name is reusable; item/uuid still resolve to
    tombstone marker → 410 Gone."""
    print("\n[R14] tombstone: name reusable, item/uuid → 410")
    r = fresh()
    e = r.register("doomed", kind="view")
    r.delete(e.item, when="2026-01-01T00:00:00Z")

    # Name lookup of the freed name → 404 (not tombstoned)
    res = r.resolve(name="doomed")
    ok = assert_true(res.entry is None, "name 'doomed' no longer resolves")
    ok &= assert_true(not res.tombstoned,
                       "freed name is not tombstoned (404, not 410)")

    # Item lookup → 410
    res2 = r.resolve(item=e.item)
    ok &= assert_true(res2.tombstoned, "item lookup → tombstone")

    # UUID lookup → 410
    res3 = r.resolve(uid=e.uuid)
    ok &= assert_true(res3.tombstoned, "uuid lookup → tombstone")

    # Re-registering the name produces fresh item and uuid
    e2 = r.register("doomed", kind="view")
    ok &= assert_true(e2.item != e.item,
                       "item NOT reissued after tombstone")
    ok &= assert_true(e2.uuid != e.uuid,
                       "uuid NOT reissued after tombstone")
    return ok


# ---------- §2.2 backward-compatibility scenarios ----------

def test_backcompat_non_optin_name_resolves():
    """§2.2 scenario: non-opt-in client, name resolves → identical to today."""
    print("\n[B1] §2.2: non-opt-in client, name resolves")
    r = fresh()
    r.register("alpha", kind="view")
    res = r.resolve(name="alpha")
    ok = assert_true(res.entry is not None, "resolved")
    ok &= assert_eq(res.served_via, "name", "no fallback")
    ok &= assert_true(not res.has_drift, "no drift signal")
    return ok


def test_backcompat_optin_name_resolves_no_drift():
    """§2.2 scenario: opt-in client, name resolves → identical to today;
    backups are not consulted."""
    print("\n[B2] §2.2: opt-in client, name resolves, backups ignored")
    r = fresh()
    e = r.register("alpha", kind="view")
    res = r.resolve(name="alpha", item=e.item, uid=e.uuid)
    ok = assert_true(res.entry is not None, "resolved")
    ok &= assert_eq(res.served_via, "name", "served via name")
    ok &= assert_true(not res.has_drift,
                       "no drift when name and backups all agree")
    return ok


def test_backcompat_partial_registry():
    """§2.2 scenario: partial implementation (resolver but empty registry)
    behaves like today — every lookup returns 404 cleanly."""
    print("\n[B3] §2.2: empty registry returns 404 like today")
    r = fresh()
    res = r.resolve(name="anything",
                     item="00000000-0000-0000-0000-000000000000",
                     uid="11111111-1111-1111-1111-111111111111")
    ok = assert_true(res.entry is None, "empty registry → 404")
    ok &= assert_true(not res.is_redirect, "no redirect")
    ok &= assert_true(not res.tombstoned, "not tombstoned")
    return ok


# ---------- persistence ----------

def test_persistence_includes_history():
    """Registry survives reload, including name-history entries."""
    print("\n[R15] persistence: entries and name-history both survive reload")
    p = _TMPDIR / "persist_test.yaml"
    r = Registry(path=p, name_history_enabled=True)
    e = r.register("survives", kind="source")
    item = e.item
    uid = e.uuid
    r.rename(item, "renamed")

    # New registry pointed at same file
    r2 = Registry(path=p, name_history_enabled=True)
    res = r2.resolve(name="renamed")
    ok = assert_eq(res.entry.item, item, "item survives reload")
    ok &= assert_eq(res.entry.uuid, uid, "uuid survives reload")
    # History should also have survived
    h = r2.lookup_name_history("survives")
    ok &= assert_true(h is not None,
                       "name-history entry survives reload")
    if h is not None:
        ok &= assert_eq(h.canonical_item, item,
                         "history points to correct item")
    return ok


# ---------- main ----------

def main():
    tests = [
        # registration
        test_basic_register,
        test_share_item,
        test_rename_records_history,
        test_collision_rejected,
        # resolution cascade
        test_resolve_step1_name_succeeds,
        test_resolve_step2_item_resolves_after_rename,
        test_resolve_step3_uuid_resolves_after_item_fail,
        test_resolve_step4_all_fail_returns_404,
        test_natural_first_no_short_circuit_on_disagreement,
        # name-history fallback (§2.4)
        test_name_history_returns_redirect,
        test_name_history_disabled_returns_404,
        test_name_history_with_backups_still_redirects,
        test_name_history_retention_window,
        # tombstones
        test_tombstone_returns_gone,
        # §2.2 backward-compatibility
        test_backcompat_non_optin_name_resolves,
        test_backcompat_optin_name_resolves_no_drift,
        test_backcompat_partial_registry,
        # persistence
        test_persistence_includes_history,
    ]
    results = [t() for t in tests]
    passed = sum(1 for r in results if r)
    print(f"\n{'='*60}")
    print(f"  {passed}/{len(tests)} test groups passed")
    print(f"{'='*60}")
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
