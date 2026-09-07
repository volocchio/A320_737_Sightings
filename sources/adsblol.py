"""
sources/adsblol.py — adsb.lol community ADS-B network

Does NOT participate in FAA LADD / BARR blocking — catches aircraft that
FlightAware and FlightRadar24 hide at operator request (dbFlags & 8 = LADD).

API: https://api.adsb.lol/v2/type/<ICAO>  — free, no key required
Response format: {"ac": [...], "now": ..., "total": ...}  (readsb / ADSBX-v2 schema)

One call per aircraft type per poll cycle. Landing transitions are inferred from
on-ground state changes. dest_icao is resolved from the landing lat/lon against
the airports table; origin/dep-time are enriched via FlightAware in main.py.
"""

import logging
import time
from datetime import datetime, timezone

import requests

import airports as _ap
import config
from sources import Sighting

BASE_URL = "https://api.adsb.lol/v2/type"

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "A320737Sightings/1.0 (voloaltro.tech)",
    "Accept": "application/json",
})

# hex → bool: was the aircraft on-ground in the previous poll?
# Seeded True so we don't fire a phantom landing on first observation.
_was_on_ground: dict[str, bool] = {}

# hex → (lat, lon): last known position while airborne, used to estimate
# destination if alt_baro transitions to "ground" without a position update.
_last_airborne_pos: dict[str, tuple[float, float]] = {}


def _is_on_ground(ac: dict) -> bool:
    alt = ac.get("alt_baro")
    if alt == "ground":
        return True
    if isinstance(alt, (int, float)):
        return alt <= 100
    return False


def fetch_landings(lookback_minutes: int) -> list[Sighting]:  # noqa: ARG001
    if not config.ADSBLOL_ACTIVE:
        return []

    sightings: list[Sighting] = []
    now_ts = int(time.time())
    arrived_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    for ac_type in config.AIRCRAFT_TYPES:
        try:
            resp = _SESSION.get(f"{BASE_URL}/{ac_type}", timeout=15)
            if resp.status_code == 429:
                log.warning("adsb.lol: rate-limited on type %s", ac_type)
                break
            if not resp.ok:
                log.debug("adsb.lol: HTTP %s for type %s", resp.status_code, ac_type)
                continue
        except requests.RequestException as exc:
            log.debug("adsb.lol: request error for type %s: %s", ac_type, exc)
            continue

        for ac in resp.json().get("ac") or []:
            hex_code = (ac.get("hex") or "").lower().strip()
            if not hex_code:
                continue

            on_ground = _is_on_ground(ac)
            prev = _was_on_ground.get(hex_code, True)
            _was_on_ground[hex_code] = on_ground

            lat = ac.get("lat")
            lon = ac.get("lon")
            if not on_ground and lat is not None and lon is not None:
                _last_airborne_pos[hex_code] = (lat, lon)

            # Only fire on fresh landing (airborne last poll, on-ground now)
            if not on_ground or prev:
                continue

            registration = (ac.get("r") or "").strip().upper()
            if not registration:
                continue

            # Resolve destination airport from landing position
            dest_icao = ""
            dest_name = ""
            land_lat = lat or (_last_airborne_pos.get(hex_code) or (None, None))[0]
            land_lon = lon or (_last_airborne_pos.get(hex_code) or (None, None))[1]
            if land_lat is not None and land_lon is not None:
                apt = _ap.nearest_airport(land_lat, land_lon, max_km=8.0)
                if apt:
                    dest_icao = apt.get("icao", "")
                    dest_name = apt.get("name", "")

            sightings.append(Sighting(
                source="adsblol",
                flight_id=f"adsblol_{hex_code}_{now_ts}",
                tail_number=registration,
                ac_type=ac.get("t") or ac_type,
                origin_icao="",
                origin_name="",
                dest_icao=dest_icao,
                dest_name=dest_name,
                departed_utc="",
                arrived_utc=arrived_utc,
                operator="",
                tracking_url=f"https://globe.adsbexchange.com/?icao={hex_code}",
            ))

    if sightings:
        log.info("adsb.lol: detected %d landing transition(s)", len(sightings))
    return sightings
