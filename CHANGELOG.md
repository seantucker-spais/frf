# Changelog

## v1.3.0 — May 2026

This release aligns the reference implementation with v1.3 of the
practitioner technical brief: https://doi.org/10.5281/zenodo.19980026

### Changed
- `Entry.item` is now a server-instance UUID (string) rather than an integer
- Resolver follows natural-first cascade: name → item → uuid (was uuid > item > name)
- HTTP layer returns 308 Permanent Redirect when name-history hits
- `_resolve_collection` now returns a 3-tuple `(resolved_name, drift_info, redirect_url)`

### Added
- `NameHistoryEntry` dataclass for tracking renamed-from → renamed-to mappings
- `Registry.lookup_name_history()` for name-history queries
- `Registry.name_history_enabled` flag (deployment policy)
- `Registry.history_retention_seconds` field (deployment policy)
- `_build_redirect()` helper for 308 responses with Location and Link headers
- §2.2 backward-compatibility coverage in tests (8 scenarios)
- §2.3 edge-case coverage in tests (silent reassignment, retention expiry)
- §2.4 name-history fallback tests (enabled, disabled, retention window)
- `frf_registry.py.v1_0.bak`, `test_registry.py.v1_0.bak`,
  `test_integration.py.v1_0.bak` preserved for diff reference

### Removed
- Integer-based item identifiers (replaced by UUIDs)
- `uuid > item > name` precedence (replaced by name-first cascade)
- `_next_item` counter (no longer needed for UUID items)
- `_tombstoned_items: set[int]` (replaced by `set[str]`)

### Test status
- 18/18 registry tests passing
- 7/7 HTTP integration tests passing
- Total: 25/25

## v1.0.0 — earlier

Initial reference implementation accompanying brief v1.0.
