#!/usr/bin/env python3
"""
FRF Identity Registry — Stable Identifiers and Name Drift Signaling.

Issues, retains, and resolves stable `(item, uuid)` pairs for sources and views.
Implements the proposed OGC API extension: dual-key resolution with name drift,
tombstoning, and number-wins-on-disagreement.

Persistence: views/_registry.yaml is the authoritative item book.
Sources and their Core views share an item — they're the same logical thing
at two tiers (the data, and its faithful mirror).
"""

from __future__ import annotations

import threading
import uuid as uuid_lib
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


REGISTRY_PATH = Path(__file__).parent / "views" / "_registry.yaml"

# Sentinel returned when an identifier is tombstoned (deleted, never reissued).
TOMBSTONED = object()


@dataclass
class Entry:
    """One row in the identity registry."""
    item: int
    uuid: str
    name: str            # current canonical name
    kind: str            # "source" | "view"
    aliases: list[str] = field(default_factory=list)
    deleted: bool = False
    deleted_at: str = ""

    def to_dict(self) -> dict:
        d = {"item": self.item, "uuid": self.uuid, "name": self.name, "kind": self.kind}
        if self.aliases:
            d["aliases"] = self.aliases
        if self.deleted:
            d["deleted"] = True
            d["deleted_at"] = self.deleted_at
        return d


@dataclass
class ResolveResult:
    """Outcome of resolving (name, item, uuid) against the registry."""
    entry: Entry | None = None
    tombstoned: bool = False
    drift: list[str] = field(default_factory=list)   # which identifiers drifted
    requested_name: str = ""
    requested_item: int | None = None
    requested_uuid: str = ""

    @property
    def has_drift(self) -> bool:
        return bool(self.drift) and self.entry is not None

    def warning_message(self) -> str:
        """Build the human-readable warning for HTTP header + body."""
        if not self.has_drift or self.entry is None:
            return ""
        parts = []
        if "name" in self.drift and self.requested_name:
            parts.append(f"requested name {self.requested_name!r} resolved via "
                          f"{'item' if 'name' not in [d for d in self.drift if d!='name'] else 'durable id'} "
                          f"to current name {self.entry.name!r}")
        elif "item" in self.drift:
            parts.append(f"item {self.requested_item} disagreed with name "
                          f"{self.requested_name!r}; served by item")
        elif "uuid" in self.drift:
            parts.append(f"uuid disagreed with name; served by uuid")
        return "; ".join(parts) or "name drift detected"


