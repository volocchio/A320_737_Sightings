"""
watch_airports.py — Persisted user-managed list of airports of interest.

When a new sighting's origin OR destination ICAO matches the watch list,
teams_notifier.notify_sighting() posts an Adaptive Card to the configured
Teams webhook channel.

The list lives in ./watch_airports.json on the volume-mounted /app directory,
so it survives container rebuilds.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_PATH = Path(__file__).parent / "watch_airports.json"
_LOCK = threading.Lock()


def get_watch_list() -> list[str]:
    """Return the current watch list (uppercase ICAO codes)."""
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(c).strip().upper() for c in data if str(c).strip()]
        if isinstance(data, dict) and isinstance(data.get("airports"), list):
            return [str(c).strip().upper() for c in data["airports"] if str(c).strip()]
    except Exception as e:   # noqa: BLE001
        log.warning("watch_airports read failed: %s", e)
    return []


def set_watch_list(codes: list[str]) -> list[str]:
    """Overwrite the watch list. Returns the saved list."""
    clean = []
    for c in codes:
        s = str(c).strip().upper()
        if s and s not in clean:
            clean.append(s)
    with _LOCK:
        _PATH.write_text(
            json.dumps({"airports": clean}, indent=2),
            encoding="utf-8",
        )
    log.info("watch_airports updated: %s", clean)
    return clean


def matches(*icaos: str | None) -> list[str]:
    """Return subset of supplied ICAO codes that are on the watch list."""
    watch = set(get_watch_list())
    if not watch:
        return []
    hits = []
    for code in icaos:
        if code and code.upper() in watch and code.upper() not in hits:
            hits.append(code.upper())
    return hits
