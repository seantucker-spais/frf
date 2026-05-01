#!/usr/bin/env python3
"""
FRF MCP — File-REST-Framework MCP server.

Drop CSVs into ./sources/ and they're auto-served in OGC-style REST.
A single MCP tool `frf` accepts a command-line-style string.

Commands
--------
  -help                          Show help
  -src                           List sources (compact)
  -src -s <pattern>              Filter sources by name/wildcard (parcels*, *roads*, exact)
  -src -all                      List all sources with full metadata
  -sch <source>                  Schema for one source
  -sch -s <pattern>              Schema(s) for matching sources
  -get <source>                  Fetch features (OGC FeatureCollection)
       --bbox minx,miny,maxx,maxy
       --limit N                 (default 100, max 10000)
       --offset N
       --where "col op value"    (op: =, !=, >, <, >=, <=, like)
       --fields a,b,c
       --f json|geojson|esrijson|csv      (default geojson if geometry detected, else json)
       --id <value>              Single feature by id column
  -get <source> --id <v>         Shortcut for single feature

Ontology (EES-A-R)
------------------
  -ont                           List all ontological views (compact)
  -ont -all                      List with full metadata
  -ont -s <pattern>              Filter by name, category, or domain_type
                                 (e.g. -ont -s entity)
  -rel                           Show the full relation graph
  -rel <view>                    Show relations from a specific view
  -anom                          Show all anomalies across all views
  -anom <view>                   Show anomalies for one view
  -explain <view>                Show provenance trail for a view's schema
"""

from __future__ import annotations

import csv
import fnmatch
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    from shapely import wkt as _wkt
    from shapely.geometry import mapping as _shapely_mapping
    _HAS_SHAPELY = True
except ImportError:
    _HAS_SHAPELY = False

# ---------- config ----------
SOURCES_DIR = Path(__file__).parent / "sources"
DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000

# Heuristics for detecting geometry columns in CSVs
LON_NAMES = {"lon", "lng", "long", "longitude", "x"}
LAT_NAMES = {"lat", "latitude", "y"}
WKT_NAMES = {"wkt", "geom", "geometry", "the_geom"}


# ---------- source registry ----------
@dataclass
class Source:
    name: str           # canonical id (filename stem, lowercased)
    path: Path
    rows: int
    fields: list[str]
    geom_kind: str      # "point_xy" | "wkt" | "none"
    geom_cols: tuple[str, ...]  # ("lon","lat") or ("wkt_col",) or ()


def discover_sources() -> dict[str, Source]:
    """Scan SOURCES_DIR and build a registry of CSV sources.
    Also auto-registers each in the identity registry, issuing stable item+uuid.

    Identity continuity: a source is identified to the registry by its file
    PATH, not its filename. After an operator rename via the registry, the
    file path still maps to the same registered entry — preventing duplicate
    registration on rediscovery.
    """
    SOURCES_DIR.mkdir(exist_ok=True)
    registry: dict[str, Source] = {}
    try:
        import frf_registry
        idreg = frf_registry.get_registry()
    except ImportError:
        idreg = None

    # Build a path -> entry index for quick "does this file already have an id?"
    path_index: dict[str, object] = {}
    if idreg is not None:
        for e in idreg.all_entries(include_deleted=False):
            for a in e.aliases:
                if a.startswith("path::"):
                    path_index[a] = e

    for path in sorted(SOURCES_DIR.glob("*.csv")):
        try:
            src = _profile_csv(path)
            registry[src.name] = src
            if idreg is None:
                continue
            path_alias = f"path::{path.resolve()}"
            existing = path_index.get(path_alias)
            if existing is None and idreg.lookup_by_name(src.name) is None:
                # Brand new source — register and tag with path alias.
                try:
                    e = idreg.register(src.name, kind="source",
                                        aliases=[path_alias])
                except ValueError:
                    pass
            elif existing is None and idreg.lookup_by_name(src.name) is not None:
                # Filename matches an existing registry name but no path alias.
                # Pin the path to the existing entry as an alias for next time.
                e = idreg.lookup_by_name(src.name)
                if e is not None:
                    try:
                        idreg.add_alias(e.item, path_alias)
                    except ValueError:
                        pass
            # else: path already has a registered entry (post-rename case);
            # keep the registry's canonical name, leave the source.name from disk
        except Exception as e:
            sys.stderr.write(f"[frf] skipping {path.name}: {e}\n")
    return registry


