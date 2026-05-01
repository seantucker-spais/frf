"""Integration tests — end-to-end through Flask, exercising the wire-level
behavior described in the technical brief §4.

Run with: pytest tests/test_integration.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import frf_registry
from frf_http import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with a fresh registry per test."""
    # Point the registry at a per-test file
    p = tmp_path / "_registry.yaml"
    monkeypatch.setattr(frf_registry, "REGISTRY_PATH", p)
    frf_registry.reset_registry_for_tests()
    c = app.test_client()
    # Trigger discovery so registry populates
    c.get("/registry")
    return c


def test_collection_carries_dual_keys(client):
    """Every collection descriptor carries item + uuid."""
    rv = client.get("/collections/stations")
    body = rv.get_json()
    assert "item" in body
    assert "uuid" in body
    assert isinstance(body["item"], int)
    assert len(body["uuid"]) == 36


def test_happy_path_no_warnings(client):
    """Valid name, no backup → 200, no warning."""
    rv = client.get("/collections/stations")
    assert rv.status_code == 200
    assert rv.headers.get("Warning") is None
    assert rv.headers.get("Link") is None
    assert "RestReference:nameWarning" not in rv.get_json()


def test_headline_scenario(client):
    """The brief's §4 wire example — bookmark survives rename."""
    # Bookmark
    rv = client.get("/collections/stations")
    item = rv.get_json()["item"]

    # Operator renames
    idreg = frf_registry.get_registry()
    idreg.rename(item, "mta_lirr_stations")

    # Stale bookmark + backup item resolves
    rv = client.get(f"/collections/stations?item={item}")
    assert rv.status_code == 200

    # Headers (the brief's required signals)
    link = rv.headers.get("Link", "")
    assert 'rel="canonical"' in link
    assert "mta_lirr_stations" in link

    warning = rv.headers.get("Warning", "")
    assert "299" in warning

    # Body field
    body = rv.get_json()
    assert "RestReference:nameWarning" in body
    nw = body["RestReference:nameWarning"]
    assert nw["current_canonical_name"] == "mta_lirr_stations"
    assert nw["current_item"] == item
    assert nw["served_via"] == "item"


def test_tombstoned_returns_410(client):
    """A deleted resource returns 410 Gone, not 404."""
    rv = client.get("/collections/zones")
    item = rv.get_json()["item"]

    idreg = frf_registry.get_registry()
    idreg.delete(item, when="2026-01-01T00:00:00Z")

    rv = client.get(f"/collections/anything?item={item}")
    assert rv.status_code == 410


def test_truly_unknown_returns_404(client):
    """An identifier that never existed returns 404, not 410."""
    rv = client.get("/collections/whatever?item=99999")
    assert rv.status_code == 404


def test_uuid_only_resolution(client):
    """A UUID can resolve a request even when the path name is meaningless."""
    rv = client.get("/collections/stations")
    uid = rv.get_json()["uuid"]

    # Use an arbitrary path name
    rv = client.get(f"/collections/_anything_?uuid={uid}")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "RestReference:nameWarning" in body
    assert body["RestReference:nameWarning"]["current_canonical_name"] == "stations"


def test_conformance_declares_stable_ids(client):
    """The proposed conformance class is declared at /conformance."""
    rv = client.get("/conformance")
    classes = rv.get_json()["conformsTo"]
    assert any("stable-ids" in c for c in classes)
    assert any("ogcapi-features-1" in c for c in classes)


def test_registry_endpoint_lists_entries(client):
    """The /registry endpoint exposes the live identifier book."""
    rv = client.get("/registry")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "live" in data
    assert "tombstoned" in data
    assert len(data["live"]) > 0
    # Each entry has the dual keys
    e = data["live"][0]
    assert "item" in e
    assert "uuid" in e
    assert "name" in e
    assert "kind" in e
