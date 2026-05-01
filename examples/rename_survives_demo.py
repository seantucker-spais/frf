#!/usr/bin/env python3
"""
Demonstrates the single most important property: a bookmark survives a
rename when the client included the backup `item` query parameter.

This is the failure mode the brief is solving — collections renamed in
production silently 404 every external integration. With the proposed
extension, none of that breaks.

Run:
    python examples/rename_survives_demo.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

# Sandbox setup before FRF imports
SANDBOX = Path(tempfile.mkdtemp(prefix="frf_rename_demo_"))
SOURCES = SANDBOX / "sources"
VIEWS = SANDBOX / "views"
SOURCES.mkdir()
VIEWS.mkdir()

project_root = Path(__file__).parent.parent
shutil.copy(project_root / "sources" / "parcels.csv", SOURCES / "parcels.csv")
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
    client.get("/registry")  # warm up

    # Capture a bookmark — what a client saved 3 years ago
    r = client.get("/collections/parcels")
    bookmark = {
        "url": "/collections/parcels",
        "item": r.get_json()["item"],
        "uuid": r.get_json()["uuid"],
    }
    print(f"Bookmark from 2023: name=parcels, item={bookmark['item']}, "
           f"uuid={bookmark['uuid'][:8]}...")

    # Operator renames
    idreg = frf_registry.get_registry()
    idreg.rename(bookmark["item"], "tax_lots")
    print("Operator renamed:    parcels -> tax_lots")

    # Bookmark with backup parameter — resource served, drift signaled
    r2 = client.get(f'{bookmark["url"]}?item={bookmark["item"]}')
    print(f"\nBookmark + item:    HTTP {r2.status_code}  ← self-healing reference")

    body = r2.get_json()
    nw = body["RestReference:nameWarning"]
    print(f"\nServer told the client:")
    print(f"  requested:  {nw['requested_name']}")
    print(f"  served via: {nw['served_via']}")
    print(f"  new name:   {nw['current_canonical_name']}")
    print()
    print(f"The client can update its cache from 'parcels' to "
          f"'{nw['current_canonical_name']}' on its own.")
    print("Zero downtime. Zero phone calls. Zero crosswalk tables.")

    shutil.rmtree(SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    main()