def _profile_csv(path: Path) -> Source:
    """Read header + count rows + detect geometry."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = sum(1 for _ in reader)
    fields = [h.strip() for h in header]
    lower = {f.lower(): f for f in fields}

    # detect point columns
    lon_col = next((lower[k] for k in LON_NAMES if k in lower), None)
    lat_col = next((lower[k] for k in LAT_NAMES if k in lower), None)
    wkt_col = next((lower[k] for k in WKT_NAMES if k in lower), None)

    if lon_col and lat_col:
        geom_kind, geom_cols = "point_xy", (lon_col, lat_col)
    elif wkt_col:
        geom_kind, geom_cols = "wkt", (wkt_col,)
    else:
        geom_kind, geom_cols = "none", ()

    return Source(
        name=path.stem.lower(),
        path=path,
        rows=rows,
        fields=fields,
        geom_kind=geom_kind,
        geom_cols=geom_cols,
    )


# ---------- pattern matching ----------
def match_sources(registry: dict[str, Source], pattern: str) -> list[Source]:
    """Match by exact name, wildcard (*, ?), or substring."""
    p = pattern.lower()
    # if no wildcard chars, allow substring match too
    if any(ch in p for ch in "*?["):
        return [s for s in registry.values() if fnmatch.fnmatch(s.name, p)]
    if p in registry:
        return [registry[p]]
    return [s for s in registry.values() if p in s.name]


# ---------- where-clause filter ----------
_WHERE_RE = re.compile(
    r"""^\s*([A-Za-z_][A-Za-z0-9_]*)   # column
        \s*(=|!=|>=|<=|>|<|like)\s*    # op
        (.+?)\s*$""",                  # value
    re.VERBOSE | re.IGNORECASE,
)


def _parse_where(expr: str) -> tuple[str, str, str]:
    m = _WHERE_RE.match(expr)
    if not m:
        raise ValueError(f"bad --where expression: {expr!r}")
    col, op, val = m.group(1), m.group(2).lower(), m.group(3).strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return col, op, val


def _coerce(a: str, b: str) -> tuple[Any, Any]:
    """Try numeric comparison, fall back to string."""
    try:
        return float(a), float(b)
    except ValueError:
        return a, b


def _row_matches(row: dict, col: str, op: str, val: str) -> bool:
    if col not in row:
        return False
    cell = row[col]
    if op == "like":
        return fnmatch.fnmatch(str(cell).lower(), val.lower())
    a, b = _coerce(str(cell), val)
    return {
        "=": a == b, "!=": a != b,
        ">": a > b, "<": a < b,
        ">=": a >= b, "<=": a <= b,
    }[op]


# ---------- feature streaming ----------
def _row_to_feature(row: dict, src: Source, fid: int) -> dict:
    geom = None
    props = dict(row)
    if src.geom_kind == "point_xy":
        lon_c, lat_c = src.geom_cols
        try:
            lon = float(row[lon_c]); lat = float(row[lat_c])
            geom = {"type": "Point", "coordinates": [lon, lat]}
        except (TypeError, ValueError):
            geom = None
    elif src.geom_kind == "wkt":
        wkt_str = row.get(src.geom_cols[0])
        geom = _parse_wkt(wkt_str)
        # drop the raw WKT from properties; geometry replaces it
        props.pop(src.geom_cols[0], None)
    return {"type": "Feature", "id": fid, "geometry": geom, "properties": props}


def _parse_wkt(wkt_str: str | None) -> dict | None:
    """Parse a WKT string to a GeoJSON-style geometry dict using shapely."""
    if not wkt_str or not _HAS_SHAPELY:
        return None
    try:
        g = _wkt.loads(wkt_str)
        if g.is_empty:
            return None
        # shapely returns tuples; json.dumps handles them, but normalize for cleanliness
        return json.loads(json.dumps(_shapely_mapping(g)))
    except Exception:
        return None


def _geom_bounds(geom: dict | None) -> tuple[float, float, float, float] | None:
    """Compute bbox of a GeoJSON geometry. Uses shapely if available."""
    if geom is None:
        return None
    if geom.get("type") == "Point":
        x, y = geom["coordinates"][0], geom["coordinates"][1]
        return (x, y, x, y)
    if not _HAS_SHAPELY:
        return None
    try:
        from shapely.geometry import shape
        return shape(geom).bounds
    except Exception:
        return None



def _iter_rows(src: Source) -> Iterator[dict]:
    with src.path.open(newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


# ---------- handlers ----------
HELP_TEXT = __doc__


def _cmd_help() -> str:
    return HELP_TEXT


def _cmd_src(registry: dict[str, Source], args: list[str]) -> str:
    show_all = "-all" in args
    pattern = None
    if "-s" in args:
        i = args.index("-s")
        if i + 1 >= len(args):
            return "error: -s requires a pattern"
        pattern = args[i + 1]

    sources = match_sources(registry, pattern) if pattern else list(registry.values())

    if not sources:
        return f"no sources match {pattern!r}" if pattern else \
               f"no sources found in {SOURCES_DIR}/ — drop .csv files there"

    if show_all:
        return json.dumps(
            [{"name": s.name, "rows": s.rows, "fields": s.fields,
              "geometry": s.geom_kind, "geom_cols": list(s.geom_cols),
              "endpoint": f"-get {s.name}"} for s in sources],
            indent=2,
        )
    # compact
    lines = [f"{s.name:<30} {s.rows:>8} rows  geom={s.geom_kind}" for s in sources]
    return "\n".join(lines)


def _cmd_sch(registry: dict[str, Source], args: list[str]) -> str:
    if "-s" in args:
        i = args.index("-s")
        if i + 1 >= len(args):
            return "error: -s requires a pattern"
        sources = match_sources(registry, args[i + 1])
    elif args and not args[0].startswith("-"):
        sources = match_sources(registry, args[0])
    else:
        return "usage: -sch <source>  |  -sch -s <pattern>"

    if not sources:
        return "no matching sources"

    out = []
    for s in sources:
        out.append({
            "source": s.name,
            "rows": s.rows,
            "geometry": s.geom_kind,
            "geom_cols": list(s.geom_cols),
            "fields": [{"name": f, "type": _guess_type(s, f)} for f in s.fields],
        })
    return json.dumps(out if len(out) > 1 else out[0], indent=2)


def _guess_type(src: Source, field: str) -> str:
    """Sample first 50 rows to guess type."""
    seen_int = seen_float = seen_other = 0
    for i, row in enumerate(_iter_rows(src)):
        if i >= 50:
            break
        v = (row.get(field) or "").strip()
        if not v:
            continue
        try:
            int(v); seen_int += 1; continue
        except ValueError:
            pass
        try:
            float(v); seen_float += 1; continue
        except ValueError:
            pass
        seen_other += 1
    if seen_other == 0 and seen_int > 0 and seen_float == 0:
        return "integer"
    if seen_other == 0 and (seen_int + seen_float) > 0:
        return "number"
    return "string"


def _cmd_get(registry: dict[str, Source], args: list[str]) -> str:
    if not args or args[0].startswith("-"):
        return "usage: -get <source> [--bbox ...] [--limit N] [--where ...] [--fields ...] [--f geojson|json|csv] [--id v]"

    name = args[0].lower()
    if name not in registry:
        # try fuzzy
        matches = match_sources(registry, name)
        if len(matches) == 1:
            src = matches[0]
        else:
            return f"unknown source {name!r}. try -src"
    else:
        src = registry[name]

    # parse flags
    opts = _parse_get_flags(args[1:])

    # default format
    fmt = opts.get("f") or ("geojson" if src.geom_kind != "none" else "json")

    # --- filtering pipeline ---
    rows_iter = _iter_rows(src)

    # --id short-circuit (id == row index in 1..N, OR matching first column)
    if opts.get("id") is not None:
        target = opts["id"]
        id_col = src.fields[0]
        for i, row in enumerate(rows_iter, start=1):
            if str(i) == target or str(row.get(id_col)) == target:
                feat = _row_to_feature(row, src, i)
                if fmt == "esrijson":
                    return json.dumps(_to_esrijson(src, [feat], 1), indent=2)
                if fmt == "json":
                    return json.dumps(feat["properties"], indent=2)
                if fmt == "csv":
                    cols = src.fields
                    p = feat["properties"]
                    return ",".join(cols) + "\n" + ",".join(_csv_escape(p.get(c, "")) for c in cols)
                return json.dumps(feat, indent=2)
        return f"id {target!r} not found"

    # where
    where = None
    if opts.get("where"):
        where = _parse_where(opts["where"])

    # bbox (only valid for point geometry)
    bbox = opts.get("bbox")
    if bbox:
        try:
            bbox_t = tuple(float(x) for x in bbox.split(","))
            if len(bbox_t) != 4:
                raise ValueError
        except ValueError:
            return "bad --bbox; expected minx,miny,maxx,maxy"
    else:
        bbox_t = None

    # fields projection
    fields = None
    if opts.get("fields"):
        fields = [f.strip() for f in opts["fields"].split(",")]

    limit = min(int(opts.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)
    offset = int(opts.get("offset") or 0)

    # collect
    features = []
    matched = 0
    for i, row in enumerate(rows_iter, start=1):
        if where and not _row_matches(row, *where):
            continue
        if bbox_t:
            if src.geom_kind == "point_xy":
                try:
                    lon = float(row[src.geom_cols[0]]); lat = float(row[src.geom_cols[1]])
                except (TypeError, ValueError):
                    continue
                if not (bbox_t[0] <= lon <= bbox_t[2] and bbox_t[1] <= lat <= bbox_t[3]):
                    continue
            elif src.geom_kind == "wkt":
                gb = _geom_bounds(_parse_wkt(row.get(src.geom_cols[0])))
                if gb is None:
                    continue
                # bbox intersection test
                if not (gb[2] >= bbox_t[0] and gb[0] <= bbox_t[2]
                        and gb[3] >= bbox_t[1] and gb[1] <= bbox_t[3]):
                    continue
            else:
                continue  # bbox on tabular source returns nothing
        matched += 1
        if matched <= offset:
            continue
        if len(features) >= limit:
            # keep counting matched for numberMatched, but cap features
            continue
        feat = _row_to_feature(row, src, i)
        if fields is not None:
            feat["properties"] = {k: feat["properties"].get(k) for k in fields}
        features.append(feat)

    # --- serialize ---
    if fmt == "csv":
        if not features:
            return ""
        cols = fields or src.fields
        lines = [",".join(cols)]
        for feat in features:
            p = feat["properties"]
            lines.append(",".join(_csv_escape(p.get(c, "")) for c in cols))
        return "\n".join(lines)

    if fmt == "json":
        return json.dumps(
            {"source": src.name, "numberMatched": matched,
             "numberReturned": len(features),
             "features": [f["properties"] for f in features]},
            indent=2,
        )

    if fmt == "esrijson":
        return json.dumps(_to_esrijson(src, features, matched), indent=2)

    # geojson (OGC API - Features style)
    return json.dumps({
        "type": "FeatureCollection",
        "source": src.name,
        "numberMatched": matched,
        "numberReturned": len(features),
        "features": features,
    }, indent=2)


def _csv_escape(v: Any) -> str:
    s = "" if v is None else str(v)
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


# ---------- EsriJSON ----------
# Maps GeoJSON geometry types -> Esri "geometryType" + per-feature shape.
# Reference: ArcGIS REST API FeatureSet & Geometry Objects.
_ESRI_GEOM_TYPE = {
    "Point": "esriGeometryPoint",
    "MultiPoint": "esriGeometryMultipoint",
    "LineString": "esriGeometryPolyline",
    "MultiLineString": "esriGeometryPolyline",
    "Polygon": "esriGeometryPolygon",
    "MultiPolygon": "esriGeometryPolygon",
}

_ESRI_FIELD_TYPE = {
    "integer": "esriFieldTypeInteger",
    "number": "esriFieldTypeDouble",
    "string": "esriFieldTypeString",
}


def _geojson_to_esri(geom: dict | None) -> dict | None:
    """Convert a GeoJSON geometry dict to an Esri geometry dict (WGS84 assumed)."""
    if not geom:
        return None
    t = geom.get("type")
    coords = geom.get("coordinates")
    sr = {"spatialReference": {"wkid": 4326}}
    if t == "Point":
        return {"x": coords[0], "y": coords[1], **sr}
    if t == "MultiPoint":
        return {"points": [list(c) for c in coords], **sr}
    if t == "LineString":
        return {"paths": [[list(c) for c in coords]], **sr}
    if t == "MultiLineString":
        return {"paths": [[list(c) for c in line] for line in coords], **sr}
    if t == "Polygon":
        # GeoJSON: [exterior_ring, hole, hole...]; Esri: same array of rings,
        # but Esri rings must be clockwise for exterior, counter-clockwise for holes.
        # We don't enforce winding here — most consumers tolerate either.
        return {"rings": [[list(c) for c in ring] for ring in coords], **sr}
    if t == "MultiPolygon":
        rings = []
        for poly in coords:
            for ring in poly:
                rings.append([list(c) for c in ring])
        return {"rings": rings, **sr}
    return None  # GeometryCollection etc. not supported in Esri FeatureSet


def _to_esrijson(src: "Source", features: list[dict], matched: int) -> dict:
    """Build an Esri FeatureSet from already-collected GeoJSON features."""
    # pick geometryType from first feature with a geometry
    geom_type = None
    for f in features:
        g = f.get("geometry")
        if g and g.get("type") in _ESRI_GEOM_TYPE:
            geom_type = _ESRI_GEOM_TYPE[g["type"]]
            break

    # field defs (skip geometry source columns)
    skip = set(src.geom_cols)
    field_defs = []
    for fname in src.fields:
        if fname in skip:
            continue
        ftype = _guess_type(src, fname)
        field_defs.append({
            "name": fname,
            "type": _ESRI_FIELD_TYPE.get(ftype, "esriFieldTypeString"),
            "alias": fname,
        })

    esri_features = []
    for f in features:
        attrs = {k: v for k, v in f["properties"].items() if k not in skip}
        attrs["OBJECTID"] = f["id"]
        item = {"attributes": attrs}
        eg = _geojson_to_esri(f.get("geometry"))
        if eg is not None:
            item["geometry"] = eg
        esri_features.append(item)

    out: dict = {
        "objectIdFieldName": "OBJECTID",
        "globalIdFieldName": "",
        "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"}] + field_defs,
        "features": esri_features,
        "exceededTransferLimit": len(features) < matched,
    }
    if geom_type:
        out["geometryType"] = geom_type
        out["spatialReference"] = {"wkid": 4326}
    return out


def _parse_get_flags(tokens: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                opts[key] = tokens[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1
    return opts


# ---------- dispatcher ----------
def run(command: str) -> str:
    """Parse a command-line-style string and return the response text."""
    if not command or not command.strip():
        return _cmd_help()
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return f"parse error: {e}"

    # normalize: first token must be a verb
    verb, args = tokens[0], tokens[1:]
    registry = discover_sources()

    if verb in ("-help", "--help", "-h", "help"):
        return _cmd_help()
    if verb == "-src":
        return _cmd_src(registry, args)
    if verb == "-sch":
        return _cmd_sch(registry, args)
    if verb == "-get":
        return _cmd_get(registry, args)
    # ontology commands (delegated to frf_ont)
    if verb in ("-ont", "-rel", "-anom", "-explain"):
        try:
            import frf_ont
        except ImportError as e:
            return f"ontology engine unavailable: {e}"
        if verb == "-ont":
            return frf_ont.cmd_ont(args)
        if verb == "-rel":
            return frf_ont.cmd_rel(args)
        if verb == "-anom":
            return frf_ont.cmd_anom(args)
        if verb == "-explain":
            return frf_ont.cmd_explain(args)
    return f"unknown command {verb!r}. try -help"


# ---------- MCP wiring ----------
def _serve_mcp() -> None:
    """Expose `run` as an MCP tool over stdio."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("install mcp first:  pip install mcp\n")
        sys.exit(1)

    mcp = FastMCP("frf")

    @mcp.tool()
    def frf(command: str) -> str:
        """Run an FRF command. Try '-help' to see all commands."""
        return run(command)

    mcp.run()


# ---------- CLI for local testing ----------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        _serve_mcp()
    else:
        # Treat all argv as one command for quick testing
        cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "-help"
        print(run(cmd))
