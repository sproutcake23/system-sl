"""Configurable news source registry for Daily Intel.

Built-in sources live in :mod:`system_sl.intel.sources`; this module merges them
with the player's persisted overrides and custom sources. Enable/disable stays
in ``intel_config.json`` (``disabled_sources``) so existing data keeps working,
while definitions live in ``intel_sources.json``.

A persisted source list stores only overrides and custom entries plus the ids
of removed built-ins. New built-in sources added by a future release therefore
appear automatically, without resurrecting entries the player deleted.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from system_sl.intel.sources import DEFAULT_SOURCES, SOURCE_TYPES, NewsSource
from system_sl.intel.topics import GENERAL_TOPIC

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 7 * 24 * 60
DEFAULT_INTERVAL_MINUTES = 6 * 60
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

BUILTIN_IDS = frozenset(source.id for source in DEFAULT_SOURCES)


class SourceValidationError(ValueError):
    """Raised when a source definition cannot be used safely."""


def slugify(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return text.strip("-")[:64]


def _clean_text(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _validate_url(value: object, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise SourceValidationError(f"{field} is required")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SourceValidationError(f"{field} is not a valid URL") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise SourceValidationError(f"{field} must be an http(s) URL")
    return raw


def normalize_source_type(value: object) -> str:
    raw = str(value or "rss").strip().lower()
    if raw == "feed":
        raw = "rss"
    if raw not in SOURCE_TYPES:
        raise SourceValidationError(f"type must be one of: {', '.join(SOURCE_TYPES)}")
    return raw


def validate_source_payload(
    payload: dict,
    *,
    source_id: str | None = None,
    existing_ids: Iterable[str] = (),
) -> dict:
    """Validate and normalize one source definition. Raises on bad input."""
    if not isinstance(payload, dict):
        raise SourceValidationError("source must be an object")

    name = _clean_text(payload.get("name"), limit=80)
    if not name:
        raise SourceValidationError("name is required")

    identifier = source_id or payload.get("id") or slugify(name)
    identifier = str(identifier).strip().lower()
    if not SOURCE_ID_RE.match(identifier):
        raise SourceValidationError(
            "id must be 2-64 lowercase letters, digits or dashes"
        )

    known = {str(item) for item in existing_ids}
    if identifier in known:
        raise SourceValidationError(f"a source with id '{identifier}' already exists")

    url = _validate_url(payload.get("url"), "url")
    homepage = payload.get("homepage")
    if homepage:
        homepage = _validate_url(homepage, "homepage")
    else:
        parts = urlsplit(url)
        homepage = f"{parts.scheme}://{parts.netloc}"

    category = _clean_text(payload.get("category") or GENERAL_TOPIC, limit=80) or GENERAL_TOPIC

    try:
        reliability = int(payload.get("reliability", 70))
    except (TypeError, ValueError) as exc:
        raise SourceValidationError("reliability must be a number from 0 to 100") from exc
    reliability = max(0, min(100, reliability))

    interval = payload.get("interval_minutes")
    if interval in (None, "", 0, "0"):
        interval = None
    else:
        try:
            interval = int(interval)
        except (TypeError, ValueError) as exc:
            raise SourceValidationError("interval_minutes must be an integer") from exc
        interval = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval))

    return {
        "id": identifier,
        "name": name,
        "url": url,
        "homepage": homepage,
        "category": category,
        "reliability": reliability,
        "enabled": bool(payload.get("enabled", True)),
        "official": bool(payload.get("official", False)),
        "type": normalize_source_type(payload.get("type") or payload.get("source_type")),
        "interval_minutes": interval,
        "extract": bool(payload.get("extract", True)),
    }


def source_from_dict(payload: dict) -> NewsSource:
    """Build a :class:`NewsSource` from a normalized definition."""
    return NewsSource(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        url=str(payload.get("url") or ""),
        homepage=str(payload.get("homepage") or ""),
        category=str(payload.get("category") or GENERAL_TOPIC),
        reliability=int(payload.get("reliability") or 0),
        enabled=bool(payload.get("enabled", True)),
        official=bool(payload.get("official", False)),
        source_type=str(payload.get("type") or payload.get("source_type") or "rss"),
        interval_minutes=payload.get("interval_minutes"),
        extract=bool(payload.get("extract", True)),
    )


class SourceRegistry:
    """Loads, validates and persists the source catalog."""

    def __init__(self, store, *, defaults: Iterable[NewsSource] = DEFAULT_SOURCES) -> None:
        self.store = store
        self.defaults = tuple(defaults)
        self._defaults_by_id = {source.id: source for source in self.defaults}

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def sources(self) -> list[NewsSource]:
        persisted = self.store.load_source_definitions()
        removed = {
            str(item)
            for item in persisted.get("removed_defaults", [])
            if str(item) in self._defaults_by_id
        }
        overrides: dict[str, dict] = {}
        custom: list[dict] = []
        for item in persisted.get("sources", []):
            try:
                normalized = validate_source_payload(item, source_id=str(item.get("id") or ""))
            except SourceValidationError:
                # A hand-edited, invalid entry is ignored instead of breaking
                # the whole catalog.
                continue
            if normalized["id"] in self._defaults_by_id:
                overrides[normalized["id"]] = normalized
            elif normalized["id"] not in removed:
                custom.append(normalized)

        catalog: list[NewsSource] = []
        for default in self.defaults:
            if default.id in removed:
                continue
            override = overrides.get(default.id)
            catalog.append(source_from_dict(override) if override else default)
        catalog.extend(source_from_dict(item) for item in custom)
        return catalog

    def get(self, source_id: str) -> Optional[NewsSource]:
        for source in self.sources():
            if source.id == source_id:
                return source
        return None

    def source_ids(self) -> set[str]:
        return {source.id for source in self.sources()}

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def add(self, payload: dict) -> NewsSource:
        current = self.sources()
        requested = str((payload or {}).get("id") or "").strip().lower()
        removed = self._removed_defaults()
        if requested and requested in self._defaults_by_id and requested in removed:
            # Re-adding a deleted built-in is an update, not a duplicate.
            definition = validate_source_payload(
                payload,
                source_id=requested,
                existing_ids={source.id for source in current if source.id != requested},
            )
            self._ensure_unique_url(definition, current)
            removed.discard(requested)
            self._write(current + [source_from_dict(definition)], removed)
            return source_from_dict(definition)

        source_id = requested or slugify(payload.get("name"))
        definition = validate_source_payload(
            payload,
            source_id=source_id,
            existing_ids={source.id for source in current},
        )
        self._ensure_unique_url(definition, current)
        updated = current + [source_from_dict(definition)]
        self._write(updated, removed)
        return source_from_dict(definition)

    def update(self, source_id: str, payload: dict) -> NewsSource:
        current = self.sources()
        target = next((source for source in current if source.id == source_id), None)
        if target is None:
            raise SourceValidationError(f"unknown source '{source_id}'")
        merged = {**target.as_dict(), **(payload or {}), "id": source_id}
        definition = validate_source_payload(
            merged,
            source_id=source_id,
            existing_ids={source.id for source in current if source.id != source_id},
        )
        self._ensure_unique_url(definition, current, ignore_id=source_id)
        updated = self._replace(current, definition)
        self._write(updated, self._removed_defaults())
        return source_from_dict(definition)

    def remove(self, source_id: str) -> bool:
        current = self.sources()
        if not any(source.id == source_id for source in current):
            return False
        removed = self._removed_defaults()
        if source_id in self._defaults_by_id:
            removed.add(source_id)
        remaining = [source for source in current if source.id != source_id]
        self._write(remaining, removed)
        return True

    def reset(self) -> list[NewsSource]:
        """Drop every override and custom source, restoring the built-ins."""
        self.store.save_source_definitions(
            {"version": 2, "removed_defaults": [], "sources": []}
        )
        return self.sources()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _removed_defaults(self) -> set[str]:
        return {
            str(item)
            for item in self.store.load_source_definitions().get("removed_defaults", [])
        }

    def _replace(self, sources: list[NewsSource], definition: dict) -> list[NewsSource]:
        replacement = source_from_dict(definition)
        return [
            replacement if source.id == definition["id"] else source
            for source in sources
        ]

    def _ensure_unique_url(
        self,
        definition: dict,
        current: Iterable[NewsSource],
        *,
        ignore_id: str | None = None,
    ) -> None:
        target = definition["url"].rstrip("/").lower()
        for source in current:
            if source.id == ignore_id or source.id == definition["id"]:
                continue
            if source.url.rstrip("/").lower() == target:
                raise SourceValidationError(
                    f"that feed is already registered as '{source.name}'"
                )

    def _write(self, sources: Iterable[NewsSource], removed_defaults: Iterable[str]) -> None:
        stored: list[dict] = []
        for source in sources:
            base = self._defaults_by_id.get(source.id)
            if base is None or source.as_dict() != base.as_dict():
                stored.append(source.as_dict())
        self.store.save_source_definitions(
            {
                "version": 2,
                "removed_defaults": sorted({str(item) for item in removed_defaults}),
                "sources": stored,
            }
        )
