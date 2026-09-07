"""
sources/adsbexchange.py — ADS-B Exchange via RapidAPI

NOTE: The RapidAPI tier only supports per-aircraft lookups (by hex/reg/callsign).
The bulk type-query endpoint (/v2/type/{type}/) is only available on the direct
adsbexchange.com enterprise API.

Current strategy: use the FAA hex cache (shared with OpenSky) and query a
rotating batch of 50 aircraft per poll via the "last position" endpoint. This
stays well within the RapidAPI quota (~1,500 calls/day at 5-min polls × 50/poll)
and gives us an independent ADS-B source for landing detection.

Each aircraft in the cache is checked roughly once every (1246/50 × 5) = 2 hours,
which is acceptable for market intelligence — landings stay on the ground long
enough to be caught.
"""

import logging
import time
from datetime import datetime, timezone

import requests

import config
from sources import Sighting

BASE_URL = "https://adsbexchange-com1.p.rapidapi.com/v2"

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "x-rapidapi-key": config.ADSBEXCHANGE_API_KEY,
    "x-rapidapi-host": "adsbexchange-com1.p.rapidapi.com",
})

# Rotating index into the hex cache list
_batch_index = 0
BATCH_SIZE = 50

# Track previous on-ground state per hex to detect landing transitions
_was_on_ground: dict[str, bool] = {}


def fetch_landings(lookback_minutes: int) -> list[Sighting]:  # noqa: ARG001
    if not config.ADSBEXCHANGE_ACTIVE:
        return []

    # Import hex cache from opensky (shared FAA data)
    try:
        from sources.opensky import _hex_cache, _hex_cache_loaded, _load_faa_hex_cache
        if not _hex_cache_loaded:
            _load_faa_hex_cache()
        hex_list = list(_hex_cache.keys())
    except Exception as exc:  # noqa: BLE001
        log.warning("ADS-B Exchange: could not access hex cache: %s", exc)
        return []

    if not hex_list:
        return []

    global _batch_index
    batch = hex_list[_batch_index: _batch_index + BATCH_SIZE]
    _batch_index = (_batch_index + BATCH_SIZE) % len(hex_list)

    sightings: list[Sighting] = []
    now_ts = int(time.time())

    for hex_code in batch:
        try:
            resp = _SESSION.get(
                f"{BASE_URL}/icao/{hex_code}/",
                timeout=10,
            )
            if resp.status_code == 401:
                log.error("ADS-B Exchange: invalid API key")
                return []
            if resp.status_code == 429:
                log.warning("ADS-B Exchange: rate-limited, skipping remainder of batch")
                break
            if not resp.ok:
                continue
        except requests.RequestException as exc:
            log.debug("ADS-B Exchange query error (%s): %s", hex_code, exc)
            continue

        data = resp.json()
        aircraft_list = data.get("ac") or []

        for ac in aircraft_list:
            on_ground = bool(ac.get("on_grnd") == "1" or ac.get("gnd"))
            prev = _was_on_ground.get(hex_code, True)
            _was_on_ground[hex_code] = on_ground

            if not on_ground or prev:
                continue

            info = _hex_cache.get(hex_code, {})
            registration = ac.get("r") or info.get("registration", hex_code)
            arrived_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

            sightings.append(
                Sighting(
                    source="adsbexchange",
                    flight_id=f"adsbx_{hex_code}_{now_ts}",
                    tail_number=registration,
                    ac_type=ac.get("t") or info.get("typecode", "C525"),
                    origin_icao="",
                    origin_name="",
                    dest_icao="",
                    dest_name="",
                    departed_utc="",
                    arrived_utc=arrived_utc,
                    operator="",
                    tracking_url=f"https://globe.adsbexchange.com/?icao={hex_code}",
                )
            )

    if sightings:
        log.info("ADS-B Exchange: detected %d landing transition(s)", len(sightings))
    return sightings
