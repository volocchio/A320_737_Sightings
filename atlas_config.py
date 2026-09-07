"""
atlas_config.py — ATLAS range improvement data and prospect scoring.

CJ4 (C25C / 525C-xxxx) is a separate product line and is NOT in ATLAS scope.

Cessna 525-family serial number → ICAO type mapping (authoritative):
  525-xxxx   → C525   CJ (0001–0359) · CJ1 (0360–0599) · CJ1+ (0600–0799)
  525-0800+  → C25M   M2  (FJ44-1AP-21 / dual-channel FADEC)
  525A-xxxx  → C25A   CJ2 (0001–~0193) · CJ2+ (~0194+)  (FJ44-3A-24 FADEC)
  525B-xxxx  → C25B   CJ3 / CJ3+
  525C-xxxx  → C25C   CJ4  ← not tracked

Performance groupings per Tamarack product management:
  CJ / CJ1        — similar ATLAS gain
  CJ1+ / M2       — similar (smaller) ATLAS gain
  CJ2             — large ATLAS gain
  CJ2+            — medium ATLAS gain (FADEC already boosted baseline)
  CJ3 / CJ3+      — medium ATLAS gain
"""

# ── Sub-variant level (preferred when serial number or ACFTREF resolves) ─────
#
# icao          — ICAO aircraft type code returned by flight tracking
# engine        — Williams-Rolls engine series
# fadec         — True = Full Authority Digital Engine Control
# baseline_nm   — real-world range at MCT (Max Continuous Thrust),
#                 source: Tamarack product management, 2026-06-26
# atlas_gain_nm — range extension from ATLAS winglets at MCT
ATLAS_SUBVARIANT: dict[str, dict] = {
    "CJ":      {
        "icao":          "C525",
        "label":         "CJ",
        "engine":        "FJ44-1A",
        "fadec":         False,
        "baseline_nm":   900,
        "atlas_gain_nm": 300,
    },
    "CJ1":     {
        "icao":          "C525",
        "label":         "CJ1",
        "engine":        "FJ44-1AP",
        "fadec":         False,
        "baseline_nm":   900,
        "atlas_gain_nm": 300,
    },
    "CJ1PLUS": {
        "icao":          "C525",
        "label":         "CJ1+",
        "engine":        "FJ44-1AP (FADEC)",
        "fadec":         True,
        "baseline_nm":   1000,
        "atlas_gain_nm": 200,
    },
    "M2":      {
        "icao":          "C25M",
        "label":         "M2",
        "engine":        "FJ44-1AP-21",
        "fadec":         True,
        "baseline_nm":   1000,
        "atlas_gain_nm": 200,
    },
    "CJ2":     {
        "icao":          "C25A",
        "label":         "CJ2",
        "engine":        "FJ44-2C",
        "fadec":         False,
        "baseline_nm":   1300,
        "atlas_gain_nm": 300,
    },
    "CJ2PLUS": {
        "icao":          "C25A",
        "label":         "CJ2+",
        "engine":        "FJ44-3A-24",
        "fadec":         True,
        "baseline_nm":   1300,
        "atlas_gain_nm": 200,
    },
    "CJ3":     {
        "icao":          "C25B",
        "label":         "CJ3",
        "engine":        "FJ44-3A",
        "fadec":         False,
        "baseline_nm":   1700,
        "atlas_gain_nm": 200,
    },
    "CJ3PLUS": {
        "icao":          "C25B",
        "label":         "CJ3+",
        "engine":        "FJ44-3AP-21",
        "fadec":         True,
        "baseline_nm":   1700,
        "atlas_gain_nm": 200,
    },
}

# ── ICAO-level fallback (used when sub-variant is unknown) ───────────────────
# Conservative gains: use the lower of the two sub-variant values.
ATLAS: dict[str, dict] = {
    "C525": {"label": "CJ/CJ1/CJ1+", "baseline_nm":  900, "atlas_gain_nm": 200},  # CJ/CJ1=900+300, CJ1+=1000+200
    "C25M": {"label": "M2",           "baseline_nm": 1000, "atlas_gain_nm": 200},
    "C25A": {"label": "CJ2/CJ2+",    "baseline_nm": 1300, "atlas_gain_nm": 200},   # CJ2=+300, CJ2+=+200
    "C25B": {"label": "CJ3/CJ3+",    "baseline_nm": 1700, "atlas_gain_nm": 200},
    "C25C": {"label": "CJ4",          "baseline_nm": 2200, "atlas_gain_nm":   0},   # not in ATLAS scope
    # Mustang: tracked as an "up-purchase" adjacent tier. baseline_nm is only
    # used by the fuel-stop chain detector to flag operators whose Mustang is
    # doing chain trips (signal that they're outgrowing the airplane). Not an
    # ATLAS product — atlas_gain_nm intentionally 0.
    "C510": {"label": "Mustang",      "baseline_nm": 1000, "atlas_gain_nm":   0},
}


# ── Scope tier — separates ATLAS-eligible aircraft from adjacent tracking ───
# atlas — full ATLAS scoring, WAT tables, hot/warm tiers, insights math
# up    — adjacent-below (Mustang). Tracked for up-purchase sales signal.
#         Never scored as an ATLAS prospect on its own.
# down  — adjacent-above (CJ4). Classified but not currently ingested.
_SCOPE_TIER_BY_TYPE: dict[str, str] = {
    "C525": "atlas",
    "C25A": "atlas",
    "C25B": "atlas",
    "C25M": "atlas",
    "C510": "up",
    "C25C": "down",
}


