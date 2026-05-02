#!/usr/bin/env python3
"""
FRF Identity Registry — Stable Identifiers and Name Drift Signaling.

Issues, retains, and resolves stable `(item, uuid)` triplets for sources and views.
Implements the proposed REST/OGC pattern (brief v1.3): name-first resolution
with durable UUID backups, optional name-history fallback returning 308 redirects,
and tombstoning with 410 Gone.

This is the reference implementation for the v1.3 brief. See:
  - §2 Proposal — name-first cascade (name → item → uuid)
  - §2.4 Optional Name-History Fallback (308 Redirect)
  - §2.5 Client Implementation Pattern

Persistence: views/_registry.yaml is the authoritative book.
Sources and their Core views share an item — they're the same logical thing
at two tiers (the data, and its faithful mirror).
"""

from __future__ import annotations

import threading
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


REGISTRY_PATH = Path(__file__).parent / "views" / "_registry.yaml"

# Sentinel returned when an identifier is tombstoned (deleted, never reissued).
TOMBSTONED = object()


def _now_iso() -> str:
    """Current UTC time in ISO 8601 format (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    """One row in the identity registry.

    Per brief v1.3:
      - item: server-instance UUID (durable per-instance, survives renames)
      - uuid: global UUID (durable across federated instances, survives migration)
      - name: current canonical name (mutable label, used in URI path)
    """
    item: str            # server-instance UUID (was int in v1.0)
    uuid: str            # global UUID
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
class NameHistoryEntry:
    """A record that `old_name` previously referred to a resource that is
    now reachable at the canonical entry identified by `item` (server UUID).

    Per brief §2.4: optional, doubly-policy-controlled (whether to maintain
    at all; how long to retain).
    """
    old_name: str
    canonical_item: str       # server-instance UUID of the current entry
    recorded_at: str          # ISO 8601 timestamp when the rename occurred
    expires_at: str = ""      # ISO 8601, "" = no expiration

    def to_dict(self) -> dict:
        d = {
            "old_name": self.old_name,
            "canonical_item": self.canonical_item,
            "recorded_at": self.recorded_at,
        }
        if self.expires_at:
            d["expires_at"] = self.expires_at
        return d

    def is_expired(self, now_iso: str | None = None) -> bool:
        """True if this entry has passed its retention window."""
        if not self.expires_at:
            return False
        return (now_iso or _now_iso()) >= self.expires_at


@dataclass
class ResolveResult:
    """Outcome of resolving (name, item, uuid) against the registry.

    Per brief v1.3 §2 (resolution cascade) and §2.4 (optional 308 redirect).
    """
    entry: Entry | None = None
    tombstoned: bool = False
    served_via: str = ""                 # "name" | "item" | "uuid" | "name_history"
    drift_from: str | None = None        # the requested name that drifted
    redirect_to_name: str | None = None  # set when name-history fired (308)
    requested_name: str = ""
    requested_item: str = ""
    requested_uuid: str = ""

    @property
    def is_redirect(self) -> bool:
        """True when the result should produce a 308 response (name-history hit)."""
        return self.redirect_to_name is not None

    @property
    def has_drift(self) -> bool:
        """True when the result carries a drift signal (200 with nameWarning)."""
        return self.entry is not None and self.served_via not in ("", "name")

    def warning_message(self) -> str:
        """Human-readable warning message for the Warning: 299 header."""
        if not self.has_drift or self.entry is None:
            return ""
        if self.served_via == "item":
            return (f"requested name {self.requested_name!r} resolved via "
                     f"item to current name {self.entry.name!r}")
        if self.served_via == "uuid":
            return (f"requested name {self.requested_name!r} resolved via "
                     f"uuid to current name {self.entry.name!r}")
        return "name drift detected"


class Registry:
    """Thread-safe identity registry persisted to YAML.

    Implements the brief v1.3 protocol:
      - Natural-first resolution: try the path name first, fall through to
        backups only if the name fails.
      - Optional name-history index: when enabled, returns 308 Permanent
        Redirect for stale names that are not paired with backup parameters.
      - Doubly optional: deployments choose whether to maintain history at
        all, and how long to retain entries.

    All mutating ops (register, rename, delete) are atomic w.r.t. the file.
    Reads do not lock — registry is loaded once into memory and kept in sync
    on writes.
    """

    def __init__(self, path: Path = REGISTRY_PATH,
                 name_history_enabled: bool = True,
                 history_retention_seconds: int | None = None):
        """Construct the registry.

        Args:
            path: YAML file backing the registry.
            name_history_enabled: if False, the resolver skips Step 1.5
                                  and stale names always 404 for non-opt-in
                                  clients (legacy behavior).
            history_retention_seconds: if None, history is retained forever.
                                       Otherwise, expires_at is set to
                                       recorded_at + this many seconds.
        """
        self.path = path
        self.name_history_enabled = name_history_enabled
        self.history_retention_seconds = history_retention_seconds
        self._lock = threading.RLock()
        # primary store: uuid -> Entry  (uuid is globally unique always)
        self._entries: dict[str, Entry] = {}
        # secondary indices, rebuilt on every change
        self._by_name: dict[str, Entry] = {}      # current canonical names only
        self._by_alias: dict[str, Entry] = {}     # alias -> entry (live only)
        self._by_item: dict[str, Entry] = {}      # server-instance uuid -> entry (live only)
        self._items_in_use: set[str] = set()      # items that have been issued
        self._tombstoned_items: set[str] = set()  # items on dead entries
        # name-history index: old_name -> NameHistoryEntry
        self._name_history: dict[str, NameHistoryEntry] = {}
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        with self._lock:
            self._entries.clear()
            self._name_history.clear()
            if not self.path.exists() or not _HAS_YAML:
                self._reindex()
                return
            data = yaml.safe_load(self.path.read_text()) or {}
            for raw in data.get("entries", []):
                e = Entry(
                    item=str(raw["item"]),
                    uuid=str(raw["uuid"]),
                    name=str(raw["name"]),
                    kind=str(raw.get("kind", "view")),
                    aliases=list(raw.get("aliases", [])),
                    deleted=bool(raw.get("deleted", False)),
                    deleted_at=str(raw.get("deleted_at", "")),
                )
                self._entries[e.uuid] = e
            for raw in data.get("name_history", []):
                h = NameHistoryEntry(
                    old_name=str(raw["old_name"]),
                    canonical_item=str(raw["canonical_item"]),
                    recorded_at=str(raw["recorded_at"]),
                    expires_at=str(raw.get("expires_at", "")),
                )
                self._name_history[h.old_name] = h
            self._reindex()

    def _save(self) -> None:
        if not _HAS_YAML:
            return
        self.path.parent.mkdir(exist_ok=True)
        ordered = sorted(self._entries.values(), key=lambda e: (e.item, e.uuid))
        history_ordered = sorted(self._name_history.values(),
                                  key=lambda h: h.recorded_at)
        data = {
            "entries": [e.to_dict() for e in ordered],
            "name_history": [h.to_dict() for h in history_ordered],
        }
        self.path.write_text(yaml.safe_dump(data, sort_keys=False))

    def _reindex(self) -> None:
        self._by_name = {}
        self._by_alias = {}
        self._by_item = {}
        self._items_in_use = set()
        self._tombstoned_items = set()
        for e in self._entries.values():
            self._items_in_use.add(e.item)
            if e.deleted:
                self._tombstoned_items.add(e.item)
                continue
            self._by_name[e.name] = e
            self._by_item[e.item] = e
            for a in e.aliases:
                self._by_alias[a] = e

    def _record_history(self, old_name: str, canonical_item: str,
                          when: str | None = None) -> None:
        """Add a name-history entry. Called on rename and delete.

        Respects history_retention_seconds for expires_at calculation.
        """
        recorded_at = when or _now_iso()
        expires_at = ""
        if self.history_retention_seconds is not None:
            from datetime import timedelta
            recorded_dt = datetime.fromisoformat(recorded_at)
            expires_dt = recorded_dt + timedelta(
                seconds=self.history_retention_seconds)
            expires_at = expires_dt.isoformat(timespec="seconds")
        self._name_history[old_name] = NameHistoryEntry(
            old_name=old_name,
            canonical_item=canonical_item,
            recorded_at=recorded_at,
            expires_at=expires_at,
        )

    def _purge_expired_history(self) -> None:
        """Remove expired name-history entries. Called opportunistically."""
        now = _now_iso()
        expired = [k for k, h in self._name_history.items()
                    if h.is_expired(now)]
        for k in expired:
            del self._name_history[k]

    # ---------- registration ----------
    def register(self, name: str, kind: str = "view",
                 aliases: list[str] | None = None,
                 share_item_with: str | None = None) -> Entry:
        """Issue a fresh `(item, uuid)` triplet for `name`.

        Both item and uuid are UUIDs (RFC 4122). Item is the server-instance
        identifier; uuid is the global identifier. They are issued
        independently except when `share_item_with` is provided.
        """
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
                if share_item_with not in self._items_in_use:
                    raise ValueError(
                        f"cannot share item {share_item_with}: not registered"
                    )
                if share_item_with in self._tombstoned_items:
                    if not any(not e.deleted for e in self._entries.values()
                                if e.item == share_item_with):
                        raise ValueError(
                            f"cannot share item {share_item_with}: all tombstoned"
                        )
                item = share_item_with
            else:
                item = str(uuid_lib.uuid4())

            new_uuid = str(uuid_lib.uuid4())
            e = Entry(item=item, uuid=new_uuid, name=name, kind=kind,
                       aliases=list(aliases or []))
            self._entries[new_uuid] = e
            self._reindex()
            self._save()
            return e

    def rename(self, item: str, new_name: str) -> Entry:
        """Change the canonical name of an entry. Records a name-history
        entry mapping the old name to the canonical item, so non-opt-in
        clients can self-heal via 308 redirect (see §2.4).
        """
        with self._lock:
            e = self._lookup_item_live(item)
            if e is None:
                raise KeyError(f"no live entry with item={item}")
            if new_name in self._by_name and self._by_name[new_name] is not e:
                raise ValueError(f"name {new_name!r} already in use")
            old_name = e.name
            e.name = new_name
            # Record name history for the old name (and any aliases that
            # were on the entry)
            if old_name != new_name:
                self._record_history(old_name, e.item)
            self._reindex()
            self._save()
            return e

    def add_alias(self, item: str, alias: str) -> Entry:
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

    def delete(self, item: str, when: str) -> Entry:
        """Tombstone an entry. The item and uuid are retained forever; the
        name and aliases are released for reuse by other resources.
        If multiple entries share an item (source + core view), tombstone all.

        Note: name-history is NOT recorded for deletion. The tombstone is
        sufficient — opt-in clients hitting the item or uuid receive 410 Gone.
        Non-opt-in clients hitting the deleted name receive 404 (since the
        name is now reusable). Deployments wanting deletion-aware redirects
        can layer their own logic atop this primitive.
        """
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
                target = siblings[-1]
            self._reindex()
            self._save()
            return target

    # ---------- lookup ----------
    def _lookup_item_live(self, item: str) -> Entry | None:
        """First live entry with this item, or None."""
        return self._by_item.get(item)

    def lookup_by_item(self, item: str) -> Entry | None:
        return self._lookup_item_live(item)

    def lookup_by_uuid(self, uid: str) -> Entry | None:
        return self._entries.get(uid)

    def lookup_by_name(self, name: str) -> Entry | None:
        e = self._by_name.get(name)
        if e is not None:
            return e
        return self._by_alias.get(name)

    def lookup_name_history(self, name: str) -> NameHistoryEntry | None:
        """Return the name-history entry for `name`, or None if absent or
        expired. Callers should check `name_history_enabled` first.
        """
        h = self._name_history.get(name)
        if h is None:
            return None
        if h.is_expired():
            # Lazy expiration: drop it and persist next save.
            return None
        return h

    def all_entries(self, include_deleted: bool = False) -> list[Entry]:
        out = [e for e in self._entries.values()
                if include_deleted or not e.deleted]
        return sorted(out, key=lambda e: (e.item, e.uuid))

    def all_history(self) -> list[NameHistoryEntry]:
        """Live (non-expired) history entries, sorted by recorded_at desc."""
        live = [h for h in self._name_history.values() if not h.is_expired()]
        return sorted(live, key=lambda h: h.recorded_at, reverse=True)

    # ---------- the natural-first resolver (the heart of the proposal) ----------
    def resolve(self, name: str | None = None,
                item: str | None = None,
                uid: str | None = None) -> ResolveResult:
        """Resolve in natural order per brief v1.3:

            (1) try the path name
            (1.5) optional: consult name-history → 308 redirect
            (2) on path failure, try item (server-instance UUID)
            (3) on item failure, try uuid (global UUID)
            (4) on all failures, return 404 (or 410 for tombstoned)

        The break in the path lookup is the trigger for drift signaling.
        Name-history (step 1.5) is policy-controlled by
        `self.name_history_enabled` and `self.history_retention_seconds`.
        """
        result = ResolveResult(
            requested_name=name or "",
            requested_item=item or "",
            requested_uuid=uid or "",
        )

        # Step 1: try the path name (the natural primary lookup)
        if name is not None:
            entry = self.lookup_by_name(name)
            if entry is not None and not entry.deleted:
                result.entry = entry
                result.served_via = "name"
                return result

        # Step 1.5 (optional): consult name-history if enabled.
        # Returns a 308 redirect indicator, not a resolved entry.
        # This applies even to opt-in clients (with backups) — see §2.4
        # "Interaction with opt-in clients."
        if name is not None and self.name_history_enabled:
            history = self.lookup_name_history(name)
            if history is not None:
                # Verify the canonical entry still exists; if it's been
                # subsequently deleted, fall through to backup logic.
                canonical = self._lookup_item_live(history.canonical_item)
                if canonical is not None:
                    result.entry = canonical
                    result.served_via = "name_history"
                    result.redirect_to_name = canonical.name
                    return result

        # Step 2: path failed — try item (server-instance UUID)
        if item is not None:
            entry = self.lookup_by_item(item)
            if entry is None and item in self._tombstoned_items:
                result.tombstoned = True
                result.served_via = "item"
                return result
            if entry is not None and not entry.deleted:
                result.entry = entry
                result.served_via = "item"
                if name is not None and name != entry.name:
                    result.drift_from = name
                return result

        # Step 3: item failed — try uuid (global UUID)
        if uid is not None:
            entry = self.lookup_by_uuid(uid)
            if entry is not None and entry.deleted:
                result.tombstoned = True
                result.served_via = "uuid"
                return result
            if entry is not None and not entry.deleted:
                result.entry = entry
                result.served_via = "uuid"
                if name is not None and name != entry.name:
                    result.drift_from = name
                return result

        # Step 4: nothing resolved — 404
        return result


# ---------- module-level singleton ----------
_global_registry: Registry | None = None


def get_registry() -> Registry:
    """Return the process-wide Registry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = Registry(path=REGISTRY_PATH)
    return _global_registry


def reset_registry_for_tests() -> None:
    """Test helper: drop the singleton so a fresh registry loads."""
    global _global_registry
    _global_registry = None
