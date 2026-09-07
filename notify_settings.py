"""
notify_settings.py — user-configurable cadence for Teams sighting cards.

The per-sighting Adaptive Cards (teams_notifier.notify_sighting) can fire very
frequently when a busy tail or airport is on the watch list. This module holds
a single knob — the minimum minutes that must elapse before another card is
sent for the SAME tail — so the user can throttle the A320/737 Sightings Thread from
the /watch page without a redeploy.

0 = send a card for every matching sighting (original behavior).

The value lives in ./notify_settings.json on the volume-mounted /app directory
so it survives container rebuilds (same pattern as watch_airports/watch_tails).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_PATH = Path(__file__).parent / "notify_settings.json"
_LOCK = threading.Lock()

_DEFAULT_MIN_INTERVAL = 0  # minutes; 0 = notify on every matching sighting

# Choices surfaced in the /watch dropdown: (minutes, human label).
INTERVAL_CHOICES: list[tuple[int, str]] = [
    (0,    "Every sighting (no limit)"),
    (60,   "At most once per hour"),
    (180,  "At most once every 3 hours"),
    (360,  "At most once every 6 hours"),
    (720,  "At most once every 12 hours"),
    (1440, "At most once per day"),
]


def get_min_interval_minutes() -> int:
    """Return the configured per-tail cooldown in minutes (0 = no limit)."""
    if not _PATH.exists():
        return _DEFAULT_MIN_INTERVAL
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = int(data.get("min_interval_minutes", _DEFAULT_MIN_INTERVAL))
            return val if val >= 0 else _DEFAULT_MIN_INTERVAL
    except Exception as e:   # noqa: BLE001
        log.warning("notify_settings read failed: %s", e)
    return _DEFAULT_MIN_INTERVAL


def set_min_interval_minutes(minutes: int) -> int:
    """Persist the per-tail cooldown (clamped to >= 0). Returns saved value."""
    try:
        m = max(0, int(minutes))
    except (TypeError, ValueError):
        m = _DEFAULT_MIN_INTERVAL
    with _LOCK:
        _PATH.write_text(
            json.dumps({"min_interval_minutes": m}, indent=2),
            encoding="utf-8",
        )
    log.info("notify_settings updated: min_interval_minutes=%s", m)
    return m


def interval_label(minutes: int) -> str:
    """Human label for a stored interval; falls back to a generic phrasing."""
    for m, label in INTERVAL_CHOICES:
        if m == minutes:
            return label
    if minutes <= 0:
        return "Every sighting (no limit)"
    return f"At most once every {minutes} minutes"
