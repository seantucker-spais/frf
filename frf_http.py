#!/usr/bin/env python3
"""
FRF HTTP — OGC API - Features (Part 1: Core) compliant HTTP server.

Reuses the same source registry and feature pipeline as frf_mcp.py, exposing
them over standard OGC paths so QGIS, ArcGIS Pro, Leaflet, OpenLayers, and
the OGC TEAM Engine validator all just work.

Endpoints (per OGC 17-069r4 / ISO 19168-1:2025)
-----------------------------------------------
  GET /                                    Landing page
  GET /conformance                         Conformance declaration
  GET /api                                 OpenAPI 3.0 definition (minimal)
  GET /collections                         List of collections
  GET /collections/{collectionId}          Collection metadata
  GET /collections/{collectionId}/items    Features (query: bbox, limit, offset, properties, f)
  GET /collections/{collectionId}/items/{featureId}   Single feature

Extension query params
----------------------
  f=json|geojson|esrijson|csv              Output format (geojson is OGC default)
  where=<col op value>                     FRF attribute filter (vendor extension)
  fields=a,b,c                             Property projection (vendor extension)

Run
---
  python frf_http.py                       # serves on http://127.0.0.1:5000
  python frf_http.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlencode

from flask import Flask, jsonify, request, abort, Response

# Reuse the engine from the MCP server.
import frf_mcp as engine
try:
    import frf_ont as ont
    _HAS_ONT = True
except ImportError:
    _HAS_ONT = False
try:
    import frf_registry
    _HAS_REGISTRY = True
except ImportError:
    _HAS_REGISTRY = False

app = Flask(__name__)


def _resolve_collection(collection_id: str):
    """Resolve a collection per brief v1.3 protocol.

    Returns a tuple (resolved_name, drift_info, redirect_url):
      - resolved_name: the canonical name to dispatch to internally, or None
                       if the response should be a redirect.
      - drift_info: dict for the response body's nameWarning, or None.
      - redirect_url: an absolute URL string if the resolver indicated 308,
                      or None.

    Honors:
      - 200 OK with drift signal when a backup resolves
      - 308 redirect when name-history is enabled and the path name has
        a history entry
      - 410 Gone when an identifier is tombstoned
      - 404 deferred to downstream when no backups were sent and registry
        has no entry (legacy passthrough so file-based source discovery
        can still find pre-registry resources)
    """
    if not _HAS_REGISTRY:
        return collection_id, None, None

    item_param = request.args.get("item", type=str)
    uuid_param = request.args.get("uuid", type=str)

    idreg = frf_registry.get_registry()
    res = idreg.resolve(name=collection_id, item=item_param, uid=uuid_param)

    # 410 Gone — identifier was tombstoned
    if res.tombstoned:
        abort(410, f"resource was deleted "
                    f"(item={item_param}, uuid={uuid_param})")

    # 308 redirect — name-history hit
    if res.is_redirect:
        base = request.host_url.rstrip("/")
        redirect_url = f"{base}/collections/{res.redirect_to_name}"
        return None, None, redirect_url

    # Successful resolution via registry (name, item, or uuid)
    if res.entry is not None:
        drift_info = None
        if res.has_drift:
            drift_info = {
                "requested_name": collection_id,
                "served_via": res.served_via,
                "current_canonical_name": res.entry.name,
                "current_item": res.entry.item,
                "current_uuid": res.entry.uuid,
                "message": res.warning_message(),
            }
        return res.entry.name, drift_info, None

    # No registry entry. If the client sent backups, this is a definite 404
    # (they specified durable IDs that don't resolve anywhere). Otherwise,
    # defer to downstream — the source may be discoverable from disk and
    # not yet in the registry (legacy passthrough).
    if item_param is not None or uuid_param is not None:
        abort(404, f"unknown collection: {collection_id}")

    # No backups, no registry hit, no history hit → defer to downstream
    return collection_id, None, None


def _attach_drift_warning(response: Response, drift_info: dict | None) -> Response:
    """Add drift-signaling headers per the proposed stable-ids conformance class:

    - Link: rel="canonical" per RFC 8288 (the brief's primary mechanism)
    - Warning: 299 per RFC 7234 §5.5 (auxiliary, human-readable)

    The body field 'RestReference:nameWarning' is added by the caller.
    """
    if drift_info:
        canonical_name = drift_info.get("current_canonical_name", "")
        msg = drift_info.get("message", "name drift detected")
        if canonical_name:
            base = request.host_url.rstrip("/")
            canonical_url = f"{base}/collections/{canonical_name}"
            response.headers["Link"] = f'<{canonical_url}>; rel="canonical"'
        response.headers["Warning"] = f'299 - "{msg}"'
    return response


def _build_redirect(canonical_url: str) -> Response:
    """Build a 308 Permanent Redirect response per brief v1.3 §2.4.

    Standard HTTP clients follow this automatically; method is preserved
    (308 over 301) so POST/PUT/DELETE redirect cleanly.
    """
    response = Response(
        f"resource has moved; canonical URL: {canonical_url}",
        status=308,
        mimetype="text/plain",
    )
    response.headers["Location"] = canonical_url
    response.headers["Link"] = f'<{canonical_url}>; rel="canonical"'
    return response


def _all_collections() -> dict:
    """Return name -> descriptor for every collection. Views shadow raw sources
    (so /collections/stations serves the View, not the raw file). Sources without
    a view still appear, classified as 'Raw'.

    The keys include CURRENT canonical names from the identity registry — so
    after a rename, `_all_collections()['mta_lirr_stations']` finds the source
    that used to be called `stations` on disk. Both the YAML name and the
    registry name are valid keys (when they differ) so legacy YAML refs and
    post-rename refs both work.
    """
    sources = engine.discover_sources()
    views = ont.discover_views() if _HAS_ONT else {}
    out: dict = {}

    idreg = frf_registry.get_registry() if _HAS_REGISTRY else None

    def canonical_for(name: str, fallback_path=None) -> str:
        if idreg is None:
            return name
        if fallback_path is not None:
            e = idreg.lookup_by_name(f"path::{fallback_path}")
            if e is not None:
                return e.name
        e = idreg.lookup_by_name(name)
        if e is not None:
            return e.name
        return name

    # views first
    for v in views.values():
        canon = canonical_for(v.name).lower()
        yaml_name = v.name.lower()
        out[canon] = ("view", v)
        if yaml_name != canon:
            # also accept the YAML-file name for backward compatibility
            out[yaml_name] = ("view", v)

    # raw sources that don't have a corresponding view at either canon or yaml name
    for s in sources.values():
        canon = canonical_for(s.name, fallback_path=s.path.resolve()).lower()
        if canon not in out:
            out[canon] = ("source", s)
        # also expose under the filename-derived name if different
        if s.name.lower() != canon and s.name.lower() not in out:
            out[s.name.lower()] = ("source", s)

    return out


# ---------- helpers ----------
def _base_url() -> str:
    """The {root} for OGC link construction."""
    return request.host_url.rstrip("/")


def _link(href: str, rel: str, type_: str = "application/json", title: str | None = None) -> dict:
    link = {"href": href, "rel": rel, "type": type_}
    if title:
        link["title"] = title
    return link


def _negotiate_format(default: str = "geojson") -> str:
    """Honor ?f= first, then Accept header. OGC convention is f-param wins."""
    f = request.args.get("f", "").lower()
    if f in ("json", "geojson", "esrijson", "csv"):
        return f
    accept = request.headers.get("Accept", "")
    if "geo+json" in accept or "geojson" in accept:
        return "geojson"
    if "esri" in accept:
        return "esrijson"
    if "text/csv" in accept:
        return "csv"
    if "application/json" in accept:
        return "json"
    return default


def _content_type(fmt: str) -> str:
    return {
        "geojson": "application/geo+json",
        "json": "application/json",
        "esrijson": "application/json",
        "csv": "text/csv",
    }[fmt]


def _collection_descriptor(src: engine.Source) -> dict:
    """Build the JSON descriptor for one collection (used in /collections and
    /collections/{id})."""
    base = _base_url()
    desc = {
        "id": src.name,
        "title": src.name,
        "description": f"FRF source backed by {src.path.name} ({src.rows} rows)",
        "itemType": "feature",
        "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
        "links": [
            _link(f"{base}/collections/{src.name}", "self", title="this collection"),
            _link(f"{base}/collections/{src.name}/items", "items",
                  type_="application/geo+json", title="features"),
        ],
    }
    # extent if we can compute one cheaply
    extent = _compute_extent(src)
    if extent:
        desc["extent"] = {"spatial": {"bbox": [extent],
                                       "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}}
    # Stable identifiers from the registry
    if _HAS_REGISTRY:
        idreg = frf_registry.get_registry()
        e = idreg.lookup_by_name(src.name)
        if e:
            desc["item"] = e.item
            desc["uuid"] = e.uuid
            if e.aliases:
                desc["RestReference:also_known_as"] = e.aliases
    return desc


def _compute_extent(src: engine.Source) -> list[float] | None:
    """Compute spatial extent [minx,miny,maxx,maxy] in WGS84."""
    if src.geom_kind == "none":
        return None
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    found = False
    for row in engine._iter_rows(src):
        if src.geom_kind == "point_xy":
            try:
                x = float(row[src.geom_cols[0]]); y = float(row[src.geom_cols[1]])
            except (TypeError, ValueError):
                continue
            minx, miny, maxx, maxy = min(minx, x), min(miny, y), max(maxx, x), max(maxy, y)
            found = True
        elif src.geom_kind == "wkt":
            g = engine._parse_wkt(row.get(src.geom_cols[0]))
            b = engine._geom_bounds(g)
            if not b:
                continue
            minx, miny = min(minx, b[0]), min(miny, b[1])
            maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
            found = True
    return [minx, miny, maxx, maxy] if found else None


def _serialize(payload: dict | str, fmt: str) -> Response:
    if isinstance(payload, str):
        return Response(payload, mimetype=_content_type(fmt))
    return Response(json.dumps(payload, indent=2), mimetype=_content_type(fmt))


# ---------- routes ----------
@app.route("/")
def landing_page():
    base = _base_url()
    links = [
        _link(f"{base}/", "self", title="this document"),
        _link(f"{base}/api", "service-desc",
              type_="application/vnd.oai.openapi+json;version=3.0",
              title="API definition"),
        _link(f"{base}/conformance", "conformance", title="OGC API conformance classes"),
        _link(f"{base}/collections", "data", title="Feature collections"),
    ]
    if _HAS_ONT:
        links.append(_link(f"{base}/ontology", "frf:ontology",
                             title="EES-A-R ontology"))
    if _HAS_REGISTRY:
        links.append(_link(f"{base}/registry", "RestReference:registry",
                             title="Identity registry (stable IDs)"))
    return jsonify({
        "title": "FRF — File-REST-Framework",
        "description": "OGC API - Features service backed by CSV files in ./sources/, "
                        "with optional EES-A-R ontological views.",
        "links": links,
    })


@app.route("/conformance")
def conformance():
    classes = [
        "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
        "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
        "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
    ]
    if _HAS_ONT:
        classes.append("https://frf.dev/spec/ontology/1.0/conf/eesar")
    if _HAS_REGISTRY:
        classes.append("https://frf.dev/spec/stable-ids/1.0/conf/stable-ids")
    return jsonify({"conformsTo": classes})


@app.route("/registry")
def registry_endpoint():
    """The authoritative identifier registry. Lists every (item, uuid, name)
    triple, including tombstoned ones."""
    if not _HAS_REGISTRY:
        abort(404, "registry not available")
    # Trigger discovery so any newly-found sources/views are registered first.
    engine.discover_sources()
    if _HAS_ONT:
        ont.discover_views()
    idreg = frf_registry.get_registry()
    return jsonify({
        "title": "FRF Identity Registry",
        "description": "Authoritative item ↔ uuid ↔ name registry. "
                        "Tombstoned identifiers are retained forever; "
                        "their items and uuids are never reused.",
        "live": [{"item": e.item, "uuid": e.uuid, "name": e.name,
                    "kind": e.kind, "aliases": e.aliases}
                   for e in idreg.all_entries(include_deleted=False)],
        "tombstoned": [{"item": e.item, "uuid": e.uuid,
                          "former_name": e.name, "kind": e.kind,
                          "deleted_at": e.deleted_at}
                          for e in idreg.all_entries(include_deleted=True)
                          if e.deleted],
    })


# ---------- ontology endpoints ----------
@app.route("/ontology")
def ontology_landing():
    if not _HAS_ONT:
        abort(404, "ontology engine not available")
    base = _base_url()
    views = ont.discover_views()
    by_cat: dict[str, list[str]] = {c: [] for c in ont.CATEGORIES}
    for v in views.values():
        by_cat.setdefault(v.category, []).append(v.name)
    return jsonify({
        "title": "FRF Ontology — EES-A-R",
        "description": "Five-category ontological framework over the FRF data sources.",
        "categories": ont.CATEGORIES,
        "views_by_category": by_cat,
        "links": [
            _link(f"{base}/ontology", "self"),
            _link(f"{base}/ontology/views", "frf:views", title="all views"),
            _link(f"{base}/ontology/relations", "frf:relations", title="relation graph"),
            _link(f"{base}/ontology/anomalies", "frf:anomalies", title="anomalies"),
            _link(f"{base}/collections", "data", title="OGC collections"),
        ],
    })


@app.route("/ontology/views")
def ontology_views():
    if not _HAS_ONT:
        abort(404, "ontology engine not available")
    views = ont.discover_views()
    return jsonify([{
        "name": v.name, "category": v.category, "domain_type": v.domain_type,
        "description": v.description, "primary": v.primary,
        "virtual": v.virtual_from is not None,
        "fields": [{"name": fm.out_name, "source": fm.source,
                     "source_column": fm.source_col} for fm in v.fields],
        "relations": [{"name": r.name, "target": r.target, "on": r.on,
                         "kind": r.kind} for r in v.relations],
        "constraints": [{"name": c.name, "expr": c.expr,
                           "description": c.description} for c in v.constraints],
        "source_path": v.source_path,
    } for v in views.values()])


@app.route("/ontology/relations")
def ontology_relations():
    if not _HAS_ONT:
        abort(404, "ontology engine not available")
    views = ont.discover_views()
    edges = []
    for v in views.values():
        for r in v.relations:
            edges.append({"from": v.name, "to": r.target,
                           "name": r.name, "on": r.on, "kind": r.kind})
    return jsonify({"views": list(views.keys()), "edges": edges})


@app.route("/ontology/anomalies")
def ontology_anomalies():
    if not _HAS_ONT:
        abort(404, "ontology engine not available")
    views = ont.discover_views()
    registry = engine.discover_sources()
    out = {}
    for v in views.values():
        if not v.constraints:
            continue
        anoms = ont.evaluate_constraints(v, registry)
        if anoms:
            out[v.name] = anoms
    return jsonify(out)


@app.route("/api")
def api_definition():
    """Minimal OpenAPI 3.0 definition. Enough for OGC validator's existence
    check; expand later if you want full schema docs."""
    base = _base_url()
    return jsonify({
        "openapi": "3.0.3",
        "info": {
            "title": "FRF OGC API - Features",
            "version": "1.0.0",
            "description": "File-REST-Framework: CSVs served as OGC API features.",
        },
        "servers": [{"url": base}],
        "paths": {
            "/": {"get": {"summary": "Landing page", "responses": {"200": {"description": "OK"}}}},
            "/conformance": {"get": {"summary": "Conformance declaration",
                                      "responses": {"200": {"description": "OK"}}}},
            "/collections": {"get": {"summary": "Collections list",
                                       "responses": {"200": {"description": "OK"}}}},
            "/collections/{collectionId}": {
                "get": {"summary": "Collection metadata",
                        "parameters": [{"name": "collectionId", "in": "path", "required": True,
                                          "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "OK"}}},
            },
            "/collections/{collectionId}/items": {
                "get": {"summary": "Features",
                        "parameters": [
                            {"name": "collectionId", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "bbox", "in": "query",
                             "schema": {"type": "string"}, "description": "minx,miny,maxx,maxy"},
                            {"name": "limit", "in": "query",
                             "schema": {"type": "integer", "default": engine.DEFAULT_LIMIT}},
                            {"name": "offset", "in": "query",
                             "schema": {"type": "integer", "default": 0}},
                            {"name": "f", "in": "query",
                             "schema": {"type": "string",
                                          "enum": ["json", "geojson", "esrijson", "csv"]}},
                        ],
                        "responses": {"200": {"description": "OK"}}},
            },
            "/collections/{collectionId}/items/{featureId}": {
                "get": {"summary": "Single feature",
                        "parameters": [
                            {"name": "collectionId", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "featureId", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "OK"},
                                       "404": {"description": "Not found"}}},
            },
        },
    })


@app.route("/collections")
def collections():
    base = _base_url()
    cols = []
    for name, (kind, obj) in _all_collections().items():
        if kind == "view":
            cols.append(_view_descriptor(obj))
        else:
            cols.append(_collection_descriptor(obj))
    return jsonify({
        "links": [_link(f"{base}/collections", "self", title="this document")],
        "collections": cols,
    })


def _view_descriptor(view) -> dict:
    """Collection descriptor for an ontological view. Adds frf:* extension props."""
    base = _base_url()
    desc = {
        "id": view.name,
        "title": view.name,
        "description": view.description or f"FRF view ({view.category}/{view.domain_type})",
        "itemType": "feature" if view.geom_kind != "none" else "record",
        "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
        "links": [
            _link(f"{base}/collections/{view.name}", "self", title="this collection"),
            _link(f"{base}/collections/{view.name}/items", "items",
                  type_="application/geo+json", title="features"),
        ],
        "frf:category": view.category,
        "frf:domainType": view.domain_type,
        "frf:virtual": view.virtual_from is not None,
        "frf:relations": [{"name": r.name, "target": r.target, "on": r.on}
                            for r in view.relations],
        "frf:constraints": [c.name for c in view.constraints],
    }
    # Stable identifiers from the registry
    if _HAS_REGISTRY:
        idreg = frf_registry.get_registry()
        e = idreg.lookup_by_name(view.name)
        if e:
            desc["item"] = e.item
            desc["uuid"] = e.uuid
            if e.aliases:
                desc["RestReference:also_known_as"] = e.aliases
    return desc


@app.route("/collections/<collection_id>")
def collection(collection_id: str):
    resolved_name, drift, redirect_url = _resolve_collection(collection_id)
    if redirect_url:
        return _build_redirect(redirect_url)
    cols = _all_collections()
    cid = resolved_name.lower()
    if cid not in cols:
        abort(404, f"unknown collection: {collection_id}")
    kind, obj = cols[cid]
    if kind == "view":
        body = _view_descriptor(obj)
    else:
        body = _collection_descriptor(obj)
    if drift:
        body["RestReference:nameWarning"] = drift
    response = jsonify(body)
    return _attach_drift_warning(response, drift)


@app.route("/collections/<collection_id>/items")
def items(collection_id: str):
    resolved_name, drift, redirect_url = _resolve_collection(collection_id)
    if redirect_url:
        # Preserve the original query string on the redirect target so
        # any ?bbox, ?limit, ?f, etc. flows to the canonical URL.
        if request.query_string:
            sep = "&" if "?" in redirect_url else "?"
            redirect_url = (redirect_url + sep +
                             request.query_string.decode("ascii"))
        # The /items suffix needs to be on the redirect target
        if redirect_url.endswith("/items") is False:
            # Replace /collections/{name} with /collections/{name}/items
            base, _, _ = redirect_url.partition("?")
            qs = redirect_url[len(base):]
            redirect_url = base + "/items" + qs
        return _build_redirect(redirect_url)
    cols = _all_collections()
    cid = resolved_name.lower()
    if cid not in cols:
        abort(404, f"unknown collection: {collection_id}")
    kind, obj = cols[cid]

    # ?raw=true forces the raw-source path even if a view exists.
    raw = request.args.get("raw", "").lower() in ("true", "1", "yes")
    explain = request.args.get("explain", "").lower() in ("true", "1", "yes")

    if kind == "view" and not raw and _HAS_ONT:
        fmt = _negotiate_format("geojson" if obj.geom_kind != "none" else "json")
        try:
            fc = ont.get_view_features(
                obj.name,
                limit=request.args.get("limit"),
                offset=request.args.get("offset"),
                bbox=request.args.get("bbox"),
                where=request.args.get("where"),
                fields=request.args.get("fields"),
                explain=explain,
            )
        except KeyError:
            abort(404, f"unknown view: {collection_id}")
        fc["links"] = _build_paging_links(collection_id, fc)
        fc["timeStamp"] = _now_iso()
        if drift:
            fc["RestReference:nameWarning"] = drift
        response = _serialize(fc, fmt)
        return _attach_drift_warning(response, drift)

    # raw-source path (or raw=true): use the existing engine
    src = obj if kind == "source" else engine.discover_sources().get(obj.primary)
    if src is None:
        abort(404, f"unknown source")
    fmt = _negotiate_format("geojson" if src.geom_kind != "none" else "json")
    parts = [f"-get {src.name}"]
    if "bbox" in request.args:
        parts.append(f"--bbox {request.args['bbox']}")
    if "limit" in request.args:
        parts.append(f"--limit {request.args['limit']}")
    if "offset" in request.args:
        parts.append(f"--offset {request.args['offset']}")
    if "where" in request.args:
        parts.append(f'--where "{request.args["where"]}"')
    if "fields" in request.args:
        parts.append(f"--fields {request.args['fields']}")
    parts.append(f"--f {fmt}")
    body = engine.run(" ".join(parts))
    if fmt in ("geojson", "json"):
        try:
            obj_ = json.loads(body)
            obj_["links"] = _build_paging_links(collection_id, obj_)
            obj_["timeStamp"] = _now_iso()
            if drift:
                obj_["RestReference:nameWarning"] = drift
            response = _serialize(obj_, fmt)
            return _attach_drift_warning(response, drift)
        except json.JSONDecodeError:
            pass
    response = _serialize(body, fmt)
    return _attach_drift_warning(response, drift)


@app.route("/collections/<collection_id>/items/<feature_id>")
def item(collection_id: str, feature_id: str):
    resolved_name, drift, redirect_url = _resolve_collection(collection_id)
    if redirect_url:
        # Append /items/{feature_id} to the redirect target, preserving the
        # query string.
        base, _, _ = redirect_url.partition("?")
        qs = redirect_url[len(base):]
        redirect_url = f"{base}/items/{feature_id}{qs}"
        if request.query_string:
            sep = "&" if "?" in redirect_url else "?"
            redirect_url = (redirect_url + sep +
                             request.query_string.decode("ascii"))
        return _build_redirect(redirect_url)
    registry = engine.discover_sources()
    src = registry.get(resolved_name.lower())
    if not src:
        abort(404, f"unknown collection: {collection_id}")
    fmt = _negotiate_format("geojson" if src.geom_kind != "none" else "json")
    body = engine.run(f"-get {src.name} --id {feature_id} --f {fmt}")
    if body.startswith("id ") and body.endswith("not found"):
        abort(404, body)
    # Add OGC self/collection links for GeoJSON
    if fmt == "geojson":
        try:
            feat = json.loads(body)
            base = _base_url()
            feat["links"] = [
                _link(f"{base}/collections/{collection_id}/items/{feature_id}", "self"),
                _link(f"{base}/collections/{collection_id}", "collection"),
            ]
            if drift:
                feat["RestReference:nameWarning"] = drift
            response = _serialize(feat, fmt)
            return _attach_drift_warning(response, drift)
        except json.JSONDecodeError:
            pass
    response = _serialize(body, fmt)
    return _attach_drift_warning(response, drift)


# ---------- paging + timestamp ----------
def _build_paging_links(collection_id: str, fc: dict) -> list[dict]:
    base = _base_url()
    args = dict(request.args)
    limit = int(args.get("limit") or engine.DEFAULT_LIMIT)
    offset = int(args.get("offset") or 0)
    matched = fc.get("numberMatched", 0)
    returned = fc.get("numberReturned", 0)

    self_args = dict(args)
    self_url = f"{base}/collections/{collection_id}/items"
    if self_args:
        self_url += "?" + urlencode(self_args)
    links = [_link(self_url, "self", type_="application/geo+json")]

    if offset + returned < matched:
        next_args = dict(args)
        next_args["limit"] = str(limit)
        next_args["offset"] = str(offset + limit)
        links.append(_link(
            f"{base}/collections/{collection_id}/items?{urlencode(next_args)}",
            "next", type_="application/geo+json", title="next page"))

    if offset > 0:
        prev_args = dict(args)
        prev_args["limit"] = str(limit)
        prev_args["offset"] = str(max(0, offset - limit))
        links.append(_link(
            f"{base}/collections/{collection_id}/items?{urlencode(prev_args)}",
            "prev", type_="application/geo+json", title="previous page"))

    links.append(_link(
        f"{base}/collections/{collection_id}", "collection", title="parent collection"))
    return links


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- error handling ----------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"code": "NotFound", "description": str(e)}), 404


@app.errorhandler(410)
def gone(e):
    return jsonify({
        "code": "Gone",
        "description": str(e),
        "RestReference:hint": "This identifier was deleted. Items and UUIDs "
                                "are never reissued. Check /registry for current resources.",
    }), 410


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"code": "BadRequest", "description": str(e)}), 400


# ---------- entry ----------
def main():
    p = argparse.ArgumentParser(description="FRF OGC API - Features HTTP server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print(f"FRF HTTP serving on http://{args.host}:{args.port}")
    print(f"  landing page:  http://{args.host}:{args.port}/")
    print(f"  collections:   http://{args.host}:{args.port}/collections")
    print(f"  conformance:   http://{args.host}:{args.port}/conformance")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
