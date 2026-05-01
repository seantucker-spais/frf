#!/usr/bin/env python3
"""
Demonstrates tombstone behavior — deleted resources return 410 Gone with
deletion metadata, and their identifiers are never reissued.

The 410 vs 404 distinction matters: 410 is a permanent answer ("retire
the bookmark"), 404 is conditional ("maybe later"). For caches, mirrors,
and downstream consumers, this is the difference between clean garbage
collection and indefinite uncertainty.

Run:
    python examples/tombstone_demo.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="frf_tombstone_demo_"))
SOURCES = SANDBOX / "sources"
VIEWS = SANDBOX / "views"
SOURCES.mkdir()
VIEWS.mkdir()

project_root = Path(__file__).parent.parent
shutil.copy(project_root / "sources" / "zones.csv", SOURCES / "zones.csv")
sys.path.insert(0, str(project_root))

import frf_mcp
frf_mcp.SOURCES_DIR = SOURCES
import frf_ont
frf_ont.VIEWS_DIR = VIEWS
import frf_registry
frf_registry.REGISTRY_PATH = VIEWS / "_registry.yaml"
frf_registry.reset_registry_for_tests()

from frf_http import app


def main():
    client = app.test_client()
    client.get("/registry")

    idreg = frf_registry.get_registry()
    e = idreg.lookup_by_name("zones")
    item, uid = e.item, e.uuid
    print(f"Target: name='zones', item={item}, uuid={uid[:8]}...")

    print("\n--- Operator deletes the resource ---")
    idreg.delete(item, when="2026-05-01T09:00:00Z")
    print(f"Deleted at: 2026-05-01T09:00:00Z")

    # 1. Item lookup → 410 Gone
    r = client.get(f"/collections/zones?item={item}")
    print(f"\nGET /collections/zones?item={item}")
    print(f"  HTTP {r.status_code}")
    print(f"  Body: {json.dumps(r.get_json(), indent=4)}")

    # 2. UUID lookup → also 410 Gone
    r = client.get(f"/collections/anything?uuid={uid}")
    print(f"\nGET /collections/anything?uuid={uid[:8]}...")
    print(f"  HTTP {r.status_code} ← same answer via uuid")

    # 3. Truly unknown identifier → 404 Not Found (NOT 410)
    r = client.get("/collections/whatever?item=99999")
    print(f"\nGET /collections/whatever?item=99999")
    print(f"  HTTP {r.status_code} ← 404, never existed")

    # 4. Freed name CAN be reused, but with NEW item and uuid
    print("\n--- The name 'zones' is now free for reuse ---")
    new_e = idreg.register("zones", kind="source")
    print(f"new 'zones' has item={new_e.item} (was {item}, never reissued)")
    print(f"new uuid={new_e.uuid[:8]}... (was {uid[:8]}..., never reissued)")

    # 5. Old identifiers stay tombstoned
    r = client.get(f"/collections/anything?item={item}")
    print(f"\nGET /collections/anything?item={item}")
    print(f"  HTTP {r.status_code} ← still 410, the old item stays dead")
    print()
    print("This is the durability guarantee: a stale bookmark for a deleted")
    print("resource gets a definitive permanent 'no', never a confusing 'yes'.")

    shutil.rmtree(SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    main()
