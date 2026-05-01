#!/usr/bin/env python3
"""
FRF Ontology Engine — EES-A-R views over the source registry.

Concepts
--------
View         A named collection derived from one or more sources, classified
             with an EES-A-R category (Entity, Event, State, Artifact, Relation).
             Either declared in views/*.yaml or auto-promoted from a CSV that
             carries its own classification columns.

Categories   The five-category framework: Entity (a thing), Event (a happening),
             State (a condition at a time), Artifact (a representation/document),
             Relation (a link between things).

Provenance   Every output field knows where it came from. With ?explain=true
             on the HTTP API or `--explain` on the MCP, each feature carries a
             trail of source.column -> view.field for every property.

Constraints  Per-view validation rules. Rows that violate a constraint feed
             an automatically-generated anomaly view, parallel to the main one,
             so data quality is itself an ontological view.

Self-promote A CSV with `eesr_type` / `domain_type` columns becomes a view
             without any YAML. The data declares its own classification.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

import frf_mcp as engine


# ---------- config ----------
VIEWS_DIR = Path(__file__).parent / "views"

CATEGORIES = ("Entity", "Event", "State", "Artifact", "Relation")

# Columns that, when present in a CSV, mark it as self-describing.
SELF_DESCRIBE_CATEGORY_COLS = ("eesr_type", "eesr_category", "ontology_type")
SELF_DESCRIBE_DOMAIN_COLS = ("domain_type", "kind", "subtype")


# ---------- view model ----------
@dataclass
class Constraint:
    """A per-view validation rule. Violations form an anomaly view."""
    name: str
    expr: str          # e.g. "station_name not endswith 'Branch'"
    description: str = ""

    def evaluate(self, row: dict) -> bool:
        """Return True if the row satisfies the constraint (i.e., is OK)."""
        return _eval_constraint(self.expr, row)


@dataclass
class Relation:
    """A relation from this view to another."""
    name: str          # e.g. "branch"
    target: str        # name of target view/source
    on: str            # column in this view that links
    target_on: str = ""  # column in target (defaults to same name)
    kind: str = "many_to_one"  # many_to_one, one_to_many, many_to_many


@dataclass
class FieldMap:
    """A projected field with provenance."""
    out_name: str
    source: str        # source name
    source_col: str    # column in source
    derivation: str = "direct"   # direct, computed, derived_from_column, joined


@dataclass
class View:
    """An ontological view. Has a category, a primary source, projections,
    relations, and (optionally) constraints. Can be virtual (derived from
    a column rather than a file)."""
    name: str
    category: str
    domain_type: str = ""
    description: str = ""
    primary: str = ""              # name of primary source (or another view)
    fields: list[FieldMap] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    virtual_from: dict | None = None   # {source, column, dedupe_on} for derived views
    geometry_from: tuple[str, str] | None = None  # (source, col_or_pair)
    geom_kind: str = "none"        # point_xy | wkt | none
    geom_cols: tuple[str, ...] = ()
    source_path: str = ""          # YAML file or "auto-promoted"


# ---------- constraint evaluator ----------
_CONSTRAINT_RE = re.compile(
    r"""^\s*([A-Za-z_][A-Za-z0-9_]*)               # column
        \s+(not\s+)?
        (endswith|startswith|contains|matches|equals|in|gt|lt|ge|le|ne|eq|nonempty|empty)
        (?:\s+(.+))?\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def _eval_constraint(expr: str, row: dict) -> bool:
    """Evaluate a constraint expression against a row. True = satisfies."""
    m = _CONSTRAINT_RE.match(expr)
    if not m:
        # fall back to FRF where-clause syntax
        try:
            col, op, val = engine._parse_where(expr)
            return engine._row_matches(row, col, op, val)
        except Exception:
            return True  # malformed → don't flag
    col, neg, op, val = m.group(1), bool(m.group(2)), m.group(3).lower(), m.group(4)
    if val:
        val = val.strip().strip('"').strip("'")
    cell = str(row.get(col, "") or "")
    result = _apply_op(cell, op, val)
    return (not result) if neg else result


