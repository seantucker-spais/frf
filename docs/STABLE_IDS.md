# Stable Identifiers and Name Drift — Implementation Walkthrough

This document maps the proposed conformance class from the technical brief
onto the reference implementation in [`frf_registry.py`](../frf_registry.py).
It is intended for OGC SWG reviewers, implementers of similar patterns in
other REST APIs, and anyone evaluating the brief against running code.

## At a glance

The brief proposes:

> Two artifacts on every named resource: `item` (server-issued integer) and
> `uuid` (RFC 4122 UUIDv4). One behavior: dual-key resolution with drift
> signaling via `Link: rel="canonical"` and a `nameWarning` body field.
> Tombstone retention with 410 Gone. Resolution precedence:
> uuid > item > name.

The implementation:

- **One module**, ~370 lines, no external dependencies beyond PyYAML
- **Identity record**: the `Entry` dataclass
- **Persistence**: YAML file (`views/_registry.yaml`), human-readable, version-control-friendly
- **Mutation API**: `register`, `rename`, `add_alias`, `delete`
- **Query API**: `lookup_by_item`, `lookup_by_uuid`, `lookup_by_name`
- **Resolution**: the `resolve()` method — the algorithmic core of the brief

## Module map

| Section | Lines | Purpose | Brief reference |
|---|---|---|---|
| `Entry` dataclass | 28–48 | Immutable identity record | §2 artifacts |
| `ResolveResult` | 51–79 | Resolution outcome + drift info | §2 behavior |
| `Registry` constructor + persistence | 88–141 | YAML-backed state | §3 implementation |
| `register` / `rename` / `add_alias` | 144–224 | Mutation API | §3 |
| `delete` (tombstoning) | 226–246 | Permanent retention | §2, 410 Gone |
| `lookup_by_*` primitives | 248–270 | Single-key lookups | resolver dependencies |
| `resolve` (dual-key resolver) | 290–360 | The algorithm | §3 code listing |

## The resolver, line-by-line

The brief's §3 shows an abbreviated 30-line version. The full implementation
is in `Registry.resolve()`. The two are equivalent in behavior; the full
version adds detailed drift attribution for the warning message.

```python
def resolve(self, name=None, item=None, uid=None) -> ResolveResult:
```

**Inputs.** Any subset of `(name, item, uid)`. The path component of an OGC
URI provides `name`; query params `?item=` and `?uuid=` provide the others.
Any combination is valid; missing identifiers are simply not consulted.

```python
candidates = {}
if name is not None:
    candidates["name"] = self.lookup_by_name(name)
```

**Build candidates.** For each supplied identifier, look it up. The result
is one of: an `Entry` (live), `TOMBSTONED` (a sentinel for deleted), or
`None` (never existed).

```python
if item is not None:
    cand = self._lookup_item_live(item)
    candidates["item"] = TOMBSTONED if (cand is None
                                          and item in self._tombstoned_items) else cand
```

**Tombstone detection on items.** An item lookup that returns nothing might
mean "never existed" or "existed and was deleted." The set
`_tombstoned_items` tracks the latter; if the item is in it, the candidate
becomes the `TOMBSTONED` sentinel. This is the discriminator between 410 and 404.

```python
any_tomb = any(c is TOMBSTONED for c in candidates.values())
any_live = any(isinstance(c, Entry) and not c.deleted for c in candidates.values())

if any_tomb and not any_live:
    return ResolveResult(tombstoned=True)        # → 410 Gone
if not any_live:
    return ResolveResult()                        # → 404 Not Found
```

**Tombstone-over-not-found rule.** If any supplied identifier hit a
tombstone *and* nothing live resolved, the result is tombstoned. This is
critical: a stale bookmark for a deleted resource gets a definitive 410,
not an ambiguous 404 that suggests "maybe later."

```python
chosen = next((c for k in ("uuid", "item", "name")
                if isinstance((c := candidates.get(k)), Entry)
                and not c.deleted), None)
```

**Durability hierarchy.** Iterate the identifier types in order
`uuid > item > name` and pick the first one that resolved to a live entry.
This implements the brief's stated precedence directly.

