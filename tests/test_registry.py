"""Unit tests for frf_registry.Registry — dual-key resolver, drift, tombstoning.

Run with: pytest tests/test_registry.py -v
"""

import sys
from pathlib import Path

import pytest

# Make project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import frf_registry
from frf_registry import Registry, TOMBSTONED


@pytest.fixture
def reg(tmp_path):
    """Fresh in-memory registry backed by a unique temp file per test."""
    return Registry(path=tmp_path / "test_registry.yaml")


# ---------- registration ----------

def test_basic_register(reg):
    e = reg.register("lirr_stations", kind="source")
    assert e.item == 1
    assert len(e.uuid) == 36 and e.uuid.count("-") == 4
    assert e.name == "lirr_stations"

    e2 = reg.register("parcels", kind="source")
    assert e2.item == 2
    assert e.uuid != e2.uuid


def test_share_item(reg):
    """A Core view shares its source's item but gets its own uuid."""
    src = reg.register("lirr_stations", kind="source")
    core = reg.register("lirr_stations_core_view", kind="view",
                         share_item_with=src.item)
    assert core.item == src.item
    assert core.uuid != src.uuid


def test_collision_rejected(reg):
    reg.register("foo", kind="view")
    with pytest.raises(ValueError):
        reg.register("foo", kind="view")


# ---------- mutation ----------

def test_rename_and_aliases(reg):
    e = reg.register("old_name", kind="view")
    reg.rename(e.item, "new_name")
    assert reg.lookup_by_name("new_name").item == e.item
    assert reg.lookup_by_name("old_name") is None

    reg.add_alias(e.item, "old_name")
    assert reg.lookup_by_name("old_name").item == e.item


# ---------- the resolver ----------

def test_resolve_happy_path(reg):
    """Name only, no drift."""
    e = reg.register("lirr_stations", kind="source")
    res = reg.resolve(name="lirr_stations")
    assert res.entry is not None
    assert res.entry.uuid == e.uuid
    assert not res.has_drift


def test_resolve_via_item_after_rename(reg):
    """Headline scenario: bookmark survives a rename."""
    e = reg.register("lirr_stations", kind="source")
    original_item = e.item
    reg.rename(e.item, "mta_lirr_stations")

    # client still has the old name AND the original item
    res = reg.resolve(name="lirr_stations", item=original_item)
    assert res.entry is not None
    assert res.entry.name == "mta_lirr_stations"
    assert res.has_drift
    assert "name" in res.drift


def test_resolve_disagreement_number_wins(reg):
    """When name and item resolve to different entries, item wins."""
    a = reg.register("alpha", kind="view")
    b = reg.register("beta", kind="view")
    res = reg.resolve(name="alpha", item=b.item)
    assert res.entry.uuid == b.uuid
    assert res.has_drift


def test_resolve_uuid_wins_over_item(reg):
    """uuid > item when both supplied and disagree."""
    a = reg.register("alpha", kind="view")
    b = reg.register("beta", kind="view")
    res = reg.resolve(item=a.item, uid=b.uuid)
    assert res.entry.uuid == b.uuid


# ---------- tombstones ----------

def test_tombstone_returns_gone(reg):
    """Tombstoned identifiers signal 'gone', not 'not found'."""
    e = reg.register("doomed", kind="view")
    reg.delete(e.item, when="2026-01-01T00:00:00Z")

    # The name doesn't resolve (not tombstoned, just unmapped)
    res = reg.resolve(name="doomed")
    assert res.entry is None
    assert not res.tombstoned

    # The item DOES tombstone
    res2 = reg.resolve(item=e.item)
    assert res2.tombstoned

    # The uuid DOES tombstone
    res3 = reg.resolve(uid=e.uuid)
    assert res3.tombstoned


def test_tombstoned_identifiers_never_reissued(reg):
    """Names can be reused after tombstone; items and uuids cannot."""
    e = reg.register("doomed", kind="view")
    old_item, old_uuid = e.item, e.uuid
    reg.delete(e.item, when="2026-01-01T00:00:00Z")

    e2 = reg.register("doomed", kind="view")    # same name OK
    assert e2.item != old_item                   # item NOT reissued
    assert e2.uuid != old_uuid                   # uuid NOT reissued


# ---------- persistence ----------

def test_persistence(tmp_path):
    """Registry survives reload from disk."""
    p = tmp_path / "persist.yaml"
    r1 = Registry(path=p)
    e = r1.register("survives", kind="source")
    item, uid = e.item, e.uuid

    r2 = Registry(path=p)
    res = r2.resolve(name="survives")
    assert res.entry.item == item
    assert res.entry.uuid == uid


# ---------- warning message ----------

def test_warning_message_is_informative(reg):
    e = reg.register("lirr_stations", kind="source")
    reg.rename(e.item, "mta_lirr_stations")
    res = reg.resolve(name="lirr_stations", item=e.item)
    msg = res.warning_message()
    assert "lirr_stations" in msg
    assert "mta_lirr_stations" in msg