def _apply_op(cell: str, op: str, val: str | None) -> bool:
    if op == "endswith":
        return cell.endswith(val or "")
    if op == "startswith":
        return cell.startswith(val or "")
    if op == "contains":
        return (val or "") in cell
    if op == "matches":
        try:
            return bool(re.search(val or "", cell))
        except re.error:
            return False
    if op in ("equals", "eq"):
        return cell == (val or "")
    if op == "ne":
        return cell != (val or "")
    if op == "in":
        opts = [v.strip() for v in (val or "").split(",")]
        return cell in opts
    if op == "nonempty":
        return cell.strip() != ""
    if op == "empty":
        return cell.strip() == ""
    if op in ("gt", "lt", "ge", "le"):
        try:
            a, b = float(cell), float(val or "0")
        except ValueError:
            return False
        return {"gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b}[op]
    return True


# ---------- view discovery ----------
def discover_views() -> dict[str, View]:
    """Return name -> View for all views. Two sources:
       1. YAML files in views/
       2. Auto-promoted from self-describing CSVs in sources/
    YAML wins on name conflict.
    Also auto-registers each view in the identity registry."""
    views: dict[str, View] = {}

    # 1. Auto-promote self-describing CSVs
    registry = engine.discover_sources()
    for src in registry.values():
        v = _auto_promote(src)
        if v:
            views[v.name] = v

    # 2. Load YAML views (override autos with same name, register new)
    if VIEWS_DIR.exists() and _HAS_YAML:
        for yml in sorted(VIEWS_DIR.glob("*.yaml")):
            if yml.name.startswith("_"):  # _registry.yaml etc. are not views
                continue
            try:
                v = _load_yaml_view(yml, registry)
                views[v.name] = v
            except Exception as e:
                import sys
                sys.stderr.write(f"[frf-ont] skipping {yml.name}: {e}\n")

    # Auto-register views in the identity registry.
    # Core view convention: name ends with "_core_view" → shares its primary's item.
    try:
        import frf_registry
        idreg = frf_registry.get_registry()
        for v in views.values():
            if idreg.lookup_by_name(v.name) is not None:
                continue  # already registered
            share_item = None
            if v.name.endswith("_core_view") and v.primary in registry:
                src_entry = idreg.lookup_by_name(v.primary)
                if src_entry is not None and not src_entry.deleted:
                    share_item = src_entry.item
            try:
                idreg.register(v.name, kind="view", share_item_with=share_item)
            except ValueError:
                pass  # collision — skip
    except ImportError:
        pass

    return views


def _auto_promote(src: engine.Source) -> View | None:
    """If a CSV has eesr_type/domain_type columns, build a View for it."""
    cat_col = next((c for c in src.fields if c.lower() in SELF_DESCRIBE_CATEGORY_COLS), None)
    if not cat_col:
        return None

    # peek first row to grab category + domain
    rows = list(_take(engine._iter_rows(src), 1))
    if not rows:
        return None
    first = rows[0]
    category = (first.get(cat_col) or "").strip().capitalize()
    if category not in CATEGORIES:
        return None

    dom_col = next((c for c in src.fields if c.lower() in SELF_DESCRIBE_DOMAIN_COLS), None)
    domain_type = (first.get(dom_col) or "").strip() if dom_col else ""

    # all columns become projected fields with direct provenance
    fields = [FieldMap(out_name=c, source=src.name, source_col=c) for c in src.fields]

    # carry geometry detection
    geom_kind, geom_cols = src.geom_kind, src.geom_cols

    return View(
        name=src.name,
        category=category,
        domain_type=domain_type,
        description=f"Auto-promoted from {src.path.name} (self-describing via {cat_col!r})",
        primary=src.name,
        fields=fields,
        geom_kind=geom_kind,
        geom_cols=geom_cols,
        source_path="auto-promoted",
    )


def _load_yaml_view(yml_path: Path, registry: dict) -> View:
    """Parse a views/*.yaml file into a View."""
    data = yaml.safe_load(yml_path.read_text())
    if not isinstance(data, dict) or "name" not in data or "category" not in data:
        raise ValueError("missing required 'name' or 'category'")
    cat = str(data["category"]).strip().capitalize()
    if cat not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}; got {cat!r}")

    primary = data.get("primary", "")
    fields_spec = data.get("fields") or []
    fields: list[FieldMap] = []
    for spec in fields_spec:
        if isinstance(spec, str):
            # "col" or "col as alias"
            if " as " in spec:
                src_part, alias = [p.strip() for p in spec.split(" as ", 1)]
            else:
                src_part, alias = spec.strip(), spec.strip()
            if "." in src_part:
                src_name, col = src_part.split(".", 1)
            else:
                src_name, col = primary, src_part
            fields.append(FieldMap(out_name=alias, source=src_name, source_col=col))
        elif isinstance(spec, dict):
            fields.append(FieldMap(
                out_name=spec["name"],
                source=spec.get("source", primary),
                source_col=spec.get("column", spec["name"]),
                derivation=spec.get("derivation", "direct"),
            ))

    relations = []
    for r in (data.get("relations") or []):
        # PyYAML 1.1 interprets bare 'on' as a boolean True, so the key may
        # come back as True instead of 'on'. Look for both.
        on_val = r.get("on", r.get(True))
        target_on = r.get("target_on", on_val)
        relations.append(Relation(
            name=r["name"], target=r["target"], on=on_val,
            target_on=target_on,
            kind=r.get("kind", "many_to_one"),
        ))

    constraints = [
        Constraint(name=c["name"], expr=c["expr"], description=c.get("description", ""))
        for c in (data.get("constraints") or [])
    ]

    # virtual derivation
    virtual_from = data.get("virtual_from")  # {source, column, dedupe_on?}

    # geometry
    geom_spec = data.get("geometry")
    geom_kind = "none"
    geom_cols: tuple[str, ...] = ()
    if geom_spec:
        if "lon" in geom_spec and "lat" in geom_spec:
            geom_kind, geom_cols = "point_xy", (geom_spec["lon"], geom_spec["lat"])
        elif "wkt" in geom_spec:
            geom_kind, geom_cols = "wkt", (geom_spec["wkt"],)
    elif primary in registry:
        # inherit from primary source
        geom_kind = registry[primary].geom_kind
        geom_cols = registry[primary].geom_cols

    return View(
        name=data["name"],
        category=cat,
        domain_type=data.get("domain_type", ""),
        description=data.get("description", ""),
        primary=primary,
        fields=fields,
        relations=relations,
        constraints=constraints,
        virtual_from=virtual_from,
        geom_kind=geom_kind,
        geom_cols=geom_cols,
        source_path=str(yml_path),
    )


