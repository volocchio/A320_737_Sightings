"""
costs.py — Per-sub-variant operating cost model for the Cessna 525 family.

Used by the dashboard to estimate fuel + engine reserve + parts reserve cost
per flight, given the aircraft sub-variant and great-circle distance.

All numbers are APPROXIMATIONS for sales-conversation purposes. Override the
constants as better data becomes available. Sources:
  - NBAA published cruise speeds & fuel burns
  - Conklin & de Decker variable cost ranges (2024-2025)
  - JSSI / TAP Blue / PowerAdvantage published rate cards (FJ44 series)
  - Cessna ProParts published rates

Add the Cessna 525 family cruise + burn numbers per sub-model:
"""

# Jet-A price (USD per gallon).  Retail FBO national average; update as needed.
FUEL_PRICE_USD_PER_GAL: float = 6.50

# Time penalty added to (distance / cruise_kts) to capture climb + descent.
# A 30-minute round-trip on top of cruise covers a typical FL370 profile.
CLIMB_DESCENT_HOURS: float = 0.50


# Per-sub-variant cost & performance assumptions.
#   cruise_kts          — NBAA long-range cruise true airspeed
#   fuel_gph            — trip-average fuel burn (gallons per hour)
#   engine_reserve_hr   — both-engine program reserve ($/hr aircraft, not per engine)
#   parts_reserve_hr    — ProParts / airframe parts reserve ($/hr aircraft)
COSTS: dict[str, dict] = {
    "CJ":      {"cruise_kts": 389, "fuel_gph": 120, "engine_reserve_hr": 600, "parts_reserve_hr": 200},
    "CJ1":     {"cruise_kts": 389, "fuel_gph": 115, "engine_reserve_hr": 600, "parts_reserve_hr": 210},
    "CJ1PLUS": {"cruise_kts": 404, "fuel_gph": 110, "engine_reserve_hr": 640, "parts_reserve_hr": 220},
    "M2":      {"cruise_kts": 404, "fuel_gph": 100, "engine_reserve_hr": 680, "parts_reserve_hr": 240},
    "CJ2":     {"cruise_kts": 413, "fuel_gph": 150, "engine_reserve_hr": 760, "parts_reserve_hr": 260},
    "CJ2PLUS": {"cruise_kts": 418, "fuel_gph": 140, "engine_reserve_hr": 820, "parts_reserve_hr": 270},
    "CJ3":     {"cruise_kts": 417, "fuel_gph": 165, "engine_reserve_hr": 880, "parts_reserve_hr": 290},
    "CJ3PLUS": {"cruise_kts": 417, "fuel_gph": 155, "engine_reserve_hr": 900, "parts_reserve_hr": 300},
}

# ICAO-level fallback when sub-variant is unknown (foreign tails)
_ICAO_FALLBACK = {
    "C525": "CJ",     # canonical base
    "C25A": "CJ2",
    "C25B": "CJ3",
    "C25M": "M2",
}


def flight_cost(
    distance_nm: float | None,
    subvariant: str = "",
    ac_type:    str = "",
) -> dict | None:
    """
    Estimate per-flight operating cost.

    Returns dict with:
        hours        — block time estimate (cruise + climb/descent)
        fuel_gal     — gallons burned
        fuel_usd     — Jet-A cost
        engine_usd   — engine program reserve
        parts_usd    — parts program reserve
        total_usd    — sum of the three
        gph, kts     — assumption values used
    Or None if no usable distance / no model match.
    """
    if not distance_nm or distance_nm <= 0:
        return None

    key = (subvariant or "").upper()
    cfg = COSTS.get(key)
    if not cfg:
        fallback_key = _ICAO_FALLBACK.get((ac_type or "").upper())
        if fallback_key:
            cfg = COSTS[fallback_key]
    if not cfg:
        return None

    kts        = cfg["cruise_kts"]
    gph        = cfg["fuel_gph"]
    eng_per_hr = cfg["engine_reserve_hr"]
    prt_per_hr = cfg["parts_reserve_hr"]

    hours    = distance_nm / kts + CLIMB_DESCENT_HOURS
    fuel_gal = gph * hours
    fuel_usd = fuel_gal * FUEL_PRICE_USD_PER_GAL
    eng_usd  = eng_per_hr * hours
    prt_usd  = prt_per_hr * hours
    total    = fuel_usd + eng_usd + prt_usd

    return {
        "hours":      round(hours, 2),
        "fuel_gal":   int(round(fuel_gal)),
        "fuel_usd":   int(round(fuel_usd)),
        "engine_usd": int(round(eng_usd)),
        "parts_usd":  int(round(prt_usd)),
        "total_usd":  int(round(total)),
        "gph":        gph,
        "kts":        kts,
    }