def scope_tier_for(ac_type: str | None, ac_subvariant: str | None = None) -> str | None:
    """
    Return the scope tier for an aircraft: 'atlas' | 'up' | 'down' | None.
    Sub-variant takes precedence when present (e.g. 'CJ4' → 'down').
    """
    sv = (ac_subvariant or "").upper().strip()
    if sv == "CJ4":
        return "down"
    if sv == "MUSTANG":
        return "up"
    return _SCOPE_TIER_BY_TYPE.get((ac_type or "").upper().strip())


# Scoring weights
WEIGHT_HOT  = 3   # trip > baseline  → currently needs fuel stop; ATLAS eliminates it
WEIGHT_WARM = 1   # trip 80–100% of baseline → range anxiety / payload restriction


def atlas_cfg(ac_type: str, subvariant: str = "") -> dict:
    """
    Return the ATLAS config dict for an aircraft, preferring sub-variant when known.

    ac_type    — ICAO type code  (e.g. 'C25B')
    subvariant — sub-variant key (e.g. 'CJ2PLUS'), or '' to use ICAO fallback
    """
    if subvariant:
        sv = ATLAS_SUBVARIANT.get(subvariant.upper())
        if sv:
            return sv
    return ATLAS.get((ac_type or "").upper(), {})


def trip_tier(distance_nm: float | None, ac_type: str,
              subvariant: str = "") -> str | None:
    """
    Classify a single trip by ATLAS benefit tier.
    Returns 'hot', 'warm', or None.

    hot  — distance exceeds baseline range (fuel stop required today)
    warm — distance is 80–100% of baseline (pushing the limit regularly)
    """
    if not distance_nm:
        return None
    cfg = atlas_cfg(ac_type, subvariant)
    if not cfg:
        return None
    baseline = cfg["baseline_nm"]
    if distance_nm > baseline:
        return "hot"
    if distance_nm > 0.80 * baseline:
        return "warm"
    return None


def atlas_range(ac_type: str, subvariant: str = "") -> int | None:
    """Baseline + ATLAS gain for a type/subvariant, or None if unknown."""
    cfg = atlas_cfg(ac_type, subvariant)
    if not cfg:
        return None
    return cfg["baseline_nm"] + cfg["atlas_gain_nm"]


# ── Performance groups (5 canonical buckets for the dashboard) ──────────────
# Per Nick (2026-06-23): the dashboard should aggregate by these 5 groups,
# not 8 sub-variants and not 4 ICAO types. CJ1+/M2 are grouped because they
# share the same FADEC FJ44-1AP-class engine and similar ATLAS gain profile.
# CJ4 (C25C) is intentionally excluded — separate product line.
PERF_GROUPS: tuple[dict, ...] = (
    {"key": "CJ_CJ1",   "label": "CJ / CJ1",   "subvariants": ("CJ", "CJ1"),
     "ac_types": ("C525",),       "baseline_nm": 1000, "atlas_gain_nm": 300},
    {"key": "CJ1P_M2",  "label": "CJ1+ / M2",  "subvariants": ("CJ1PLUS", "M2"),
     "ac_types": ("C525", "C25M"), "baseline_nm": 1000, "atlas_gain_nm": 150},
    {"key": "CJ2",      "label": "CJ2",        "subvariants": ("CJ2",),
     "ac_types": ("C25A",),       "baseline_nm": 1300, "atlas_gain_nm": 300},
    {"key": "CJ2P",     "label": "CJ2+",       "subvariants": ("CJ2PLUS",),
     "ac_types": ("C25A",),       "baseline_nm": 1300, "atlas_gain_nm": 300},
    {"key": "CJ3_CJ3P", "label": "CJ3 / CJ3+", "subvariants": ("CJ3", "CJ3PLUS"),
     "ac_types": ("C25B",),       "baseline_nm": 1800, "atlas_gain_nm": 200},
)

# Reverse lookups
_SUBVARIANT_TO_GROUP: dict[str, str] = {
    sv: g["key"] for g in PERF_GROUPS for sv in g["subvariants"]
}
_GROUP_BY_KEY: dict[str, dict] = {g["key"]: g for g in PERF_GROUPS}


def perf_group_for(ac_type: str, subvariant: str = "") -> str | None:
    """
    Return the performance-group KEY for an aircraft, or None if out of scope.

    Resolution order:
      1. Sub-variant (most specific) — direct lookup.
      2. ICAO type fallback — only unambiguous when type maps to a single group
         (C25A is ambiguous between CJ2 and CJ2+; default to CJ2 for that case).
    """
    sv = (subvariant or "").upper().strip()
    if sv:
        g = _SUBVARIANT_TO_GROUP.get(sv)
        if g:
            return g
    ac = (ac_type or "").upper().strip()
    # ICAO fallback — pick a sensible default group when sub-variant is unknown
    icao_fallback = {
        "C525": "CJ_CJ1",     # CJ/CJ1/CJ1+ all C525; default to CJ/CJ1 bucket
        "C25M": "CJ1P_M2",
        "C25A": "CJ2",        # CJ2 is the larger production run
        "C25B": "CJ3_CJ3P",
    }
    return icao_fallback.get(ac)


def perf_group_info(key: str) -> dict | None:
    """Return the full PERF_GROUPS dict for a given key (or None)."""
    return _GROUP_BY_KEY.get(key)


def perf_group_label(key: str) -> str:
    g = _GROUP_BY_KEY.get(key)
    return g["label"] if g else key