def _take(it: Iterator, n: int) -> Iterator:
    for i, x in enumerate(it):
        if i >= n:
            return
        yield x


# ---------- view materialization ----------
def materialize(view: View, registry: dict[str, engine.Source]) -> Iterator[dict]:
    """Yield rows for the view. Handles direct, virtual, and joined views."""
    if view.virtual_from:
        yield from _materialize_virtual(view, registry)
    else:
        yield from _materialize_direct(view, registry)


def _materialize_direct(view: View, registry: dict[str, engine.Source]) -> Iterator[dict]:
    """For views backed by a single primary source: read rows, project fields."""
    if view.primary not in registry:
        return
    src = registry[view.primary]
    for row in engine._iter_rows(src):
        out = {}
        for fm in view.fields:
            if fm.source == view.primary:
                out[fm.out_name] = row.get(fm.source_col)
        # if no fields declared, pass through whole row
        if not view.fields:
            out = dict(row)
        yield out


def _materialize_virtual(view: View, registry: dict[str, engine.Source]) -> Iterator[dict]:
    """Derive rows from unique values of a column in another source.
       Example: branches view derived from stations.branch_primary."""
    spec = view.virtual_from
    src_name = spec["source"]
    col = spec["column"]
    if src_name not in registry:
        return
    src = registry[src_name]

    # Collect uniques + member counts + child rows for aggregation.
    groups: dict[str, list[dict]] = {}
    for row in engine._iter_rows(src):
        k = (row.get(col) or "").strip()
        if not k:
            continue
        groups.setdefault(k, []).append(row)

    # Build a row for each unique value.
    aggregations = spec.get("aggregations") or []
    id_field = spec.get("id_field", "id")
    name_field = spec.get("name_field", "name")
    for key, members in sorted(groups.items()):
        out = {id_field: _slugify(key), name_field: key, "member_count": len(members)}

        # carry over any agg specs: e.g. {field: county_borough, op: distinct, as: counties}
        for agg in aggregations:
            f = agg["field"]; op = agg.get("op", "distinct"); alias = agg.get("as", f)
            vals = [m.get(f) for m in members if m.get(f)]
            if op == "distinct":
                out[alias] = sorted(set(vals))
            elif op == "count":
                out[alias] = len(vals)
            elif op == "first":
                out[alias] = vals[0] if vals else None
        yield out


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ---------- anomaly detection ----------
def evaluate_constraints(view: View, registry: dict) -> list[dict]:
    """Return a list of anomaly rows for this view, one per (row, violated constraint).
    Each anomaly has the original row plus _violations describing what failed."""
    if not view.constraints:
        return []
    anomalies = []
    for i, row in enumerate(materialize(view, registry), start=1):
        violations = []
        for c in view.constraints:
            if not c.evaluate(row):
                violations.append({"name": c.name, "expr": c.expr,
                                    "description": c.description})
        if violations:
            anomalies.append({
                "_view": view.name, "_row_index": i,
                "_violations": violations, **row,
            })
    return anomalies


