# Conformance Class — Mapping to Implementation

This document maps the requirements of the proposed
`http://www.opengis.net/spec/ogcapi-features-N/1.0/conf/stable-ids`
conformance class to the FRF reference implementation, requirement by
requirement, with code citations and test references.

## Declared at /conformance

```bash
$ curl http://localhost:5000/conformance
{
  "conformsTo": [
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
    "https://frf.dev/spec/stable-ids/1.0/conf/stable-ids"
  ]
}
```

The fourth class identifies this server as implementing the proposed
extension. The URI is provisional pending OGC adoption.

## Requirements coverage

| Req ID | Requirement | Implementation | Test |
|---|---|---|---|
| /req/stable-ids/collection-item | Each collection includes `item` integer property | `frf_http.py:_view_descriptor` and `_collection_descriptor` | `test_basic_register` |
| /req/stable-ids/collection-uuid | Each collection includes `uuid` (RFC 4122) | same as above | `test_basic_register` |
| /req/stable-ids/query-param-item | Server accepts `?item=N` | `frf_http.py:_resolve_collection` | `test_resolve_via_item_after_rename` |
| /req/stable-ids/query-param-uuid | Server accepts `?uuid=...` | same | `test_resolve_uuid_wins_over_item` |
| /req/stable-ids/durability | Once issued, item/uuid never reissued | `Registry.register` uses `_next_item` monotonic counter; never reads tombstones | `test_tombstone_returns_gone` |
| /req/stable-ids/tombstone | Deleted resources retained as tombstones, return 410 Gone | `Registry.delete` + `frf_http.py:gone` error handler | `test_tombstone_returns_gone` |
| /req/stable-ids/drift-resolution | Stale name + valid backup → serve resource | `Registry.resolve` durability hierarchy | `test_resolve_via_item_after_rename` |
| /req/stable-ids/drift-warning-header | `Link: rel="canonical"` on drift | `_attach_drift_warning` (also emits `Warning: 299` per RFC 7234) | `test_headline_scenario` |
| /req/stable-ids/drift-warning-body | Body identifies current canonical name | `nameWarning` property on response body | `test_headline_scenario` |
| /req/stable-ids/disagreement | Durable identifier wins, drift signaled | `Registry.resolve` precedence + `result.drift` | `test_resolve_disagreement_number_wins` |
| /req/stable-ids/registry | Authoritative registry endpoint | `GET /registry` | manual + `test_registry_endpoint` |

## Recommendation coverage

| Rec ID | Recommendation | Implementation |
|---|---|---|
| /rec/stable-ids/registry-endpoint | Expose `/registry` | `frf_http.py:registry_endpoint` |
| /rec/stable-ids/aliases | Accept additional alias names | `Registry.add_alias`, lookups consult `_by_alias` |
| /rec/stable-ids/uuid-precedence | uuid > item on disagreement | `Registry.resolve` iteration order |

## Worked examples

### Happy path — name only, no warning

```http
GET /collections/stations HTTP/1.1

HTTP/1.1 200 OK
Content-Type: application/json
{
  "id": "stations",
  "item": 4,
  "uuid": "c2078fea-bc28-4d11-...",
  ...
}
```

### Drift detected — wrong name + valid item

```http
GET /collections/lirr_station_master?item=4 HTTP/1.1

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
    "current_item": 4,
    "current_uuid": "c2078fea-bc28-4d11-..."
  }
}
```

### Tombstoned — 410 Gone

```http
GET /collections/anything?item=23 HTTP/1.1

HTTP/1.1 410 Gone
Content-Type: application/json
{
  "code": "Gone",
  "description": "resource was deleted (item=23)",
  "RestReference:hint": "This identifier was deleted. Items and UUIDs are never reissued."
}
```

### UUID-only resolution — paranoid mode

```http
GET /collections/_?uuid=c2078fea-bc28-4d11-... HTTP/1.1

HTTP/1.1 200 OK
Link: </collections/stations>; rel="canonical"
{
  ...
  "nameWarning": {
    "requested_name": "_",
    "served_via": "uuid",
    "current_canonical_name": "stations",
    ...
  }
}
```

## Test execution

```bash
# Run every test cited above:
python -m pytest tests/ -v

# Just the registry unit tests:
python -m pytest tests/test_registry.py -v

# Just the integration tests (the wire-level scenarios):
python -m pytest tests/test_integration.py -v
```

All tests pass on Python 3.9+ with PyYAML and Flask installed.

## Validator readiness

The OGC TEAM Engine validator for OGC API – Features Part 1 Core can be
run against this implementation:

```bash
docker run -p 8081:8080 ogccite/ets-ogcapi-features10
# point validator at http://host:5000/
```

The Core, GeoJSON, and OpenAPI 3.0 conformance classes pass. The
stable-ids class has no validator yet — it is the subject of this proposal.
