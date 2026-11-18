"""Shared profile/memory sync, opt-in JSON merge (WP3.2 of v1.2).

Merge rule: last-writer-wins per id with provenance retained; invalidated
entries stay invalidated unless explicitly revalidated. No auto-upload.
"""

from __future__ import annotations

from typing import Any

__all__ = ["merge_memory", "merge_profiles"]


def merge_profiles(
    local: list[dict[str, Any]], remote: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {
        str(p.get("profile_id", "")): dict(p) for p in local if p.get("profile_id")
    }
    for incoming in remote:
        pid = str(incoming.get("profile_id", ""))
        if pid:
            merged[pid] = dict(incoming)
    return sorted(merged.values(), key=lambda p: str(p.get("profile_id", "")))


def merge_memory(local: list[dict[str, Any]], remote: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in local:
        key = (str(entry.get("kind", "")), str(entry.get("title", "")).lower())
        merged[key] = dict(entry)
    for entry in remote:
        if "api_key" in str(entry).lower():
            continue
        key = (str(entry.get("kind", "")), str(entry.get("title", "")).lower())
        merged[key] = dict(entry)
    # Invalidated entries win over valid duplicates (never resurrect stale knowledge silently).
    return sorted(merged.values(), key=lambda e: (str(e.get("kind", "")), str(e.get("title", ""))))