```python
drift = [k for k, c in candidates.items()
          if isinstance(c, Entry) and not c.deleted
          and c.uuid != chosen.uuid]
return ResolveResult(entry=chosen, drift=drift)
```

**Drift detection.** Any other candidate that resolved to a *different*
entry than the chosen one is a drifted identifier. The HTTP layer reads
this list to construct the `Link: rel="canonical"` header and the
`nameWarning` body field.

## How the resolver maps to HTTP behavior

The HTTP layer (`frf_http.py`, function `_resolve_collection`) calls
`resolve()` and translates the `ResolveResult` into HTTP semantics:

| `ResolveResult` state | HTTP response |
|---|---|
| `entry is not None`, `not has_drift` | 200 OK, no warning |
| `entry is not None`, `has_drift` | 200 OK + `Link: rel="canonical"` + `nameWarning` body |
| `tombstoned is True` | 410 Gone with deletion metadata |
| `entry is None`, not tombstoned | 404 Not Found |

## Source/view item-sharing

The brief notes:

> A Core view shares its source's item, recognizing that they are the same
> logical resource at two tiers.

This is implemented via the `share_item_with` parameter on `register()`:

```python
src = registry.register("stations", kind="source")
core = registry.register("stations_core_view", kind="view",
                          share_item_with=src.item)
assert src.item == core.item        # same integer
assert src.uuid != core.uuid        # different UUIDs
```

Two records, one item number, two UUIDs. From the resolver's perspective
they are independently addressable (each has its own UUID); from the
operational perspective they share the integer that an analyst would write
in a spreadsheet or paste into a chat.

## Tombstone permanence

```python
def delete(self, item, when):
    siblings = [e for e in self._entries.values() if e.item == item]
    for e in siblings:
        if not e.deleted:
            e.deleted = True
            e.deleted_at = when
            e.aliases = []          # release names for reuse
    self._reindex()
    self._save()
```

When a resource is deleted:
- The integer and UUID stay in the file forever
- The canonical name and aliases are released — a future resource can claim them
- All siblings sharing the item are tombstoned together (no orphan Core views)
- The next call to `register()` does **not** revisit the dead number; the
  monotonic counter only moves forward

## Persistence and audit

The registry is persisted to `views/_registry.yaml`:

```yaml
next_item: 8
entries:
  - item: 1
    uuid: 9644a26a-...
    name: bridges
    kind: source
    aliases:
      - "path::/abs/path/sources/bridges.csv"
  - item: 4
    uuid: c2078fea-...
    name: stations
    kind: source
  - item: 7
    uuid: b432f1e4-...
    name: station_provenance
    kind: view
    deleted: true
    deleted_at: "2026-04-30T12:00:00Z"
```

This file is human-readable and intended to be committed to version control.
Every issuance, rename, and tombstone is captured in `git log`. There is no
opaque database state; the identifier history is open and inspectable.

## What the implementation does not do

To stay in scope:

- No identifier propagation across federated FRF instances. Each instance
  has its own integer counter; UUIDs are durable across instances but
  integer-to-integer mapping for federation is out of scope here.
- No transactional API for mutations. The registry is single-writer with
  thread-safe in-process locking. Multi-process or distributed mutation
  requires an external coordinator (or a SQLite swap of the backend).
- No identifier propagation to features. The brief notes feature-level IDs
  as a possible scope; this implementation registers collections only,
  with feature IDs delegated to the underlying source's natural keys.

These are deliberate boundaries that match the brief's scope.

## Related code

- [`frf_http.py`](../frf_http.py) — `_resolve_collection()` and
  `_attach_drift_warning()` show the HTTP integration
- [`tests/test_registry.py`](../tests/test_registry.py) — unit tests for
  every claim in the brief
- [`tests/test_integration.py`](../tests/test_integration.py) —
  end-to-end test of the rename + bookmark scenario through Flask
- [`examples/wire_example_demo.py`](../examples/wire_example_demo.py) —
  reproduces §4 of the brief from a cold start
