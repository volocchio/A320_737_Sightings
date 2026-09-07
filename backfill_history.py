"""
backfill_history.py — Pull historical A320/737-family landings from FlightAware
working backwards in time, fill the local sightings DB.

FlightAware AeroAPI history retention varies by plan tier:
  - Basic   :  ~14 days
  - Standard:  ~90 days
  - Premium: 365+ days

We chunk by day, query /flights/search/advanced with an arrivalTime range,
insert each result via database.record_sighting() (which auto-dedupes via
the UNIQUE(source, flight_id) constraint), and stop when N consecutive
empty days are encountered (likely past the retention horizon).

Cost: one search call per day per page. ~1 call/sec is well within most
AeroAPI tier rate limits.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

import config
import database
from sources.flightaware import _is_us_dest, BASE_URL

log = logging.getLogger(__name__)

THROTTLE_SECONDS    = 1.0   # per request
EMPTY_DAY_THRESHOLD = 5     # stop after this many consecutive empty days

_SESSION = requests.Session()
_SESSION.headers.update({
    "x-apikey": config.FLIGHTAWARE_API_KEY,
    "Accept":   "application/json; charset=UTF-8",
})

# Module-level state for the async job (one at a time)
state: dict = {
    "status":         "idle",   # idle | running | done | error | stopped
    "started_at":     None,
    "finished_at":    None,
    "days_requested": 0,
    "days_processed": 0,
    "found":          0,
    "inserted":       0,
    "current_day":    None,
    "error":          None,
}
_STOP = threading.Event()


def _query_one_day(day: datetime) -> list[dict]:
    """Fetch all FlightAware A320/737-family arrivals for a single UTC day."""
    start = int(day.replace(hour=0, minute=0, second=0,
                            microsecond=0, tzinfo=timezone.utc).timestamp())
    end   = start + 86400 - 1
    type_list = " ".join(config.AIRCRAFT_TYPES)
    fxml = (
        f"{{aircraftType {{{type_list}}}}}"
        f" {{true arrived}}"
        f" {{range arrivalTime {start} {end}}}"
    )
    params = {"query": fxml, "max_pages": 5}
    out: list[dict] = []
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        try:
            resp = _SESSION.get(
                f"{BASE_URL}/flights/search/advanced",
                params=params, timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("backfill_history: FA error on %s: %s",
                        day.strftime("%Y-%m-%d"), exc)
            break
        data    = resp.json()
        flights = data.get("flights", [])
        out.extend(flights)
        cursor = (data.get("links") or {}).get("next")
        if not cursor or not flights:
            break
        time.sleep(THROTTLE_SECONDS)
    return out


def _flight_to_sighting(flight: dict) -> dict | None:
    """Convert a FlightAware /flights/search/advanced result to a sighting dict."""
    origin = flight.get("origin") or {}
    dest   = flight.get("destination") or {}
    dest_icao = dest.get("code_icao") or dest.get("code", "")
    if not _is_us_dest(dest_icao):
        return None
    flight_id = flight.get("fa_flight_id") or flight.get("ident", "")
    tracking_url = (
        f"https://flightaware.com/live/flight/id/{quote(flight_id, safe='')}"
        if flight_id
        else f"https://flightaware.com/live/flight/{quote((flight.get('ident', '') or ''), safe='')}"
    )
    return {
        "source":       "flightaware",
        "flight_id":    flight_id,
        "tail_number":  flight.get("registration") or flight.get("ident", ""),
        "ac_type":      flight.get("aircraft_type", ""),
        "origin_icao":  origin.get("code_icao") or origin.get("code", ""),
        "origin_name":  origin.get("name", ""),
        "dest_icao":    dest_icao,
        "dest_name":    dest.get("name", ""),
        "departed_utc": flight.get("actual_off") or flight.get("scheduled_off", ""),
        "arrived_utc":  flight.get("actual_on")  or flight.get("actual_off", ""),
        "operator":     flight.get("operator") or "",
        "tracking_url": tracking_url,
        "distance_nm":  None,   # backfilled later by database.backfill_distances
    }


def _run(days: int) -> None:
    state.update({
        "status":         "running",
        "started_at":     datetime.utcnow().isoformat(),
        "finished_at":    None,
        "days_requested": days,
        "days_processed": 0,
        "found":          0,
        "inserted":       0,
        "current_day":    None,
        "error":          None,
    })
    _STOP.clear()
    empty_streak = 0
    today = datetime.utcnow().date()
    try:
        for d_off in range(1, days + 1):
            if _STOP.is_set():
                state["status"] = "stopped"
                break
            day = datetime.combine(today - timedelta(days=d_off),
                                   datetime.min.time())
            state["current_day"] = day.strftime("%Y-%m-%d")

            flights  = _query_one_day(day)
            inserted = 0
            for f in flights:
                s = _flight_to_sighting(f)
                if not s or not s["flight_id"]:
                    continue
                # Snapshot row count to detect actual insert vs duplicate
                with database._connect() as conn:
                    before = conn.execute(
                        "SELECT 1 FROM sightings WHERE source=? AND flight_id=?",
                        (s["source"], s["flight_id"]),
                    ).fetchone()
                if before:
                    continue
                try:
                    database.record_sighting(s)
                    inserted += 1
                except Exception as e:   # noqa: BLE001
                    log.debug("backfill insert failed: %s", e)
            state["days_processed"] += 1
            state["found"]    += len(flights)
            state["inserted"] += inserted
            if not flights:
                empty_streak += 1
                if empty_streak >= EMPTY_DAY_THRESHOLD:
                    log.info("backfill_history: %d consecutive empty days "
                             "(likely past retention horizon), stopping",
                             empty_streak)
                    break
            else:
                empty_streak = 0
            time.sleep(THROTTLE_SECONDS)
        if state["status"] == "running":
            state["status"] = "done"
    except Exception as e:   # noqa: BLE001
        state["status"] = "error"
        state["error"]  = f"{type(e).__name__}: {e}"
        log.exception("backfill_history crashed")
    finally:
        state["finished_at"] = datetime.utcnow().isoformat()
        state["current_day"] = None


def start(days: int) -> bool:
    """Start the backfill in a background thread. Returns False if already running."""
    if state["status"] == "running":
        return False
    threading.Thread(target=_run, args=(days,), daemon=True,
                     name="history-backfill").start()
    return True


def stop() -> None:
    """Signal the running backfill to stop after the current day completes."""
    _STOP.set()
