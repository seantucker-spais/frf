# FRF — File-REST-Framework

[![Status](https://img.shields.io/badge/status-reference%20implementation-blue)]()
[![License](https://img.shields.io/badge/license-Apache--2.0-green)]()
[![OGC API – Features Part 1](https://img.shields.io/badge/OGC%20API--Features-Part%201%3A%20Core-orange)]()
[![REST Brief DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19980026.svg)](https://doi.org/10.5281/zenodo.19980026)
[![OGC Brief DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19954488.svg)](https://doi.org/10.5281/zenodo.19954488)

A reference implementation of OGC API – Features Part 1 with a proposed
**stable-ids** conformance class implementing **name-first resolution**
with two server-issued UUID backups, optional name-history fallback via
**308 redirect**, and tombstone-aware **410 Gone** semantics.

> If you arrived here from one of the technical briefs, jump to
> [**Stable Identifiers and Name Drift**](#stable-identifiers-and-name-drift) below.

---

## Stable Identifiers and Name Drift

This repository is the reference implementation for two companion briefs:

> **REST-generic brief (primary):**
> Tucker, S. (2026). *Stable Identifiers and Name Drift Signaling for REST APIs:
> A Practitioner Technical Brief, v1.3.*
> Zenodo DOI: `10.5281/zenodo.19980026`
>
> **OGC-specific companion (v1.0; v1.3 forthcoming):**
> Tucker, S. (2026). *Stable Identifiers and Name Drift Signaling for OGC API – Features.*
> Zenodo DOI: `10.5281/zenodo.19954488`

The proposal is a small additive pattern: every named resource carries a
**triplet** of identifiers — a mutable `name` in the URI path, a server-instance
`item` UUID, and a global `uuid` — and the server resolves them in natural
order, signaling drift to the client whenever a backup carries the request.
Names change; identity persists.

The break is the trigger. The signal is the self-heal.

---

## Protocol at a glance

### The triplet

```
   Identifier   Scope                            Mutable?   Where it lives
   ──────────   ─────────────────────────        ────────   ──────────────────
   name         human-readable label             yes        URI path
   item         server-instance UUID             no         ?item=  query param
   uuid         global UUID (across federation)  no         &uuid=  query param
```

Both backups are RFC 4122 UUIDs. They differ in *scope*: `item` is durable
within one server (handles the common rename case); `uuid` is durable
across federated instances (handles migration and replication).

### The resolution cascade

Every successful path — including 308 redirects via name-history —
returns the **refreshed triplet** so the client can update its cache
and self-heal. The break is the trigger; the triplet is the self-heal.

```
                Client request
            GET /resources/old-name
            ?item=8c3a…&uuid=0a4f…
                      │
                      ▼
        ┌─────────────────────────────┐
        │ (1) try the path name       │
        └────────────┬────────────────┘
                ┌────┴────┐
                │         │
            resolves    fails
                │         │
                ▼         ▼
        ┌───────────┐  ┌─────────────────────────────────┐
        │  200 OK   │  │ (1.5) optional: name-history?   │
        │           │  └─────────────┬───────────────────┘
        │ body:     │              ┌─┴──┐
        │  name     │              │    │
        │  item     │            hit    miss / disabled
        │  uuid     │              │    │
        │           │              ▼    │
        │ no drift  │       ┌──────────────┐
        │  signal   │       │  308 Perm.   │
        │  needed   │       │  Redirect    │
        └───────────┘       │              │
                            │ Location:    │
                            │  canonical   │
                            │              │
                            │ Link: rel=   │
                            │  "canonical" │
                            │              │
                            │ client       │
                            │  follows →   │
                            │  step (1)    │
                            │  on new URL  │
                            │  → triplet   │
                            │  in body     │
                            └──────────────┘
                                 │
                                 ▼
                       ┌───────────────────────────────┐
                       │ (2) try item (server UUID)    │
                       └─────────────┬─────────────────┘
                                ┌────┴────┐
                                │         │
                            resolves    fails
                                │         │
                                ▼         ▼
                       ┌─────────────┐  ┌───────────────────────────┐
                       │  200 OK +   │  │ (3) try uuid (global)     │
                       │   drift     │  └─────────────┬─────────────┘
                       │             │           ┌────┴────┐
                       │ Link: rel=  │           │         │
                       │  "canonical"│        resolves    fails
                       │             │           │         │
                       │ Warning:299 │           ▼         ▼
                       │             │   ┌─────────────┐  ┌──────┐
                       │ body:       │   │  200 OK +   │  │ 404  │
                       │  name       │   │   drift     │  │      │
                       │  item       │   │             │  │ (or  │
                       │  uuid       │   │ Link: rel=  │  │ 410  │
                       │  + name-    │   │  "canonical"│  │ if   │
                       │   Warning   │   │             │  │ tomb-│
                       │   carrying  │   │ Warning:299 │  │ stone│
                       │   refreshed │   │             │  │  hit)│
                       │   triplet   │   │ body:       │  └──────┘
                       └─────────────┘   │  name       │
                                         │  item       │
                                         │  uuid       │
                                         │  + name-    │
                                         │   Warning   │
                                         │   carrying  │
                                         │   refreshed │
                                         │   triplet   │
                                         └─────────────┘
```

**What every successful response carries:**

| Path | Status | Triplet location | Drift signal |
|---|---|---|---|
| Step 1 — name resolves | 200 OK | response body (`item`, `uuid` fields) | none — name was current |
| Step 1.5 — history hit | 308 → step 1 retry | in the redirected response body | implicit (via the redirect itself) |
| Step 2 — item resolves | 200 OK + drift | response body + `nameWarning` object | `Link: rel="canonical"` + `Warning: 299` + `nameWarning.current_*` |
| Step 3 — uuid resolves | 200 OK + drift | response body + `nameWarning` object | `Link: rel="canonical"` + `Warning: 299` + `nameWarning.current_*` |
| Step 4 — total failure | 404 (or 410) | n/a | n/a |

The break in the path lookup is what triggers everything that follows.
A server that resolved to a durable identifier first would never observe
the failure — and would never be able to signal drift to the client.

### Drift signal anatomy (200 + drift case)

```
    HTTP/1.1 200 OK
    Content-Type: application/geo+json
    Link: </resources/new-name>; rel="canonical"          ← RFC 8288
    Warning: 299 - "name drift detected"                  ← RFC 9111

    {
      "id":   "new-name",
      "item": "8c3a1f5e-2d44-4b91-a6c2-7e9f5d2b1a83",     ← refreshed triplet
      "uuid": "0a4f3b21-bc28-4d11-8e7e-9c1a3d8f4b22",     ← (cache these)
      "data": { … },
      "nameWarning": {
        "requested_name":         "old-name",
        "served_via":             "item",
        "current_canonical_name": "new-name",
        "current_item":           "8c3a1f5e-…",
        "current_uuid":           "0a4f3b21-…"
      }
    }
```

### Wire example (the headline case)

```bash
# A bookmark from three years ago, when the resource was still named
# 'lirr_station_master'. The resource has since been renamed to 'stations'.
curl -i "http://localhost:5000/collections/lirr_station_master\
?item=8c3a1f5e-2d44-4b91-a6c2-7e9f5d2b1a83\
&uuid=0a4f3b21-bc28-4d11-8e7e-9c1a3d8f4b22"
```

The server tries `lirr_station_master` (fails), falls through to `item`
(succeeds), and serves the resource under its new name with a drift
signal carrying the refreshed triplet. The client refreshes its cache;
the next request succeeds at step (1) with no drift signal needed.

### Verify the claims in the briefs in 60 seconds

```bash
git clone https://github.com/seantucker-spais/frf
cd frf
pip install -r requirements.txt
python -m pytest tests/test_registry.py -v          # 18 tests
python -m pytest tests/test_integration.py -v       # 7 tests
python examples/wire_example_demo.py                # reproduces §4 wire example
```

What the tests demonstrate, mapped to the brief:

| Brief section | Behavior | Test |
|---|---|---|
| §2 step 1 | name resolves cleanly | `test_resolve_step1_name_succeeds` |
| §2 step 2 | bookmark survives rename via item | `test_resolve_step2_item_resolves_after_rename` |
| §2 step 3 | uuid resolves when item also fails (federation) | `test_resolve_step3_uuid_resolves_after_item_fail` |
| §2 step 4 | nothing resolves → 404 | `test_resolve_step4_all_fail_returns_404` |
| §2.2 backcompat | non-opt-in clients see today's behavior | `test_backcompat_non_optin_name_resolves`, `test_backcompat_optin_name_resolves_no_drift`, `test_backcompat_partial_registry` |
| §2.3 silent reassignment | name reuse case (server cannot signal) | `test_natural_first_no_short_circuit_on_disagreement` |
| §2.4 name-history | stale name → 308 redirect | `test_name_history_returns_redirect` |
| §2.4 disabled | history off → legacy 404 | `test_name_history_disabled_returns_404` |
| §2.4 retention | retention window expires entries | `test_name_history_retention_window` |
| Tombstone | deleted resource → 410 Gone | `test_tombstone_returns_gone` |
| Conformance | stable-ids class declared | `test_conformance_declares_stable_ids` |
| Persistence | registry + history survive reload | `test_persistence_includes_history` |
| HTTP integration | 308 redirect path end-to-end | `test_legacy_client_after_rename_gets_308` |
| HTTP integration | 200 + drift signal headers + body | `test_optin_client_with_history_disabled_gets_drift` |

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
publishable briefs; the EES-A-R ontology layer and MCP integration are
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
                   │   (stable IDs, drift, 308)   │  ◄── the briefs
                   └─────┬──────────────────┬─────┘
                         │                  │
              ┌──────────▼─────┐  ┌─────────▼──────────┐
              │   MCP server   │  │   HTTP server      │
              │   (frf_mcp.py) │  │   (frf_http.py)    │
              └────────┬───────┘  └─────────┬──────────┘
                       │                    │
                  Claude / LLMs       QGIS, ArcGIS, browsers
```

## Configuration

The `Registry` constructor accepts two policy parameters per brief §2.4:

```python
from frf_registry import Registry
from pathlib import Path

reg = Registry(
    path=Path("./views/_registry.yaml"),
    name_history_enabled=True,           # default: enabled
    history_retention_seconds=None,      # default: forever
)
```

- Set `name_history_enabled=False` to disable §2.4 behavior. Non-opt-in
  clients then receive 404 on rename, exactly as today's REST APIs do.
- Set `history_retention_seconds=N` to expire history entries after N
  seconds. Useful for limiting storage growth and aligning with
  operational expectations (renames are usually most consulted in the
  months immediately following the change).

These are *deployment* policies, not protocol concerns. Different
deployments will reasonably make different choices.

## Client implementation pattern (brief §2.5)

Clients that follow this six-bullet contract self-heal across all
observable drift modes:

1. **Send the triplet on every request when available.** Path carries
   the name; `?item=` and `&uuid=` carry the durable backups.
2. **Cache the triplet from every successful response.** The response's
   top-level `item` and `uuid` fields are canonical at that moment.
3. **Refresh on every `nameWarning`.** Overwrite cached values with
   `current_canonical_name`, `current_item`, `current_uuid`; update the
   cached URL to the canonical URL from `Link: rel="canonical"`.
4. **Follow 308 Permanent Redirect.** Standard HTTP libraries do this
   automatically; clients with custom redirect handling should ensure
   the cached URL is updated rather than discarded.
5. **Verify uuid agreement on every response.** When 200 OK is received,
   compare the response's `uuid` against the cached `uuid`. Disagreement
   without `nameWarning` indicates the §2.3 silent-reassignment case;
   refresh the cached triplet from the response.
6. **Probe occasionally with backups even when the name appears current.**
   In federated or eventually-consistent deployments, drift may exist
   globally before a particular replica observes it.

## Repository layout

```
frf/
├── README.md                     this file
├── CHANGELOG.md                  v1.3 release notes
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
│   ├── test_registry.py          18 protocol tests
│   ├── test_integration.py       7 end-to-end HTTP tests
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

If you cite the brief, please also cite this implementation.

```bibtex
@misc{tucker2026rest,
  author    = {Tucker, Sean},
  title     = {Stable Identifiers and Name Drift Signaling for REST APIs:
               A Practitioner Technical Brief},
  year      = {2026},
  version   = {v1.3},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.19980026},
  url       = {https://doi.org/10.5281/zenodo.19980026}
}

@misc{tucker2026ogc,
  author    = {Tucker, Sean},
  title     = {Stable Identifiers and Name Drift Signaling for OGC API – Features:
               A Practitioner Technical Brief},
  year      = {2026},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.19954488},
  url       = {https://doi.org/10.5281/zenodo.19954488}
}

@software{tucker2026frf,
  author    = {Tucker, Sean},
  title     = {FRF: File-REST-Framework — Reference Implementation of
               Stable Identifiers and Name Drift},
  year      = {2026},
  version   = {v1.3.0},
  url       = {https://github.com/seantucker-spais/frf}
}
```

## Status

- **REST-generic brief v1.3** — published on Zenodo (DOI above), Apache 2.0
- **OGC-specific brief v1.0** — published on Zenodo; v1.3 revision in progress
- **stable-ids reference implementation** — v1.3, complete and tested
  (25/25 tests passing across registry and integration suites)
- **OGC SWG discussion issue** — filed and updated against
  [opengeospatial/ogcapi-features](https://github.com/opengeospatial/ogcapi-features),
  v1.3 status posted
- **EES-A-R ontology layer** — present but separate from the stable-ids contribution

## License

Apache License 2.0. See [LICENSE](LICENSE).

This license was chosen for two reasons. First, the briefs define a
protocol that people will *implement* — Apache 2.0's explicit patent
grant gives implementers the legal clarity they need to build production
software on the protocol. Second, consistency: the briefs on Zenodo and
this reference implementation share the same license, removing friction
between spec and code.

## Contact

Sean Tucker
University at Buffalo, MS in Applied Ontology
