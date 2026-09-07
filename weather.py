"""
weather.py — OAT lookup with two sources.

Primary: **Iowa State Mesonet ASOS archive** — actual airport METAR
observations, hour-precision, free, no key. Best for high-DA analysis
because it's the tower's own thermometer, not a grid interpolation.
Coverage: virtually every US ASOS/AWOS station plus a wide European
network (EGxx, EDxx, LFxx, LIxx, LSxx, LOxx, EBxx, EHxx, EKxx, ENxx,
ESxx, EFxx, EIxx, LEXX, LPXX, LGxx, and more via IEM's international
networks). Docs: https://mesonet.agron.iastate.edu/request/download.phtml

Fallback: **Open-Meteo Historical** — ERA5 reanalysis grid (~9 km).
Used when METAR is unavailable (small GA fields, brand-new airports,
API timeout). Docs: https://open-meteo.com/en/docs/historical-weather-api
"""

import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

# In-process cache keyed by (lat_r2, lon_r2, YYYY-MM-DD, hour_utc)
_cache: dict[tuple, float | None] = {}

# Second cache keyed by (icao, YYYY-MM-DD, hour_utc) for METAR path so
# we don't re-hit IEM for the same airport-hour every time.
_metar_cache: dict[tuple, float | None] = {}

_ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_IEM_URL      = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch_metar_oat_c(icao: str, utc_dt: datetime,
                      max_offset_min: int = 45) -> float | None:
    """
    Pull the METAR temperature (°C) closest to ``utc_dt`` for the given ICAO
    from the Iowa State Mesonet ASOS archive. Returns the reading if within
    ``max_offset_min`` minutes of the requested time, else ``None``.

    Silent no-op if the airport isn't in IEM's network (small GA fields).
    """
    if not icao:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    icao = icao.upper()
    key = (icao, utc_dt.strftime("%Y-%m-%d"), utc_dt.hour)
    if key in _metar_cache:
        return _metar_cache[key]

    # Pull the ±1-day window so we tolerate the edge case where the
    # requested hour lies at day boundaries.
    d1 = (utc_dt - timedelta(days=0))
    d2 = (utc_dt + timedelta(days=0))
    params = {
        "station":     icao,
        "data":        "tmpc",
        "year1":       d1.year,  "month1": d1.month,  "day1": d1.day,
        "year2":       d2.year,  "month2": d2.month,  "day2": d2.day,
        "tz":          "Etc/UTC",
        "format":      "onlycomma",
        "latlon":      "no",
        "missing":     "null",
        "trace":       "null",
        "report_type": [3, 4],   # routine + special
    }
    try:
        resp = requests.get(_IEM_URL, params=params, timeout=12)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.debug("METAR fetch network error for %s @ %s: %s",
                  icao, utc_dt.isoformat(), exc)
        _metar_cache[key] = None
        return None

    body = (resp.text or "").strip()
    if not body or body.startswith("ERROR"):
        _metar_cache[key] = None
        return None

    reader = csv.DictReader(io.StringIO(body))
    best_offset = timedelta(minutes=max_offset_min)
    best_val: float | None = None
    for row in reader:
        raw = (row.get("tmpc") or "").strip()
        if not raw or raw.lower() in ("null", "m", "trace"):
            continue
        try:
            valid = datetime.fromisoformat(row["valid"]).replace(tzinfo=timezone.utc)
            tmpc  = float(raw)
        except (KeyError, ValueError):
            continue
        offset = abs(valid - utc_dt)
        if offset <= best_offset:
            best_offset = offset
            best_val    = tmpc

    _metar_cache[key] = best_val
    return best_val


def fetch_oat_c(lat: float, lon: float, utc_dt: datetime,
                icao: str | None = None) -> float | None:
    """
    Return OAT in °C at the given lat/lon at the given UTC datetime.

    Order of preference:
      1. If ``icao`` is provided, try the IEM METAR archive first — actual
         airport observation.
      2. Fall back to Open-Meteo (ERA5 reanalysis, ~9 km grid).

    Silent no-op returns ``None`` when neither source has data.
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)

    if icao:
        m = fetch_metar_oat_c(icao, utc_dt)
        if m is not None:
            return m

    key = (round(lat, 2), round(lon, 2), utc_dt.strftime("%Y-%m-%d"), utc_dt.hour)
    if key in _cache:
        return _cache[key]

    date_str  = utc_dt.strftime("%Y-%m-%d")
    days_ago  = (datetime.now(timezone.utc) - utc_dt).days
    target_ts = utc_dt.strftime("%Y-%m-%dT%H:00")

    try:
        if days_ago >= 5:
            resp = requests.get(_ARCHIVE_URL, params={
                "latitude":   lat,
                "longitude":  lon,
                "start_date": date_str,
                "end_date":   date_str,
                "hourly":     "temperature_2m",
                "timezone":   "UTC",
            }, timeout=10)
        else:
            resp = requests.get(_FORECAST_URL, params={
                "latitude":      lat,
                "longitude":     lon,
                "hourly":        "temperature_2m",
                "past_days":     max(days_ago, 1),
                "forecast_days": 1,
                "timezone":      "UTC",
            }, timeout=10)

        resp.raise_for_status()
        j     = resp.json()
        times = j["hourly"]["time"]
        temps = j["hourly"]["temperature_2m"]

        for t, temp in zip(times, temps):
            if t == target_ts and temp is not None:
                val = float(temp)
                _cache[key] = val
                return val

    except Exception as exc:
        log.debug("OAT fetch failed (%.2f, %.2f, %s h%d): %s",
                  lat, lon, date_str, utc_dt.hour, exc)

    _cache[key] = None
    return None
