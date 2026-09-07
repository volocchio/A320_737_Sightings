"""
watch_tails.py — Persisted user-managed list of N-numbers (tails) of interest.

Parallel to watch_airports.py. When a new sighting's tail_number matches the
watch list, teams_notifier.notify_sighting() posts an Adaptive Card.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_PATH = Path(__file__).parent / "watch_tails.json"
_LOCK = threading.Lock()


def get_watch_list() -> list[str]:
    """Return the current watch list (uppercase tail numbers)."""
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(t).strip().upper() for t in data if str(t).strip()]
        if isinstance(data, dict) and isinstance(data.get("tails"), list):
            return [str(t).strip().upper() for t in data["tails"] if str(t).strip()]
    except Exception as e:   # noqa: BLE001
        log.warning("watch_tails read failed: %s", e)
    return []


def set_watch_list(tails: list[str]) -> list[str]:
    """Overwrite the watch list. Returns the saved list."""
    clean = []
    for t in tails:
        s = str(t).strip().upper()
        if s and s not in clean:
            clean.append(s)
    with _LOCK:
        _PATH.write_text(
            json.dumps({"tails": clean}, indent=2),
            encoding="utf-8",
        )
    log.info("watch_tails updated: %s", clean)
    return clean


def add_tails(new: list[str]) -> list[str]:
    """Union the supplied tails into the current list. Returns the merged list."""
    current = get_watch_list()
    for t in new:
        s = str(t).strip().upper()
        if s and s not in current:
            current.append(s)
    return set_watch_list(current)


def matches(tail: str | None) -> bool:
    if not tail:
        return False
    return tail.upper() in set(get_watch_list())
