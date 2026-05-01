# FRF — File-REST-Framework

[![Status](https://img.shields.io/badge/status-reference%20implementation-blue)]()
[![License](https://img.shields.io/badge/license-Apache--2.0-green)]()
[![OGC API – Features Part 1](https://img.shields.io/badge/OGC%20API--Features-Part%201%3A%20Core-orange)]()
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19954488.svg)](https://doi.org/10.5281/zenodo.19954488)

A reference implementation of OGC API – Features Part 1 with a proposed
**stable-ids** conformance class implementing dual-key (item + uuid)
identity and name drift signaling.

> If you arrived here from the technical brief, jump to
> [**Stable Identifiers and Name Drift**](#stable-identifiers-and-name-drift) below.

---

## Stable Identifiers and Name Drift

This repository is the reference implementation for:

> *Tucker, S. (2026). Stable Identifiers and Name Drift Signaling for
> OGC API – Features. A Practitioner Technical Brief, v1.0.*
>
> <!-- TODO: replace with actual Zenodo DOI once published -->
> Zenodo DOI: `10.5281/zenodo.19954488`

The brief proposes a small, additive conformance class that gives every
named resource two server-issued durable identifiers (`item` and `uuid`)
accepted as backup query parameters, and signals name drift via
`Link: rel="canonical"` and a `nameWarning` body field when a stale name
resolves through a backup identifier.

**The dual-key resolver — the algorithmic core of the proposal — is in
[`frf_registry.py`](frf_registry.py).** See
[`docs/STABLE_IDS.md`](docs/STABLE_IDS.md) for the implementation walkthrough
mapped to the brief's requirement IDs.

### The wire example from §4 of the brief

```bash
# A bookmark from three years ago, when the collection was named
# 'lirr_station_master'. The collection has since been renamed to 'stations'.
curl -i "http://localhost:5000/collections/lirr_station_master?item=42"

HTTP/1.1 200 OK
Content-Type: application/geo+json
Link: </collections/stations>; rel="canonical"
Warning: 299 - "requested name 'lirr_station_master' resolved via item to current name 'stations'"

{
  "type": "FeatureCollection",
  "features": [...],
  "nameWarning": {
    "requested_name": "lirr_station_master",
    "served_via": "item",
    "current_canonical_name": "stations",
    "current_item": 42,
    "current_uuid": "0a4f3b21-bc28-4d11-8e7e-9c1a3d8f4b22"
  }
}
```

### Verify the claims in the brief in 60 seconds

```bash
# TODO: update repository URL once published
git clone https://github.com/seantucker-spais/frf
cd frf
pip install -r requirements.txt
python -m pytest tests/test_registry.py -v          # 12 tests
python -m pytest tests/test_integration.py -v       # 8 tests
python examples/wire_example_demo.py                # reproduces §4 wire example
```

What the tests demonstrate, mapped to the brief:

| Brief claim | Test |
|---|---|
| dual-key resolution | `test_resolve_via_item_after_rename`, `test_resolve_disagreement_number_wins` |
| tombstone retention across restarts | `test_persistence`, `test_tombstone_returns_gone` |
| alias support | `test_rename_and_aliases` |
| source/view sharing pattern | `test_share_item` |
| uuid > item > name precedence | `test_resolve_uuid_wins_over_item` |
| `Link: rel="canonical"` + `nameWarning` body | `test_headline_scenario` (integration) |
| 410 Gone for tombstoned identifiers | `test_tombstone_returns_gone` (integration) |
| Conformance declaration | `test_conformance_declares_stable_ids` |

---

## What FRF is, more broadly

FRF is a CSV-driven OGC API – Features service with three layers:

```
sources/*.csv  →  ontological views  →  HTTP/MCP front doors
```

- **Sources.** Drop CSVs into `sources/`. The engine auto-detects geometry
  (lon/lat, x/y, or WKT columns), schema, and types.
- **Views.** Optional YAML files in `views/` define ontological views over
  sources, classified per the EES-A-R framework (Entity, Event, State,
  Artifact, Relation), with relations, constraints, and provenance trails.
- **Two front doors.** Both backed by the same engine:
  - **HTTP** (`frf_http.py`): OGC API – Features Part 1 Core, GeoJSON,
    OpenAPI 3.0, plus the proposed stable-ids extension.
  - **MCP** (`frf_mcp.py --serve`): an LLM-callable tool for
    conversational data access.

The stable-ids work is independently useful and is the focus of the
publishable brief; the EES-A-R ontology layer and MCP integration are
related but separate contributions covered elsewhere.

## Quick start

```bash
pip install -r requirements.txt
python frf_http.py --port 5000
```

Then:

```bash
curl http://localhost:5000/                    # landing page
curl http://localhost:5000/conformance         # conformance classes
curl http://localhost:5000/collections         # all collections (sources + views)
curl http://localhost:5000/registry            # the identity registry
```

## Architecture

```
                   ┌──────────────────────────────┐
                   │      sources/*.csv           │
                   └──────────────┬───────────────┘
                                  │
                   ┌──────────────▼───────────────┐
                   │   frf_mcp.engine             │
                   │   (discovery, parsing, I/O)  │
                   └──────────────┬───────────────┘
                                  │
                   ┌──────────────▼───────────────┐
                   │   frf_ont (views, EES-A-R)   │
                   └──────────────┬───────────────┘
                                  │
                   ┌──────────────▼───────────────┐
                   │   frf_registry               │
                   │   (stable IDs, drift)        │  ◄── the brief
                   └─────┬──────────────────┬─────┘
                         │                  │
              ┌──────────▼─────┐  ┌─────────▼──────────┐
              │   MCP server   │  │   HTTP server      │
              │   (frf_mcp.py) │  │   (frf_http.py)    │
              └────────┬───────┘  └─────────┬──────────┘
                       │                    │
                  Claude / LLMs       QGIS, ArcGIS, browsers
```

## Repository layout

```
frf/
├── README.md                     this file
├── LICENSE                       Apache-2.0
├── requirements.txt
├── frf_registry.py               ★ the stable-ids reference implementation
├── frf_mcp.py                    engine + MCP server
├── frf_ont.py                    EES-A-R ontology engine
├── frf_http.py                   Flask + OGC API server
├── docs/
│   ├── STABLE_IDS.md             walkthrough of frf_registry.py
│   ├── CONFORMANCE.md            mapping to the proposed conformance class
│   └── ARCHITECTURE.md           the full FRF stack
├── examples/
│   ├── wire_example_demo.py      reproduces §4 of the brief
│   ├── rename_survives_demo.py   live rename + bookmark survival
│   └── tombstone_demo.py         deletion + 410 Gone behavior
├── tests/
│   ├── test_registry.py          unit tests for frf_registry.py
│   ├── test_integration.py       end-to-end through Flask
│   └── test_ontology.py          ontology layer (separate concern)
├── sources/                      sample CSVs
│   ├── stations.csv              LIRR stations (self-describing)
│   ├── parcels.csv
│   ├── bridges.csv
│   ├── zones.csv
│   └── inspections.csv
└── views/                        YAML view definitions
    ├── stations.yaml
    ├── branches.yaml
    └── station_provenance.yaml
```

## Citing this work

If you cite the brief, please also cite this implementation. Update the
DOI and URL fields with the actual values once published.

```bibtex
@misc{tucker2026stableids,
  author       = {Tucker, Sean},
  title        = {Stable Identifiers and Name Drift Signaling for OGC API – Features:
                  A Practitioner Technical Brief},
  year         = {2026},
  doi          = {10.5281/zenodo.19954488},
  url          = {https://zenodo.org/records/19954488}
}

@software{tucker2026frf,
  author       = {Tucker, Sean},
  title        = {FRF: File-REST-Framework — Reference Implementation of
                  Stable Identifiers and Name Drift for OGC API – Features},
  year         = {2026},
  url          = {https://github.com/seantucker-spais/frf}
}
```

## Status

- **stable-ids reference implementation** — complete, tested, ready for review
- **OGC SWG discussion issue** — to be filed against
  [opengeospatial/ogcapi-features](https://github.com/opengeospatial/ogcapi-features)
  referencing Issue #139
- **EES-A-R ontology layer** — present but separate from the stable-ids contribution

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contact

Sean Tucker
University at Buffalo, MS in Applied Ontology
