"""
sources/opensky.py — OpenSky Network REST API

Docs: https://openskynetwork.github.io/opensky-api/rest.html

Strategy:
  Build a hex-code cache from the FAA ReleasableAircraft registry (authoritative,
  no auth needed), then poll OpenSky /states/all for those hex codes and detect
  airborne→on_ground transitions as landing events.

  FAA registry: https://registry.faa.gov/database/ReleasableAircraft.zip
    ACFTREF.txt — maps MFR MDL CODE → manufacturer + model name
    MASTER.txt  — maps N-number → MFR MDL CODE + MODE S CODE HEX (ICAO hex)

  Note: OpenSky free tier — 400 req/day anonymous, 4000/day with account.
  At 5-minute polling that's 288 req/day — within anonymous limits.
"""

import csv
import io
import logging
import time
import zipfile
from datetime import datetime, timezone

import requests

import config
from sources import Sighting

BASE_URL = "https://opensky-network.org/api"
FAA_ZIP_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"

# Continental USA bounding box (lat/lon)
USA_BBOX = {
    "lamin": 24.396308,
    "lomin": -124.848974,
    "lamax": 49.384358,
    "lomax": -66.885444,
}

log = logging.getLogger(__name__)

# icao24_hex → {registration, typecode}
_hex_cache: dict[str, dict] = {}
_hex_cache_loaded = False

# Airbus A320-family and Boeing 737-family: FAA manufacturer/model patterns → ICAO type code.
_MODEL_RULES: list[tuple[tuple[str, ...], tuple[str, ...], str]] = [
    (("AIRBUS",), ("A318",), "A318"),
    (("AIRBUS",), ("A319NEO", "A-319NEO", "A319 NEO"), "A19N"),
    (("AIRBUS",), ("A319", "A-319"), "A319"),
    (("AIRBUS",), ("A320NEO", "A-320NEO", "A320 NEO"), "A20N"),
    (("AIRBUS",), ("A320", "A-320"), "A320"),
    (("AIRBUS",), ("A321NEO", "A-321NEO", "A321 NEO"), "A21N"),
    (("AIRBUS",), ("A321", "A-321"), "A321"),
    (("BOEING",), ("737-600", "737 600"), "B736"),
    (("BOEING",), ("737-700", "737 700"), "B737"),
    (("BOEING",), ("737-800", "737 800"), "B738"),
    (("BOEING",), ("737-900", "737 900"), "B739"),
    (("BOEING",), ("737 MAX 7", "737-7", "737 7"), "B37M"),
    (("BOEING",), ("737 MAX 8", "737-8", "737 8"), "B38M"),
    (("BOEING",), ("737 MAX 9", "737-9", "737 9"), "B39M"),
    (("BOEING",), ("737 MAX 10", "737-10", "737 10"), "B3XM"),
]

def _model_to_icao(mfr: str, model: str) -> str | None:
    hay_mfr = mfr.upper()
    hay_model = model.upper().replace("/", " ").replace("_", " ")
    compact = hay_model.replace("-", "").replace(" ", "")
    for mfr_needles, model_needles, icao in _MODEL_RULES:
        if not any(n in hay_mfr for n in mfr_needles):
            continue
        for needle in model_needles:
            n_compact = needle.upper().replace("-", "").replace(" ", "")
            if needle.upper() in hay_model or n_compact in compact:
                return icao
    return None


