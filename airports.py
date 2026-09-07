"""
airports.py — nearest airport lookup from lat/lon

Uses the free OurAirports dataset (no API key needed).

Data source order:
  1. Local files ``data/airports.csv`` + ``data/runways.csv`` (bundled with
     the repo — fastest cold start, works offline / behind a firewall).
  2. Fallback download from davidmegginson.github.io if the local files
     are missing (older code path).

Global coverage: `nearest_airport()` now resolves airports in any country,
not just the US, so EU/UK/other-region sightings from adsb.lol and FA can
land on the correct destination ICAO. `region_for_icao()` and
`region_for_sighting()` classify airports/flights into NA / EU_UK / OTHER
buckets for the regional dropdown filter.
"""

import csv
import io
import logging
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import requests

try:
    from timezonefinder import TimezoneFinder
    _tf = TimezoneFinder()
except ImportError:
    _tf = None

log = logging.getLogger(__name__)

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL  = "https://davidmegginson.github.io/ourairports-data/runways.csv"

_DATA_DIR       = Path(__file__).parent / "data"
_AIRPORTS_LOCAL = _DATA_DIR / "airports.csv"
_RUNWAYS_LOCAL  = _DATA_DIR / "runways.csv"

# List of (icao, name, municipality, lat, lon) for ALL airports (nearest_airport)
_airports: list[tuple] = []
# Global ICAO → (lat, lon) index for all airports (distance calculations)
_icao_index: dict[str, tuple[float, float]] = {}
# ICAO → elevation in feet
_elevation_index: dict[str, float] = {}
# airport_ident → longest runway length in feet
_runway_index: dict[str, int] = {}
# ICAO → region ("NA" | "EU_UK" | "OTHER") derived from OurAirports iso_country
_region_index: dict[str, str] = {}
# ICAO → ISO 3166-1 alpha-2 country code (raw, unmapped) for country-pair analytics
_country_index: dict[str, str] = {}
_loaded = False
_runways_loaded = False

# ── Region classification ────────────────────────────────────────────────────
# North America bucket: US + Canada + Mexico (shared CJ operator market).
_NA_COUNTRIES: frozenset[str] = frozenset({"US", "CA", "MX"})

# EU_UK bucket: EU-27 + UK + EEA/EFTA (Norway, Switzerland, Iceland,
# Liechtenstein). Nick's ATLAS Europe workstream targets this footprint.
_EU_UK_COUNTRIES: frozenset[str] = frozenset({
    "GB", "IE",                                        # British Isles
    "FR", "DE", "IT", "ES", "PT", "NL", "BE", "LU",    # Western Europe
    "AT", "CH", "LI",                                  # Alpine
    "DK", "NO", "SE", "FI", "IS",                      # Nordic
    "PL", "CZ", "SK", "HU", "SI", "HR",                # Central Europe
    "BA", "RS", "ME", "MK", "AL", "XK",                # W. Balkans
    "BG", "RO", "GR",                                  # SE Europe
    "EE", "LV", "LT",                                  # Baltics
    "MT", "CY",                                        # Mediterranean islands
    "MC", "AD", "SM", "VA",                            # microstates
})


def _region_from_iso(iso_country: str) -> str:
    """Map an ISO 3166-1 alpha-2 country code to our sales-region bucket."""
    c = (iso_country or "").upper()
    if c in _NA_COUNTRIES:
        return "NA"
    if c in _EU_UK_COUNTRIES:
        return "EU_UK"
    return "OTHER"


def _read_csv_source(path: Path, url: str, label: str) -> str | None:
    """Return CSV text from local file if present, otherwise download."""
    if path.exists():
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Could not read local %s CSV %s: %s", label, path, exc)
    log.info("Downloading OurAirports %s database…", label)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.warning("Could not download %s database: %s", label, exc)
        return None


def _load() -> None:
    global _airports, _loaded
    if _loaded:
        return
    text = _read_csv_source(_AIRPORTS_LOCAL, AIRPORTS_URL, "airports")
    if not text:
        _loaded = True
        return

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        country = (row.get("iso_country") or "").strip().upper()
        kind = row.get("type", "")
        icao = (row.get("gps_code") or row.get("ident") or "").strip()
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (ValueError, KeyError):
            continue
        if not icao:
            continue
        icao_up = icao.upper()
        _icao_index[icao_up] = (lat, lon)
        _region_index[icao_up] = _region_from_iso(country)
        if country:
            _country_index[icao_up] = country
        try:
            elev = float(row["elevation_ft"])
            _elevation_index[icao_up] = elev
        except (ValueError, KeyError, TypeError):
            pass
        # Nearest-airport list — keep the same airport-type filter but drop
        # the country == US restriction so EU/UK landings resolve too.
        if kind not in ("small_airport", "medium_airport", "large_airport"):
            continue
        name = (row.get("name") or "").strip()
        municipality = (row.get("municipality") or "").strip()
        _airports.append((icao_up, name, municipality, lat, lon))

    _loaded = True
    log.info("Loaded %d airports (nearest_airport index), %d ICAO entries, "
             "%d with elevation, region split NA=%d EU_UK=%d OTHER=%d",
             len(_airports), len(_icao_index), len(_elevation_index),
             sum(1 for r in _region_index.values() if r == "NA"),
             sum(1 for r in _region_index.values() if r == "EU_UK"),
             sum(1 for r in _region_index.values() if r == "OTHER"))