# ---------- explain trail ----------
def explain_feature(view: View, row: dict) -> dict:
    """Build a provenance trail for one feature: which source.column produced
    each output field. The 'schema realism' move made visible."""
    trail = []
    for fm in view.fields:
        trail.append({
            "field": fm.out_name,
            "source": fm.source,
            "source_column": fm.source_col,
            "derivation": fm.derivation,
        })
    if not view.fields:
        # whole-row passthrough
        for k in row.keys():
            trail.append({"field": k, "source": view.primary,
                           "source_column": k, "derivation": "direct"})
    return {
        "view": view.name,
        "category": view.category,
        "domain_type": view.domain_type,
        "primary_source": view.primary,
        "virtual": view.virtual_from is not None,
        "geometry_kind": view.geom_kind,
        "fields": trail,
    }


# ---------- relation resolution ----------
def find_related(view_name: str, row: dict, registry: dict,
                 views: dict[str, View]) -> dict[str, list[dict]]:
    """For each relation defined on `view_name`, find related rows in the target."""
    if view_name not in views:
        return {}
    view = views[view_name]
    out: dict[str, list[dict]] = {}
    for rel in view.relations:
        key_val = row.get(rel.on)
        if not key_val:
            continue
        target = views.get(rel.target)
        if not target:
            # fall back to source
            if rel.target in registry:
                target_rows = [r for r in engine._iter_rows(registry[rel.target])
                                if r.get(rel.target_on) == key_val]
                out[rel.name] = target_rows
            continue
        related = [r for r in materialize(target, registry)
                    if str(r.get(rel.target_on)) == str(key_val)]
        out[rel.name] = related
    return out


# ---------- MCP-side commands ----------
def cmd_ont(args: list[str]) -> str:
    """`-ont` and `-ont -s pattern`: list ontological views."""
    views = discover_views()
    pattern = None
    if "-s" in args:
        i = args.index("-s")
        if i + 1 >= len(args):
            return "error: -s requires a pattern"
        pattern = args[i + 1].lower()

    show_all = "-all" in args
    selected = list(views.values())
    if pattern:
        if any(ch in pattern for ch in "*?["):
            selected = [v for v in selected if fnmatch.fnmatch(v.name.lower(), pattern)
                         or fnmatch.fnmatch(v.category.lower(), pattern)]
        else:
            selected = [v for v in selected
                         if pattern in v.name.lower()
                         or pattern == v.category.lower()
                         or pattern in v.domain_type.lower()]

    if not selected:
        return "no matching views" if pattern else (
            "no views found. drop CSVs with eesr_type column into sources/, "
            "or YAML view definitions into views/"
        )

    if show_all:
        return json.dumps([{
            "name": v.name, "category": v.category, "domain_type": v.domain_type,
            "primary": v.primary, "virtual": v.virtual_from is not None,
            "fields": [fm.out_name for fm in v.fields],
            "relations": [{"name": r.name, "target": r.target, "on": r.on}
                           for r in v.relations],
            "constraints": [c.name for c in v.constraints],
            "source": v.source_path,
        } for v in selected], indent=2)

    lines = []
    for v in selected:
        rels = f" rels={len(v.relations)}" if v.relations else ""
        cons = f" constraints={len(v.constraints)}" if v.constraints else ""
        virt = " (virtual)" if v.virtual_from else ""
        dom = f"/{v.domain_type}" if v.domain_type else ""
        lines.append(f"{v.name:<28} {v.category:<10}{dom:<14}{virt}{rels}{cons}")
    return "\n".join(lines)


