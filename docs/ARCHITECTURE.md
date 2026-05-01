# FRF Architecture

This document describes the broader FRF stack. The stable-ids work
(separately documented in [STABLE_IDS.md](STABLE_IDS.md)) is the
publishable contribution; everything else here is supporting context.

## Three-layer model

```
┌──────────────────────────────────────────┐
│  Front Doors                             │
│  ┌─────────────┐    ┌─────────────────┐  │
│  │ MCP server  │    │  HTTP server    │  │
│  │ (frf_mcp)   │    │  (frf_http)     │  │
│  │ for LLMs    │    │  OGC API + ext. │  │
│  └──────┬──────┘    └────────┬────────┘  │
└─────────┼────────────────────┼───────────┘
          │                    │
┌─────────▼────────────────────▼───────────┐
│  Engine                                  │
│  ┌──────────────────────────────────┐    │
│  │ frf_registry (stable IDs, drift) │ ◄──┼─── the publishable contribution
│  └──────────────────────────────────┘    │
│  ┌──────────────────────────────────┐    │
│  │ frf_ont (EES-A-R views)          │    │
│  └──────────────────────────────────┘    │
│  ┌──────────────────────────────────┐    │
│  │ frf_mcp.engine (discovery, I/O)  │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
          │
┌─────────▼────────────────────────────────┐
│  Storage                                 │
│  sources/*.csv     views/*.yaml          │
│  views/_registry.yaml                    │
└──────────────────────────────────────────┘
```

## Module responsibilities

### `frf_mcp.py`

Two roles in one file:

1. **Engine module** — source discovery, geometry detection, WKT parsing,
   filter pipeline (`-where`, `--bbox`, `--fields`), output formatters
   (geojson, json, esrijson, csv).
2. **MCP server** — when launched with `--serve`, exposes `engine.run()`
   as an MCP tool callable from LLM hosts (Claude Desktop, etc.).

The CLI-style command grammar (`-src`, `-sch`, `-get`) is the same on both
sides — useful for manual testing without an LLM client.

### `frf_ont.py`

EES-A-R ontological view layer. CSVs become *sources*; YAML files in
`views/` define *views* with one of five ontological categories
(Entity, Event, State, Artifact, Relation), plus relations, constraints,
and provenance trails.

Self-describing CSVs (with an `eesr_type` column) auto-promote to views
without needing a YAML file. YAML views can override or supplement the
auto-promoted ones.

This layer is independently useful but not required for the stable-ids
contribution.

### `frf_registry.py`

The stable-ids reference implementation. See [STABLE_IDS.md](STABLE_IDS.md)
for the full walkthrough.

### `frf_http.py`

OGC API – Features Part 1 Core implementation, plus the proposed
stable-ids extension. Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /` | landing page |
| `GET /conformance` | conformance class declaration |
| `GET /api` | OpenAPI 3.0 service definition |
| `GET /collections` | every source + view |
| `GET /collections/{id}` | collection metadata (with `item`, `uuid`) |
| `GET /collections/{id}/items` | features (FeatureCollection) |
| `GET /collections/{id}/items/{featureId}` | single feature |
| `GET /registry` | identity registry (live + tombstoned) |
| `GET /ontology/...` | EES-A-R views and relation graph |

All collection-level endpoints accept `?item=N` and `?uuid=...` as backup
query parameters per the proposed conformance class.

## Data flow on a single request

For `GET /collections/lirr_station_master?item=42`:

```
  1. Flask route /collections/<collection_id> matches
  2. _resolve_collection() consults frf_registry.resolve()
       Result: entry=<stations>, drift=["name"]
  3. _all_collections() builds the source/view index, keyed by
     current canonical name (resolves "stations" via path alias)
  4. _view_descriptor() (or source equivalent) renders the body,
     including item, uuid, frf:category from the engine
  5. _attach_drift_warning() adds Link: rel="canonical" header
     and nameWarning body field
  6. Flask returns 200 OK with Content-Type: application/json
```

The same request flow handles features, with the items/feature endpoints
adding paging links, format negotiation, and OGC-required envelope fields.

## Extension points

The implementation is structured for clean extension:

- **Source backends.** Currently CSV. The `Source` dataclass and
  `_iter_rows()` / `_profile_csv()` functions are the plug-in surface for
  adding GDB, shapefile, parquet, PostGIS, or WFS backends.
- **Output formats.** `_serialize` and the format-specific helpers in
  `frf_mcp.py` are where new output formats (e.g., FlatGeobuf) would slot.
- **Identifier backends.** `frf_registry.Registry` is YAML-backed; replacing
  the `_load`/`_save` methods with SQLite or PostgreSQL is a contained
  change that doesn't affect the resolver.
- **Conformance classes.** Each new class is one entry in
  `/conformance` and one set of endpoint behaviors in `frf_http.py`.

## What's not here

Out of scope for this implementation:

- OGC API – Features Part 4 (Create, Replace, Update, Delete). The
  registry has the lifecycle hooks (`register`, `delete`) ready, but
  the HTTP write path is not wired.
- OGC API – Records. Adjacent and complementary; not implemented.
- Authentication / authorization. Add via Flask middleware.
- Multi-instance federation. Each FRF instance has its own integer
  counter; UUIDs are durable across instances but a federation-aware
  registry would need cross-walks.

These are straightforward extensions that don't change the core architecture.