def _load_runways() -> None:
    """Load OurAirports runways.csv (local first, URL fallback)."""
    global _runways_loaded
    if _runways_loaded:
        return
    _load()  # airports must be loaded first
    text = _read_csv_source(_RUNWAYS_LOCAL, RUNWAYS_URL, "runways")
    if not text:
        _runways_loaded = True
        return

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        ident = (row.get("airport_ident") or "").strip().upper()
        try:
            length = int(float(row.get("length_ft") or 0))
        except (ValueError, TypeError):
            continue
        if ident and length > _runway_index.get(ident, 0):
            _runway_index[ident] = length

    _runways_loaded = True
    log.info("Loaded %d airport runway entries", len(_runway_index))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def icao_distance_nm(icao1: str, icao2: str) -> float | None:
    """
    Great-circle distance in nautical miles between two ICAO airport codes.
    Returns None if either airport is unknown or either code is blank.
    """
    _load()
    if not icao1 or not icao2:
        return None
    pos1 = _icao_index.get(icao1.upper())
    pos2 = _icao_index.get(icao2.upper())
    if pos1 is None or pos2 is None:
        return None
    km = _haversine_km(pos1[0], pos1[1], pos2[0], pos2[1])
    return round(km / 1.852, 1)  # km → nm


def nearest_airport(lat: float, lon: float, max_km: float = 15.0) -> dict:
    """
    Return the nearest airport within max_km of the given position.
    Global coverage (any country in the OurAirports dataset). Returns dict
    with keys: icao, name, municipality, dist_km — or empty dict if none
    within range.
    """
    _load()
    if not _airports:
        return {}

    best_dist = max_km
    best = None
    for icao, name, municipality, alat, alon in _airports:
        d = _haversine_km(lat, lon, alat, alon)
        if d < best_dist:
            best_dist = d
            best = {"icao": icao, "name": name, "municipality": municipality, "dist_km": round(d, 1)}

    return best or {}


@lru_cache(maxsize=2048)
def _icao_timezone(icao: str) -> str | None:
    """Return the IANA timezone string for an airport ICAO code, or None."""
    if _tf is None:
        return None
    _load()
    pos = _icao_index.get(icao.upper())
    if pos is None:
        return None
    return _tf.timezone_at(lat=pos[0], lng=pos[1])


def local_time_at_icao(arrived_utc: str, dest_icao: str) -> str:
    """
    Convert a UTC ISO-8601 arrival time to the local time at the destination
    airport.  Returns a string like "3:42 PM MDT" or "" if unavailable.
    """
    if not arrived_utc or not dest_icao:
        return ""
    tz_name = _icao_timezone(dest_icao.upper())
    if not tz_name:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt_utc = datetime.fromisoformat(arrived_utc.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        # e.g. "3:42 PM MDT"  (%-I is Linux-only; strip leading zero manually)
        return dt_local.strftime("%I:%M %p %Z").lstrip("0")
    except Exception:
        return ""


# ── Elevation / runway / density-altitude helpers ────────────────────────────

def airport_elevation_ft(icao: str) -> float | None:
    """Return field elevation in feet for an ICAO code, or None."""
    _load()
    return _elevation_index.get((icao or "").upper())


def airport_longest_runway_ft(icao: str) -> int | None:
    """Return the longest runway length in feet for an airport, or None."""
    _load_runways()
    return _runway_index.get((icao or "").upper())


def isa_temp_c(elevation_ft: float) -> float:
    """ISA standard temperature (°C) at the given pressure altitude (ft).
    Uses the ICAO standard lapse rate of 1.981 °C / 1 000 ft."""
    return 15.0 - elevation_ft * 0.001981


def density_altitude(elevation_ft: float, oat_c: float) -> float:
    """Density altitude in feet using the 120 ft/°C rule.
    DA = pressure_altitude + 120 × (OAT_C − ISA_C)"""
    return elevation_ft + 120.0 * (oat_c - isa_temp_c(elevation_ft))


def icao_coords(icao: str) -> tuple[float, float] | None:
    """Return (lat, lon) for an ICAO code, or None."""
    _load()
    return _icao_index.get((icao or "").upper())


# ── Region helpers ───────────────────────────────────────────────────────────

def airport_region(icao: str) -> str:
    """
    Sales-region bucket for an airport: 'NA' | 'EU_UK' | 'OTHER'.
    Returns 'OTHER' when the airport is unknown so callers get a valid
    string every time (never None).
    """
    _load()
    return _region_index.get((icao or "").upper(), "OTHER")


def airport_country(icao: str) -> str:
    """
    ISO 3166-1 alpha-2 country code for an airport. Returns '' when the
    airport is unknown. Used for EU country-pair breakdowns.
    """
    _load()
    return _country_index.get((icao or "").upper(), "")


def region_for_sighting(origin_icao: str | None, dest_icao: str | None) -> str:
    """
    Region-classify a sighting from its origin + destination ICAOs.

    Rules:
      * Both endpoints in the same bucket → that bucket.
      * One endpoint known, one blank → the known bucket.
      * Endpoints straddle two named buckets (e.g. NA→EU_UK) → 'OTHER'
        (transatlantic; belongs in neither market by itself).
      * Both blank → 'OTHER'.
    """
    _load()
    o = _region_index.get((origin_icao or "").upper())
    d = _region_index.get((dest_icao   or "").upper())
    if o and d:
        return o if o == d else "OTHER"
    return o or d or "OTHER"