class Registry:
    """Thread-safe identity registry persisted to YAML.

    All mutating ops (register, rename, delete) are atomic w.r.t. the file.
    Reads do not lock — registry is loaded once into memory and kept in sync
    on writes. For a small/medium deployment this is fine; for larger we'd
    swap in SQLite without changing the API.
    """

    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._next_item: int = 1
        # primary store: uuid -> Entry  (uuid is globally unique always)
        self._entries: dict[str, Entry] = {}
        # secondary indices, rebuilt on every change
        self._by_name: dict[str, Entry] = {}      # current canonical names only
        self._by_alias: dict[str, Entry] = {}     # alias -> entry (live only)
        self._items_in_use: set[int] = set()      # items that have been issued
        self._tombstoned_items: set[int] = set()  # items on dead entries
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        with self._lock:
            self._entries.clear()
            if not self.path.exists() or not _HAS_YAML:
                self._next_item = 1
                self._reindex()
                return
            data = yaml.safe_load(self.path.read_text()) or {}
            self._next_item = int(data.get("next_item", 1))
            for raw in data.get("entries", []):
                e = Entry(
                    item=int(raw["item"]),
                    uuid=str(raw["uuid"]),
                    name=str(raw["name"]),
                    kind=str(raw.get("kind", "view")),
                    aliases=list(raw.get("aliases", [])),
                    deleted=bool(raw.get("deleted", False)),
                    deleted_at=str(raw.get("deleted_at", "")),
                )
                self._entries[e.uuid] = e
            self._reindex()

    def _save(self) -> None:
        if not _HAS_YAML:
            return
        self.path.parent.mkdir(exist_ok=True)
        # sort by (item, uuid) so output is stable
        ordered = sorted(self._entries.values(), key=lambda e: (e.item, e.uuid))
        data = {
            "next_item": self._next_item,
            "entries": [e.to_dict() for e in ordered],
        }
        self.path.write_text(yaml.safe_dump(data, sort_keys=False))

    def _reindex(self) -> None:
        self._by_name = {}
        self._by_alias = {}
        self._items_in_use = set()
        self._tombstoned_items = set()
        for e in self._entries.values():
            self._items_in_use.add(e.item)
            if e.deleted:
                self._tombstoned_items.add(e.item)
                continue
            self._by_name[e.name] = e
            for a in e.aliases:
                self._by_alias[a] = e

    # ---------- registration ----------
    def register(self, name: str, kind: str = "view",
                 aliases: list[str] | None = None,
                 share_item_with: int | None = None) -> Entry:
        """Issue a new (item, uuid) for `name`. If `share_item_with` is set,
        reuse that item number — used when a Core view shares the source's
        item. The uuid is still freshly issued for the view."""
        with self._lock:
            if name in self._by_name:
                raise ValueError(
                    f"name {name!r} already registered "
                    f"(item={self._by_name[name].item})"
                )
            if aliases:
                for a in aliases:
                    if a in self._by_name or a in self._by_alias:
                        raise ValueError(f"alias {a!r} already in use")

            if share_item_with is not None:
                # Verify the partner item exists and is live; share its number.
                if share_item_with not in self._items_in_use:
                    raise ValueError(
                        f"cannot share item {share_item_with}: not registered"
                    )
                if share_item_with in self._tombstoned_items:
                    # Edge case: every entry for this item is tombstoned.
                    # Refuse — sharing with a dead item creates a zombie.
                    if not any(not e.deleted for e in self._entries.values()
                                if e.item == share_item_with):
                        raise ValueError(
                            f"cannot share item {share_item_with}: all tombstoned"
                        )
                item = share_item_with
            else:
                item = self._next_item
                self._next_item += 1

            new_uuid = str(uuid_lib.uuid4())
            e = Entry(item=item, uuid=new_uuid, name=name, kind=kind,
                       aliases=list(aliases or []))
            self._entries[new_uuid] = e
            self._reindex()
            self._save()
            return e

    def rename(self, item: int, new_name: str) -> Entry:
        """Change the canonical name of an entry. Old name does NOT become
        an alias automatically — operator must add it explicitly if desired."""
        with self._lock:
            e = self._lookup_item_live(item)
            if e is None:
                raise KeyError(f"no live entry with item={item}")
            if new_name in self._by_name and self._by_name[new_name] is not e:
                raise ValueError(f"name {new_name!r} already in use")
            e.name = new_name
            self._reindex()
            self._save()
            return e

    def add_alias(self, item: int, alias: str) -> Entry:
        with self._lock:
            e = self._lookup_item_live(item)
            if e is None:
                raise KeyError(f"no live entry with item={item}")
            if alias in self._by_name or alias in self._by_alias:
                raise ValueError(f"alias {alias!r} already in use")
            if alias not in e.aliases:
                e.aliases.append(alias)
            self._reindex()
            self._save()
            return e

    def delete(self, item: int, when: str) -> Entry:
        """Tombstone an entry. The item and uuid are retained forever; the
        name and aliases are released for reuse by other resources.
        If multiple entries share an item (source + core view), tombstone all."""
        with self._lock:
            siblings = [e for e in self._entries.values() if e.item == item]
            if not siblings:
                raise KeyError(f"no entry with item={item}")
            target = None
            for e in siblings:
                if not e.deleted:
                    e.deleted = True
                    e.deleted_at = when
                    e.aliases = []
                    target = e
            if target is None:
                # all siblings already tombstoned; return the last one
                target = siblings[-1]
            self._reindex()
            self._save()
            return target

    # ---------- lookup ----------
    def _lookup_item_live(self, item: int) -> Entry | None:
        """First live entry sharing this item number, or None."""
        for e in self._entries.values():
            if e.item == item and not e.deleted:
                return e
        return None

    def lookup_by_item(self, item: int) -> Entry | None:
        return self._lookup_item_live(item)

    def lookup_by_uuid(self, uid: str) -> Entry | None:
        return self._entries.get(uid)

    def lookup_by_name(self, name: str) -> Entry | None:
        e = self._by_name.get(name)
        if e is not None:
            return e
        return self._by_alias.get(name)

    def all_entries(self, include_deleted: bool = False) -> list[Entry]:
        out = [e for e in self._entries.values()
                if include_deleted or not e.deleted]
        return sorted(out, key=lambda e: (e.item, e.uuid))

    # ---------- the dual-key resolver (the heart of the proposal) ----------
    def resolve(self, name: str | None = None,
                item: int | None = None,
                uid: str | None = None) -> ResolveResult:
        """Resolve (name, item, uuid) per the proposal:

        - tombstone wins over not-found (410 vs 404)
        - durable id (uuid > item) wins over name on disagreement
        - drift is reported when name resolves to a different entry than id
        - if only some keys provided, missing keys are skipped
        """
        result = ResolveResult(
            requested_name=name or "",
            requested_item=item,
            requested_uuid=uid or "",
        )

        candidates: dict[str, Entry | object | None] = {}
        if name is not None:
            candidates["name"] = self.lookup_by_name(name)
        if item is not None:
            cand = self._lookup_item_live(item)
            if cand is None and item in self._tombstoned_items:
                candidates["item"] = TOMBSTONED
            else:
                candidates["item"] = cand
        if uid is not None:
            cand = self.lookup_by_uuid(uid)
            if cand is not None and cand.deleted:
                candidates["uuid"] = TOMBSTONED
            else:
                candidates["uuid"] = cand

        # All tombstoned with no live → 410 Gone
        any_tombstoned = any(c is TOMBSTONED for c in candidates.values())
        any_live = any(isinstance(c, Entry) and not c.deleted
                        for c in candidates.values())
        if any_tombstoned and not any_live:
            result.tombstoned = True
            return result

        # No candidates → not found
        if not any_live:
            return result

        # Pick most durable: uuid > item > name
        chosen: Entry | None = None
        for kind in ("uuid", "item", "name"):
            c = candidates.get(kind)
            if isinstance(c, Entry) and not c.deleted:
                chosen = c
                break
        if chosen is None:
            return result

        result.entry = chosen

        # Drift detection: any other resolved candidate that's a different entry
        for kind, c in candidates.items():
            if isinstance(c, Entry) and not c.deleted and c.uuid != chosen.uuid:
                result.drift.append(kind)
            if isinstance(c, Entry) and c.uuid == chosen.uuid:
                # Same entry — but did the name match the *current* name?
                if kind == "name" and name and name != chosen.name and \
                        name in chosen.aliases:
                    # alias used — that's fine, no drift signal needed
                    pass
                elif kind == "name" and name and name != chosen.name:
                    # name lookup hit via aliases or fuzzy somehow
                    result.drift.append("name")

        # Special case: name didn't resolve at all but item/uuid did. That's
        # also a drift signal — the requested name no longer points anywhere.
        name_cand = candidates.get("name")
        if name and name_cand is None and chosen.name != name:
            if "name" not in result.drift:
                result.drift.append("name")

        return result


# ---------- module-level singleton ----------
_global_registry: Registry | None = None


def get_registry() -> Registry:
    """Return the process-wide Registry instance.

    Reads `REGISTRY_PATH` at call time (not import time) so tests and demos
    that monkey-patch the path before first call get the right backing file.
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = Registry(path=REGISTRY_PATH)
    return _global_registry


def reset_registry_for_tests() -> None:
    """Test helper: drop the singleton so a fresh registry loads."""
    global _global_registry
    _global_registry = None