def cmd_rel(args: list[str]) -> str:
    """`-rel` (graph) or `-rel <view>` (relations from one view)."""
    views = discover_views()
    if not args or args[0].startswith("-"):
        # whole graph
        edges = []
        for v in views.values():
            for r in v.relations:
                edges.append({"from": v.name, "to": r.target,
                               "name": r.name, "on": r.on, "kind": r.kind})
        return json.dumps({"views": list(views.keys()), "edges": edges}, indent=2)
    name = args[0].lower()
    if name not in views:
        return f"unknown view {name!r}"
    v = views[name]
    return json.dumps([{"name": r.name, "target": r.target, "on": r.on,
                          "target_on": r.target_on, "kind": r.kind}
                         for r in v.relations], indent=2)


def cmd_anom(args: list[str]) -> str:
    """`-anom` (all views) or `-anom <view>` (one view)."""
    views = discover_views()
    registry = engine.discover_sources()
    target_views = list(views.values())
    if args and not args[0].startswith("-"):
        if args[0].lower() not in views:
            return f"unknown view {args[0]!r}"
        target_views = [views[args[0].lower()]]

    out = {}
    for v in target_views:
        if not v.constraints:
            continue
        anoms = evaluate_constraints(v, registry)
        if anoms:
            out[v.name] = anoms
    if not out:
        return "no anomalies (or no constraints defined)"
    return json.dumps(out, indent=2)


def cmd_explain(args: list[str]) -> str:
    """`-explain <view>`: show the provenance trail for a view's schema."""
    if not args or args[0].startswith("-"):
        return "usage: -explain <view>"
    views = discover_views()
    name = args[0].lower()
    if name not in views:
        return f"unknown view {name!r}"
    return json.dumps(explain_feature(views[name], {}), indent=2)


# ---------- view-aware feature getter (used by HTTP) ----------
def get_view_features(view_name: str, **opts) -> dict:
    """Materialize a view as a GeoJSON-style FeatureCollection.
    Used by frf_http for /collections/{view}/items.
    Supports: limit, offset, bbox, where, fields, explain."""
    views = discover_views()
    registry = engine.discover_sources()
    if view_name not in views:
        raise KeyError(view_name)
    view = views[view_name]

    limit = min(int(opts.get("limit") or engine.DEFAULT_LIMIT), engine.MAX_LIMIT)
    offset = int(opts.get("offset") or 0)
    where = engine._parse_where(opts["where"]) if opts.get("where") else None
    bbox = None
    if opts.get("bbox"):
        try:
            bbox = tuple(float(x) for x in opts["bbox"].split(","))
            if len(bbox) != 4:
                bbox = None
        except ValueError:
            bbox = None
    explain = bool(opts.get("explain"))
    fields = None
    if opts.get("fields"):
        fields = [f.strip() for f in opts["fields"].split(",")]

    features = []
    matched = 0
    explain_block = explain_feature(view, {}) if explain else None

    for i, row in enumerate(materialize(view, registry), start=1):
        if where and not engine._row_matches(row, *where):
            continue

        # geometry
        geom = _row_geometry(view, row)

        # bbox filter
        if bbox:
            gb = engine._geom_bounds(geom)
            if gb is None:
                continue
            if not (gb[2] >= bbox[0] and gb[0] <= bbox[2]
                    and gb[3] >= bbox[1] and gb[1] <= bbox[3]):
                continue

        matched += 1
        if matched <= offset:
            continue
        if len(features) >= limit:
            continue

        props = dict(row)
        if fields is not None:
            props = {k: props.get(k) for k in fields}

        feat = {
            "type": "Feature",
            "id": i,
            "geometry": geom,
            "properties": props,
            "frf:category": view.category,
            "frf:domainType": view.domain_type,
            "frf:view": view.name,
        }
        if explain:
            feat["frf:explain"] = explain_block
        features.append(feat)

    fc = {
        "type": "FeatureCollection",
        "view": view.name,
        "category": view.category,
        "domainType": view.domain_type,
        "numberMatched": matched,
        "numberReturned": len(features),
        "features": features,
        "frf:category": view.category,
    }
    if explain:
        fc["frf:explain"] = explain_block
    return fc


def _row_geometry(view: View, row: dict) -> dict | None:
    if view.geom_kind == "point_xy":
        try:
            lon = float(row[view.geom_cols[0]])
            lat = float(row[view.geom_cols[1]])
            return {"type": "Point", "coordinates": [lon, lat]}
        except (TypeError, ValueError, KeyError):
            return None
    if view.geom_kind == "wkt":
        return engine._parse_wkt(row.get(view.geom_cols[0]))
    return None
