"""
sources/flightaware.py — FlightAware AeroAPI v4

Docs: https://flightaware.com/aeroapi/portal/documentation

Strategy: poll /flights/search/advanced with FXML for all C525 types
that have arrived in the last LOOKBACK_MINUTES minutes.

FXML notes (v4):
  - endpoint: /flights/search/advanced  (NOT /flights/search)
  - aircraft type field: aircraftType   (NOT type)
  - arrival time field: arrivalTime     (NOT actualarrivaltime)
  - US destinations: filter post-fetch by ICAO prefix K/PA/PH/TJ/TI etc.

Cost: one call per poll regardless of how many type codes we watch.
"""

import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

import config
from sources import Sighting

BASE_URL = "https://aeroapi.flightaware.com/aeroapi"

# ICAO prefixes for US and US-territory airports
_US_PREFIXES = ("K", "PA", "PH", "PG", "TJ", "TI", "TK", "TN", "MD")

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "x-apikey": config.FLIGHTAWARE_API_KEY,
        "Accept": "application/json; charset=UTF-8",
    }
)


def _fxml_query(lookback_minutes: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    cutoff_epoch = int(cutoff.timestamp())
    type_list = " ".join(config.AIRCRAFT_TYPES)
    return (
        f"{{aircraftType {{{type_list}}}}}"
        f" {{true arrived}}"
        f" {{> arrivalTime {cutoff_epoch}}}"
    )


def _is_us_dest(icao: str) -> bool:
    return icao.upper().startswith(_US_PREFIXES)


def fetch_landings(lookback_minutes: int) -> list[Sighting]:
    if not config.FLIGHTAWARE_ACTIVE:
        return []

    query = _fxml_query(lookback_minutes)
    params = {"query": query, "max_pages": 5}

    sightings: list[Sighting] = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor

        try:
            resp = _SESSION.get(
                f"{BASE_URL}/flights/search/advanced", params=params, timeout=15
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("FlightAware API error: %s", exc)
            break

        data = resp.json()
        flights = data.get("flights", [])

        for flight in flights:
            origin = flight.get("origin") or {}
            dest   = flight.get("destination") or {}

            dest_icao = dest.get("code_icao") or dest.get("code", "")
            if not _is_us_dest(dest_icao):
                continue

            arrived_str  = flight.get("actual_on")  or flight.get("actual_off", "")
            departed_str = flight.get("actual_off") or flight.get("scheduled_off", "")
            tail         = flight.get("registration") or flight.get("ident", "")
            flight_id    = flight.get("fa_flight_id") or flight.get("ident", "")
            ac_type      = flight.get("aircraft_type", "")
            tracking_url = (
                f"https://flightaware.com/live/flight/id/{quote(flight_id, safe='')}"
                if flight_id
                else f"https://flightaware.com/live/flight/{quote((flight.get('ident', '') or ''), safe='')}"
            )

            sightings.append(
                Sighting(
                    source="flightaware",
                    flight_id=flight_id,
                    tail_number=tail,
                    ac_type=ac_type,
                    origin_icao=origin.get("code_icao") or origin.get("code", ""),
                    origin_name=origin.get("name", ""),
                    dest_icao=dest_icao,
                    dest_name=dest.get("name", ""),
                    departed_utc=departed_str,
                    arrived_utc=arrived_str,
                    operator=flight.get("operator") or "",
                    tracking_url=tracking_url,
                )
            )

        links = data.get("links") or {}
        cursor = links.get("next")
        if not cursor or not flights:
            break

    log.info("FlightAware: found %d US landings", len(sightings))
    return sightings


def enrich_sighting(tail_number: str, arrived_utc: str) -> dict:
    """
    Given a tail number and approximate arrival time (ISO-8601 UTC), call
    FlightAware /flights/{tail} and return the best-matching flight's
    operator, origin_icao, origin_name, and departed_utc.

    Returns an empty dict if FlightAware is not active or no match found.
    Cost: 1 API call per new sighting from OpenSky / ADS-B Exchange.
    """
    if not config.FLIGHTAWARE_ACTIVE:
        return {}
    if not tail_number or not arrived_utc:
        return {}

    try:
        arrived_dt = datetime.fromisoformat(arrived_utc.replace("Z", "+00:00"))
    except ValueError:
        return {}

    # Look for flights in a ±2h window around the landing time
    start = (arrived_dt - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = (arrived_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = _SESSION.get(
            f"{BASE_URL}/flights/{tail_number}",
            params={"ident_type": "registration", "start": start, "end": end, "max_pages": 1},
            timeout=10,
        )
        if not resp.ok:
            return {}
        flights = resp.json().get("flights") or []
    except Exception:  # noqa: BLE001
        return {}

    if not flights:
        return {}

    # Pick the flight whose actual_on is closest to arrived_utc
    def _closeness(f):
        on = f.get("actual_on") or f.get("scheduled_on") or ""
        if not on:
            return 9999999
        try:
            dt = datetime.fromisoformat(on.replace("Z", "+00:00"))
            return abs((dt - arrived_dt).total_seconds())
        except ValueError:
            return 9999999

    best = min(flights, key=_closeness)
    origin = best.get("origin") or {}
    result = {}
    if best.get("fa_flight_id") or best.get("ident"):
        result["fa_flight_id"] = best.get("fa_flight_id") or best.get("ident", "")
    if best.get("operator"):
        result["operator"] = best["operator"]
    if origin.get("code_icao") or origin.get("code"):
        result["origin_icao"] = origin.get("code_icao") or origin.get("code", "")
        result["origin_name"] = origin.get("name", "")
    if best.get("actual_off") or best.get("scheduled_off"):
        result["departed_utc"] = best.get("actual_off") or best.get("scheduled_off", "")
    return result