def _load_faa_hex_cache() -> None:
    """
    Download FAA ReleasableAircraft.zip and build icao24_hex → aircraft info
    for all configured A320/737-family aircraft on the US registry.

    ACFTREF.txt columns (comma-sep):
      CODE, MFG, MODEL, TYPE-ACFT, TYPE-ENG, AC-CAT, ...
    MASTER.txt columns (comma-sep):
      N-NUMBER, SERIAL NUMBER, MFR MDL CODE, ...(many)..., MODE S CODE HEX, (trailing comma)
      MODE S CODE HEX is the last non-empty column (index 33).
    """
    global _hex_cache, _hex_cache_loaded
    if _hex_cache_loaded:
        return

    log.info("Downloading FAA ReleasableAircraft registry…")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(FAA_ZIP_URL, timeout=120, headers=headers)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not download FAA registry: %s", exc)
        _hex_cache_loaded = True
        return

    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile as exc:
        log.warning("FAA registry ZIP invalid: %s", exc)
        _hex_cache_loaded = True
        return

    # ── Step 1: find tracked-family MFR MDL CODEs from ACFTREF.txt ───────────
    # Build dict: FAA code → ICAO type (e.g. "1234" → "A20N")
    tracked_code_icao: dict[str, str] = {}
    try:
        with zf.open("ACFTREF.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                mfr = (row.get("MFR") or "").upper()
                model = (row.get("MODEL") or "").upper()
                icao_type = _model_to_icao(mfr, model)
                if icao_type and icao_type in config.AIRCRAFT_TYPES:
                    code = (row.get("CODE") or "").strip()
                    if code:
                        tracked_code_icao[code] = icao_type
    except Exception as exc:  # noqa: BLE001
        log.warning("ACFTREF.txt parse error: %s", exc)

    log.info("Found %d FAA model codes for tracked A320/737 families", len(tracked_code_icao))
    if not tracked_code_icao:
        _hex_cache_loaded = True
        return

    # ── Step 2: get N-numbers + hex codes from MASTER.txt ────────────────────
    count = 0
    try:
        with zf.open("MASTER.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                mfr_code = (row.get("MFR MDL CODE") or "").strip()
                icao_type = tracked_code_icao.get(mfr_code)
                if not icao_type:
                    continue
                hex_code = (row.get("MODE S CODE HEX") or "").strip().lower()
                n_number = (row.get("N-NUMBER") or "").strip()
                if hex_code and n_number:
                    _hex_cache[hex_code] = {
                        "registration": f"N{n_number}",
                        "typecode": icao_type,
                    }
                    count += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("MASTER.txt parse error: %s", exc)

    _hex_cache_loaded = True
    log.info("Loaded %d tracked A320/737-family US aircraft into hex cache", count)


# Track which hex codes were last seen on-ground so we can detect transitions
_was_on_ground: dict[str, bool] = {}


def fetch_landings(lookback_minutes: int) -> list[Sighting]:  # noqa: ARG001
    if not config.OPENSKY_ACTIVE:
        return []

    _load_faa_hex_cache()
    if not _hex_cache:
        log.warning("OpenSky: empty hex cache, skipping poll")
        return []

    auth = None
    if config.OPENSKY_USERNAME and config.OPENSKY_PASSWORD:
        auth = (config.OPENSKY_USERNAME, config.OPENSKY_PASSWORD)

    params = {**USA_BBOX}
    # Only request states for our known hex codes (comma-separated icao24 list)
    # The API accepts up to ~1000 values; C525 fleet in USA is ~2000 aircraft.
    # We chunk to stay safe.
    hex_list = list(_hex_cache.keys())
    chunk_size = 500
    sightings: list[Sighting] = []
    now_ts = int(time.time())

    for i in range(0, len(hex_list), chunk_size):
        chunk = hex_list[i : i + chunk_size]
        try:
            resp = requests.get(
                f"{BASE_URL}/states/all",
                params={**params, "icao24": chunk},
                auth=auth,
                timeout=20,
            )
            if resp.status_code == 429:
                log.warning("OpenSky rate-limited, skipping this poll cycle")
                break
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("OpenSky API error: %s", exc)
            break

        data = resp.json()
        states = data.get("states") or []

        for state in states:
            # state vector indices per OpenSky docs:
            # 0:icao24, 1:callsign, 2:origin_country, 3:time_position,
            # 4:last_contact, 5:longitude, 6:latitude, 7:baro_altitude,
            # 8:on_ground, 9:velocity, 10:true_track, 11:vertical_rate,
            # 12:sensors, 13:geo_altitude, 14:squawk, 15:spi, 16:position_source
            if len(state) < 9:
                continue

            icao24 = (state[0] or "").strip().lower()
            on_ground: bool = bool(state[8])

            prev_on_ground = _was_on_ground.get(icao24, True)
            _was_on_ground[icao24] = on_ground

            # Only emit a sighting when we detect the transition: airborne → on ground
            if not on_ground or prev_on_ground:
                continue

            info = _hex_cache.get(icao24, {})
            callsign = (state[1] or "").strip()
            arrived_utc = datetime.fromtimestamp(
                state[3] or now_ts, tz=timezone.utc
            ).isoformat()

            # Identify destination from GPS position at landing
            lat = state[6]
            lon = state[5]
            dest_icao = ""
            dest_name = ""
            if lat and lon:
                from airports import nearest_airport
                ap = nearest_airport(lat, lon)
                if ap:
                    dest_icao = ap["icao"]
                    dest_name = ap.get("municipality") or ap.get("name", "")

            # Enrich with flight history (origin/departed time + fallback dest)
            # /flights/aircraft requires auth — skip if no credentials (avoids 403)
            flight_info = _get_flight_info(icao24, state[3] or now_ts, auth) if auth else {}

            # Fall back to estArrivalAirport from flight history if GPS lookup missed
            if not dest_icao and flight_info.get("dest_icao"):
                dest_icao = flight_info["dest_icao"]

            sightings.append(
                Sighting(
                    source="opensky",
                    flight_id=f"opensky_{icao24}_{state[3] or now_ts}",
                    tail_number=info.get("registration", callsign),
                    ac_type=info.get("typecode", ""),
                    origin_icao=flight_info.get("origin_icao", ""),
                    origin_name="",
                    dest_icao=dest_icao,
                    dest_name=dest_name,
                    departed_utc=flight_info.get("departed_utc", ""),
                    arrived_utc=arrived_utc,
                    operator="",
                    tracking_url=(
                        f"https://globe.adsbexchange.com/?icao={icao24}"
                    ),
                )
            )

    if sightings:
        log.info("OpenSky: detected %d landing transition(s)", len(sightings))
    return sightings


def _get_flight_info(icao24: str, landed_ts: int, auth) -> dict:
    """
    Query OpenSky /flights/aircraft to get origin/destination for a just-landed
    aircraft. Looks back up to 12 hours to find the most recent flight record.
    """
    begin = landed_ts - 43200  # 12 hours back
    end = landed_ts + 300      # small buffer forward
    try:
        resp = requests.get(
            f"{BASE_URL}/flights/aircraft",
            params={"icao24": icao24, "begin": begin, "end": end},
            auth=auth,
            timeout=10,
        )
        if not resp.ok:
            return {}
        flights = resp.json()
        if not flights:
            return {}
        # Most recent flight first
        flight = sorted(flights, key=lambda f: f.get("lastSeen", 0), reverse=True)[0]
        result = {}
        origin = (flight.get("estDepartureAirport") or "").strip()
        dest = (flight.get("estArrivalAirport") or "").strip()
        first_seen = flight.get("firstSeen")
        if origin:
            result["origin_icao"] = origin.upper()
        if dest:
            result["dest_icao"] = dest.upper()
        if first_seen:
            result["departed_utc"] = datetime.fromtimestamp(
                first_seen, tz=timezone.utc
            ).isoformat()
        return result
    except Exception:  # noqa: BLE001
        return {}
