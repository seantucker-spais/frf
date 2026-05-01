#!/usr/bin/env python3
"""
Reproduces §4 (Wire Example) of the technical brief from a cold start.

This demo creates an isolated sandbox containing only one CSV, so the
rename behavior is unambiguous and reproducible. It does NOT modify
your project's sources/ or views/ directories.

Run:
    python examples/wire_example_demo.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

# Set up isolated sandbox BEFORE any FRF imports so module-level path
# constants pick up the sandbox.
SANDBOX = Path(tempfile.mkdtemp(prefix="frf_wire_demo_"))
SOURCES = SANDBOX / "sources"
VIEWS = SANDBOX / "views"
SOURCES.mkdir()
VIEWS.mkdir()

project_root = Path(__file__).parent.parent
shutil.copy(project_root / "sources" / "stations.csv", SOURCES / "stations.csv")
sys.path.insert(0, str(project_root))

import frf_mcp
frf_mcp.SOURCES_DIR = SOURCES
import frf_ont
frf_ont.VIEWS_DIR = VIEWS
import frf_registry
frf_registry.REGISTRY_PATH = VIEWS / "_registry.yaml"
frf_registry.reset_registry_for_tests()

from frf_http import app


def banner(text):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def main():
    client = app.test_client()

    banner("STEP 1 — Cold start: discover and issue stable IDs")
    r = client.get("/registry")
    data = r.get_json()
    for e in data["live"]:
        print(f"  item={e['item']:<3} {e['kind']:<7} {e['name']:<25} "
              f"uuid={e['uuid'][:8]}...")

    banner("STEP 2 — A client bookmarks /collections/stations")
    r = client.get("/collections/stations")
    d = r.get_json()
    saved_name, saved_item, saved_uuid = d["id"], d["item"], d["uuid"]
    print(f"  bookmark = {{name={saved_name!r}, item={saved_item}, "
           f"uuid={saved_uuid[:8]}...}}")

    banner("STEP 3 — Operator renames the collection")
    idreg = frf_registry.get_registry()
    idreg.rename(saved_item, "lirr_station_master")
    print(f"  stations -> lirr_station_master (item {saved_item} preserved)")

    banner("STEP 4 — Stale bookmark + backup item resolves with drift signal")
    print(f"  GET /collections/{saved_name}?item={saved_item}")
    r = client.get(f"/collections/{saved_name}?item={saved_item}")
    print(f"\n  HTTP {r.status_code}")
    print(f"  Link:    {r.headers.get('Link', '(none)')}")
    print(f"  Warning: {r.headers.get('Warning', '(none)')}")
    body = r.get_json()
    print(f"\n  Body (excerpt):")
    print(json.dumps({
        "id": body.get("id"),
        "item": body.get("item"),
        "uuid": body.get("uuid"),
        "RestReference:nameWarning": body.get("RestReference:nameWarning"),
    }, indent=2))

    banner("STEP 5 — UUID-only resolution (durable across instances)")
    r = client.get(f"/collections/_anything_?uuid={saved_uuid}")
    nw = r.get_json().get("RestReference:nameWarning", {})
    print(f"  HTTP {r.status_code}, resolved to "
           f"{nw.get('current_canonical_name')!r} via {nw.get('served_via')}")

    banner("STEP 6 — Tombstone: delete + 410 Gone")
    idreg.delete(saved_item, when="2026-05-01T12:00:00Z")
    r = client.get(f"/collections/whatever?item={saved_item}")
    print(f"  GET /collections/whatever?item={saved_item} -> "
           f"HTTP {r.status_code}")

    banner("STEP 7 — Tombstoned identifier never reissued")
    new_e = idreg.register("brand_new_thing", kind="view")
    print(f"  newly registered 'brand_new_thing' got item={new_e.item}")
    print(f"  the dead item {saved_item} stays dead forever")

    print("\n" + "=" * 70)
    print("  Demo complete. Reproduces §4 of the technical brief.")
    print("=" * 70 + "\n")
    shutil.rmtree(SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    main()
