"""
tamarack_fleet.py — Tamarack ATLAS fleet registry and FAA N-number lookup.

Tracks which Cessna 525-family serial numbers have ATLAS winglets installed,
including aircraft where winglets were subsequently removed (decommissioned).

Provides FAA registry lookup to resolve N-numbers ↔ serial numbers so that
live sightings can be tagged as Tamarack fleet aircraft.
"""

import io
import json
import logging
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import urllib.request as _req
except ImportError:
    _req = None

log = logging.getLogger(__name__)

# ── Active ATLAS fleet: S/N → install date ────────────────────────────────────
# 525-XXXX (≤0799) = CJ / CJ1 / CJ1+ (C525)   525-XXXX (≥0800) = M2 (C25M)
# 525AXXXX = CJ2 / CJ2+ (C25A)   525BXXXX = CJ3 / CJ3+ (C25B)
FLEET_ACTIVE: dict[str, date] = {
    "525-0604": date(2016,  2,  8),
    "525-0044": date(2016,  4, 29),
    "525-0012": date(2016,  9,  9),
    "525B-0018": date(2016, 10, 29),
    "525-0840": date(2017,  1, 17),
    "525-0093": date(2017,  1, 18),
    "525-0081": date(2017,  2, 18),
    "525-0231": date(2017,  2, 22),
    "525-0355": date(2017,  3,  1),
    "525-0525": date(2017,  3,  4),
    "525-0531": date(2017,  3, 10),
    "525-0018": date(2017,  3, 17),
    "525-0130": date(2017,  3, 17),
    "525-0369": date(2017,  3, 29),
    "525-0544": date(2017,  3, 30),
    "525-0200": date(2017,  4,  6),
    "525-0138": date(2017,  4, 19),
    "525-0545": date(2017,  5,  2),
    "525-0121": date(2017,  6, 27),
    "525-0470": date(2017,  6, 28),
    "525-0279": date(2017,  7, 28),
    "525-0645": date(2017,  8, 11),
    "525-0306": date(2017, 10, 27),
    "525A-0327": date(2017, 11, 14),
    "525-0680": date(2017, 11, 20),
    "525-0358": date(2017, 12, 11),
    "525-0134": date(2017, 12, 13),
    "525A-0338": date(2017, 12, 15),
    "525A-0062": date(2018,  1,  2),
    "525-0506": date(2018,  1, 20),
    "525-0683": date(2018,  1, 30),
    "525B-0243": date(2018,  3,  4),
    "525B-0460": date(2018,  3, 20),
    "525B-0019": date(2018,  3, 23),
    "525-0611": date(2018,  3, 26),
    "525-0666": date(2018,  3, 29),
    "525B-0371": date(2018,  3, 30),
    "525-0621": date(2018,  4,  2),
    "525A-0076": date(2018,  4,  7),
    "525B-0515": date(2018,  4,  9),
    "525B-0179": date(2018,  4, 10),
    "525-0422": date(2018,  4, 30),
    "525-0863": date(2018,  5,  8),
    "525A-0203": date(2018,  5, 20),
    "525A-0181": date(2018,  6, 11),
    "525A-0008": date(2018,  6, 12),
    "525A-0507": date(2018,  6, 25),
    "525-0152": date(2018,  7,  3),
    "525-0445": date(2018,  7,  9),
    "525A-0103": date(2018,  7, 16),
    "525A-0219": date(2018,  7, 23),
    "525-0513": date(2018,  7, 31),
    "525B-0211": date(2018,  8,  6),
    "525-0311": date(2018,  8,  7),
    "525-0314": date(2018,  8, 13),
    "525-0128": date(2018,  8, 20),
    "525-0305": date(2018,  8, 27),
    "525A-0038": date(2018,  8, 27),
    "525A-0184": date(2018,  9,  3),
    "525-0510": date(2018,  9, 12),
    "525A-0231": date(2018,  9, 18),
    "525A-0504": date(2018,  9, 24),
    "525B-0206": date(2018,  9, 27),
    "525-0199": date(2018,  9, 27),
    "525A-0006": date(2018, 10,  1),
    "525A-0483": date(2018, 10,  2),
    "525A-0211": date(2018, 10,  8),
    "525A-0315": date(2018, 10, 15),
    "525-0127": date(2018, 10, 22),
    "525B-0294": date(2018, 10, 29),
    "525-0462": date(2018, 11, 12),
    "525-0143": date(2018, 11, 22),
    "525A-0165": date(2018, 12, 10),
    "525A-0477": date(2018, 12, 18),
    "525-0345": date(2018, 12, 20),
    "525-0363": date(2019,  1, 10),
    "525A-0429": date(2019,  1, 11),
    "525-0158": date(2019,  1, 11),
    "525A-0087": date(2019,  1, 14),
    "525-0284": date(2019,  1, 26),
    "525A-0243": date(2019,  1, 28),
    "525-0218": date(2019,  2,  8),
    "525A-0208": date(2019,  3,  5),
    "525-0132": date(2019,  3,  6),
    "525-0217": date(2019,  3, 18),
    "525-0418": date(2019,  5, 16),
    "525-0271": date(2019,  7,  8),
    "525-0500": date(2019,  7, 22),
    "525-0553": date(2019,  8, 23),
    "525B-0562": date(2019,  9,  5),
    "525-0201": date(2019,  9, 20),
    "525-0343": date(2019, 11, 18),
    "525-0194": date(2019, 12,  6),
    "525-0321": date(2020,  1, 17),
    "525A-0372": date(2020,  3,  5),
    "525-0192": date(2020,  3,  6),
    "525A-0344": date(2020,  4,  3),
    "525-0073": date(2020,  4, 28),
    "525-0491": date(2020,  5, 15),
    "525-0374": date(2020,  5, 29),
    "525A-0131": date(2020,  6,  9),
    "525-0041": date(2020,  6, 25),
    "525B-0072": date(2020,  7,  9),
    "525-0452": date(2020,  7, 24),
    "525-0623": date(2020,  8,  4),
    "525-0113": date(2020,  8, 22),
    "525-0060": date(2020,  9, 24),
    "525B-0520": date(2020, 10,  6),
    "525A-0239": date(2020, 10,  9),
    "525-0899": date(2020, 10, 15),
    "525-0429": date(2020, 10, 19),
    "525-0934": date(2020, 10, 29),
    "525-0822": date(2020, 12,  1),
    "525A-0045": date(2020, 12, 19),
    "525A-0312": date(2020, 12, 22),
    "525-0353": date(2021,  1, 18),
    "525-0812": date(2021,  2,  2),
    "525-0117": date(2021,  2, 21),
    "525A-0200": date(2021,  3,  5),
    "525-1010": date(2021,  3, 23),
    "525A-0127": date(2021,  4,  1),
    "525-0803": date(2021,  4,  2),
    "525A-0216": date(2021,  4, 26),
    "525B-0537": date(2021,  4, 26),
    "525-0272": date(2021,  5, 11),
    "525-0225": date(2021,  5, 25),
    "525A-0122": date(2021,  5, 29),
    "525-0865": date(2021,  6, 16),
    "525A-0192": date(2021,  6, 22),
    "525-0027": date(2021,  8,  2),
    "525A-0242": date(2021,  8, 12),
    "525A-0025": date(2021,  9,  8),
    "525-0116": date(2021,  9, 13),
    "525-0556": date(2021, 10,  5),
    "525-0938": date(2021, 10, 27),
    "525-0440": date(2021, 11,  9),
    "525-0406": date(2021, 11, 19),
    "525-0169": date(2021, 12, 17),
    "525-0493": date(2021, 12, 30),
    "525-0826": date(2022,  1, 12),
    "525-0260": date(2022,  1, 28),
    "525-0919": date(2022,  2, 22),
    "525A-0418": date(2022,  3,  8),
    "525-0063": date(2022,  3, 23),
    "525-0227": date(2022,  4,  1),
    "525A-0100": date(2022,  4, 22),
    "525A-0240": date(2022,  4, 25),
    "525-0278": date(2022,  6, 10),
    "525-0629": date(2022,  6, 14),
    "525A-0060": date(2022,  6, 24),
    "525-0036": date(2022,  7, 14),
    "525-0658": date(2022,  7, 28),
    "525-0509": date(2022,  8, 12),
    "525A-0480": date(2022,  8, 19),
    "525-1119": date(2022,  8, 30),
    "525-0137": date(2022,  9,  8),
    "525-0333": date(2022,  9, 21),
    "525A-0439": date(2022, 10,  3),
    "525-0140": date(2022, 10,  6),
    "525A-0381": date(2022, 10, 26),
    "525-0054": date(2022, 11,  7),
    "525-0868": date(2022, 12, 20),
    "525-0995": date(2023,  1, 12),
    "525A-0505": date(2023,  1, 23),
    "525-0388": date(2023,  1, 23),
    "525A-0061": date(2023,  2,  8),
    "525B-0245": date(2023,  2, 24),
    "525-0360": date(2023,  3,  7),
    "525B-0681": date(2023,  3, 10),
    "525A-0066": date(2023,  3, 30),
    "525-0264": date(2023,  5,  8),
    "525-0189": date(2023,  5, 24),
    "525-0043": date(2023,  6, 14),
    "525-0270": date(2023,  6, 26),
    "525-0603": date(2023,  7, 28),
    "525A-0153": date(2023,  8, 30),
    "525-0016": date(2023, 12, 23),
    "525B-0505": date(2024,  3, 30),
    "525B-0403": date(2024,  4, 27),
    "525A-0428": date(2024,  5, 15),
    "525-0298": date(2024,  5, 28),
    "525-0354": date(2024,  6, 22),
    "525B-0014": date(2024,  6, 25),
    "525A-0161": date(2024,  7, 19),
    "525-0010": date(2024,  8, 16),
    "525-0613": date(2024,  9, 20),
    "525-0013": date(2024, 10, 11),
    "525-0976": date(2024, 10, 24),
    "525-0212": date(2024, 11, 26),
    "525A-0430": date(2024, 12, 18),
    "525-0498": date(2025,  1, 10),
    "525-0372": date(2025,  1, 17),
    "525-0157": date(2025,  1, 31),
    "525B-0297": date(2025,  2, 20),
    "525-0049": date(2025,  3, 20),
    "525-0275": date(2025,  4,  4),
    "525-0932": date(2025,  4, 18),
    "525A-0162": date(2025,  6, 25),
    "525-0107": date(2025,  8, 28),
    "525A-0114": date(2025,  8, 31),
    "525B-0325": date(2025,  9, 10),
    "525-0395": date(2025,  9, 30),
    "525-0163": date(2025, 10,  1),
    "525-0367": date(2025, 10,  8),
    "525A-0329": date(2025, 10, 31),
    "525B-0558": date(2025, 11, 17),
    "525-0325": date(2026,  1, 13),
    "525A-0120": date(2026,  6, 17),
}

# ── Decommissioned (winglets removed): S/N → (install_date, remove_date) ─────
# These aircraft had ATLAS installed but had winglets removed.
# The 2019 batch was removed during the FAA grounding so aircraft could continue flying.
FLEET_REMOVED: dict[str, tuple[date, date]] = {
    "525A-0449": (date(2018,  5, 20), date(2018, 11, 30)),
    "525B-0486": (date(2018,  3, 18), date(2019,  5, 29)),
    "525B-0244": (date(2018,  3, 11), date(2019,  5, 30)),
    "525B-0025": (date(2018,  4,  2), date(2019,  8, 12)),
    "525B-0284": (date(2018,  4, 18), date(2019, 12, 17)),
    "525-0905": (date(2021,  7, 20), date(2023,  8, 16)),
    "525B-0255": (date(2021, 12,  8), date(2024,  5, 22)),
    "525A-0380": (date(2023,  3, 23), date(2025,  3, 13)),
    "525B-0001": (date(2022, 12,  7), date(2025,  4, 29)),
}

# ── Derived lookups ────────────────────────────────────────────────────────────
_ACTIVE_SNS  = frozenset(FLEET_ACTIVE.keys())
_REMOVED_SNS = frozenset(FLEET_REMOVED.keys())
_ALL_SNS     = _ACTIVE_SNS | _REMOVED_SNS

FLEET_COUNT_ACTIVE  = len(_ACTIVE_SNS)
FLEET_COUNT_REMOVED = len(_REMOVED_SNS)
FLEET_COUNT_TOTAL   = len(_ALL_SNS)


def sn_variant(sn: str) -> str:
    """
    Return ICAO type code from a Cessna 525-family serial number.

    Mapping (authoritative, per Cessna TCDS):
        525-0001..0799  → C525  (CJ / CJ1 / CJ1+)
        525-0800+       → C25M  (M2  — FADEC FJ44-1AP-21)
        525A-xxxx       → C25A  (CJ2 / CJ2+)
        525B-xxxx       → C25B  (CJ3 / CJ3+)
        525C-xxxx       → C25C  (CJ4)
    """
    s = sn.upper().strip()
    if "525A" in s:
        return "C25A"
    if "525B" in s:
        return "C25B"
    if "525C" in s:
        return "C25C"
    # Base 525 — distinguish M2 (>=0800) from CJ/CJ1/CJ1+ (<=0799)
    if s.startswith("525-"):
        try:
            seq = int(s[4:].lstrip("0") or "0")
            return "C25M" if seq >= 800 else "C525"
        except ValueError:
            return "C525"
    return "C525"


# ── Cessna 525-family serial → sub-variant breakpoints (authoritative) ──
# Base 525 (no letter suffix) splits by serial-number block:
_CJ_END        = 359   # 525-0001..0359 → CJ
_CJ1_END       = 599   # 525-0360..0599 → CJ1
_CJ1PLUS_END   = 799   # 525-0600..0799 → CJ1+
# 525-0800+   → M2  (Gen 1 = 0800..1047, G3000NG = 1048..1109, Gen 2 = 1110+)
# Letter-suffix models split CJx vs CJx+ at a single serial:
_CJ2PLUS_START = 194   # 525A-~0194+ → CJ2+ (approx — Nick: "194 or something")
_CJ3PLUS_START = 583   # 525B-0583+  → CJ3+ (per Nick / FlyingMag)


def subvariant_from_sn(sn: str) -> str:
    """
    Derive the ATLAS sub-variant key from a Cessna 525-family serial number.

    Mapping (authoritative, per Cessna model designators):
        525-0001..0359 → CJ        (C525)
        525-0360..0599 → CJ1       (C525)
        525-0600..0799 → CJ1PLUS   (C525)
        525-0800+      → M2        (C25M)
                          Gen 1     = 525-0800..1047  (G3000)
                          G3000NG   = 525-1048..1109  (G3000NG)
                          Gen 2     = 525-1110+       (redesigned cabin)
                          Gen 3     = announced 2024, expected 2027 (TBD S/N)
                          NOTE: all M2 generations share the single 'M2'
                          sub-variant key — generation is a display detail only.
        525A-0001..~0193 → CJ2     (C25A)
        525A-~0194+    → CJ2PLUS   (C25A)
        525B-0001..0582 → CJ3      (C25B)
        525B-0583+     → CJ3PLUS   (C25B)
                          CJ3 Gen 2 (cert Oct 2025) shares the CJ3PLUS bucket
                          — generation is a display detail only.
        525C-xxxx      → CJ4       (C25C, not in ATLAS scope)

    Returns a sub-variant key matching ATLAS_SUBVARIANT in atlas_config.py,
    or '' if the serial format is unrecognised.
    """
    sn = sn.strip().upper()

    # Base 525: split CJ / CJ1 / CJ1+ / M2 by serial block
    if sn.startswith("525-"):
        try:
            seq = int(sn[4:].lstrip("0") or "0")
        except ValueError:
            return ""
        if seq <= _CJ_END:
            return "CJ"
        if seq <= _CJ1_END:
            return "CJ1"
        if seq <= _CJ1PLUS_END:
            return "CJ1PLUS"
        return "M2"

    # Letter-suffix models: 525A / 525B / 525C
    # FAA registry stores these either with or without a dash between the
    # letter and the sequence ("525A-0200" or "525A-0200"), so the regex must
    # accept both. Was missing the optional dash, leaving ~115 sightings with
    # NULL subvariant after rescrub (2026-06-24).
    import re
    m = re.match(r"^525([ABC])-?0*(\d+)$", sn)
    if not m:
        return ""
    series, seq = m.group(1), int(m.group(2))

    if series == "A":
        return "CJ2PLUS" if seq >= _CJ2PLUS_START else "CJ2"
    if series == "B":
        return "CJ3PLUS" if seq >= _CJ3PLUS_START else "CJ3"
    if series == "C":
        return "CJ4"
    return ""


# ── Pre-computed sub-variant for every fleet S/N ─────────────────────────────
# Available immediately without FAA registry download.
FLEET_ACTIVE_SUBVARIANT: dict[str, str] = {
    sn: subvariant_from_sn(sn) for sn in FLEET_ACTIVE
}
FLEET_REMOVED_SUBVARIANT: dict[str, str] = {
    sn: subvariant_from_sn(sn) for sn in FLEET_REMOVED
}


# ── FAA registry N-number ↔ serial number lookup ──────────────────────────────
# The FAA ReleasableAircraft database maps N-numbers to manufacturer S/Ns.
# We download once, filter to 525-series, and cache locally as JSON.

_CACHE_PATH = Path(__file__).parent / "faa_sn_cache.json"
_FAA_ZIP_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"

_nnum_to_sn:    dict[str, str] = {}   # "N604CJ"  → "525-0604"
_sn_to_nnum:    dict[str, str] = {}   # "525-0604" → "N604CJ"
_nnum_to_model: dict[str, str] = {}   # "N604CJ"  → "CITATION CJ"  (FAA model name)
_nnum_to_year:  dict[str, int] = {}   # "N604CJ"  → 1998
_registry_loaded = False


def _normalize_faa_sn(raw: str) -> str:
    """
    Convert FAA-format serial number to Tamarack fleet key format.

    Canonical Cessna 525-family format uses a dash between the model code
    and the sequence number (matches the TCDS A1WI):
        525-XXXX    (base CJ family: CJ / CJ1 / CJ1+ / M2)
        525A-XXXX   (CJ2 / CJ2+)
        525B-XXXX   (CJ3 / CJ3+)
        525C-XXXX   (CJ4, out of scope)

    FAA inputs we've seen in the wild:
        "5250604"   (base CJ, no dash)            → "525-0604"
        "525-0604"  (base CJ, already dashed)     → "525-0604"
        "525A0200"  (letter-suffix, no dash)      → "525A-0200"
        "525A-0200" (letter-suffix, already dashed) → "525A-0200"
    """
    raw = raw.strip().upper()
    # Base CJ: "525" followed immediately by digits → insert dash
    if raw.startswith("525") and len(raw) > 3 and raw[3:4].isdigit():
        return f"525-{raw[3:]}"
    # Letter-suffix: "525A0200" / "525B0018" / "525C0001" → insert dash
    # (FAA sometimes omits the dash for letter-suffix forms; canonicalise here
    # so fleet-dict lookups and DB joins always work.)
    import re
    m = re.match(r"^(525[ABC])(\d+)$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return raw


def subvariant_from_faa_model(model: str) -> str:
    """
    Map a FAA model string (e.g. "CITATION CJ2+") to our internal sub-variant key.

    Sub-variant keys:
        CJ       — original CJ,  FJ44-1A,        no FADEC
        CJ1      — CJ1,          FJ44-1AP,        no FADEC
        CJ1PLUS  — CJ1+,         FJ44-1AP-21,     FADEC
        M2       — M2,           FJ44-4A,         FADEC
        CJ2      — CJ2,          FJ44-2C,         no FADEC
        CJ2PLUS  — CJ2+,         FJ44-3AP-21,     FADEC
        CJ3      — CJ3,          FJ44-3A,         no FADEC
        CJ3PLUS  — CJ3+,         FJ44-3AP-21,     FADEC
    Returns "" if the model cannot be identified.
    """
    m = model.upper().strip()
    # Order matters: check "+" variants before base variants
    if "CJ3+" in m or "CJ3 PLUS" in m or "CJ3PLUS" in m:
        return "CJ3PLUS"
    if "CJ3" in m:
        return "CJ3"
    if "CJ2+" in m or "CJ2 PLUS" in m or "CJ2PLUS" in m:
        return "CJ2PLUS"
    if "CJ2" in m:
        return "CJ2"
    if "CJ1+" in m or "CJ1 PLUS" in m or "CJ1PLUS" in m:
        return "CJ1PLUS"
    if "M2" in m:
        return "M2"
    if "CJ1" in m:
        return "CJ1"
    if "CJ" in m:
        return "CJ"
    return ""


def nnum_to_subvariant(nnum: str) -> str:
    """
    Return sub-variant key for an N-number (e.g. 'CJ2PLUS'), or '' if unknown.

    Resolution order:
      1. Serial number from FAA registry + TCDS breakpoints  (most reliable)
      2. FAA ACFTREF.txt model name                          (good fallback)
    """
    if not _registry_loaded:
        load_faa_registry()
    nnum = nnum.upper().strip()

    # Path 1: S/N → TCDS sub-variant
    sn = _nnum_to_sn.get(nnum, "")
    if sn:
        sv = subvariant_from_sn(sn)
        if sv:
            return sv

    # Path 2: ACFTREF model name
    model = _nnum_to_model.get(nnum, "")
    return subvariant_from_faa_model(model) if model else ""


def nnum_to_faa_model(nnum: str) -> str:
    """Return full FAA model name for an N-number (e.g. 'CITATION CJ2+'), or ''."""
    if not _registry_loaded:
        load_faa_registry()
    return _nnum_to_model.get(nnum.upper().strip(), "")


def nnum_to_year(nnum: str) -> int | None:
    """Return manufacture year for an N-number, or None."""
    if not _registry_loaded:
        load_faa_registry()
    return _nnum_to_year.get(nnum.upper().strip())


def load_faa_registry(force: bool = False) -> bool:
    """
    Load N-number ↔ S/N mapping from local cache, or download from FAA if missing.
    Returns True if successfully loaded.
    Only Cessna 525-series aircraft are retained.
    """
    global _registry_loaded
    if _registry_loaded and not force:
        return True

    # Try local cache first
    if _CACHE_PATH.exists() and not force:
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            # Apply current normalizer to cached S/Ns — older caches may have
            # un-dashed letter-suffix S/Ns (525A0062) that won't match
            # FLEET_ACTIVE_SUBVARIANT keys (525A-0062).
            cached_nn_to_sn = data.get("nnum_to_sn", {})
            cached_sn_to_nn = data.get("sn_to_nnum", {})
            for nnum, sn in cached_nn_to_sn.items():
                _nnum_to_sn[nnum] = _normalize_faa_sn(sn)
            for sn, nnum in cached_sn_to_nn.items():
                _sn_to_nnum[_normalize_faa_sn(sn)] = nnum
            _nnum_to_model.update(data.get("nnum_to_model", {}))
            _nnum_to_year.update({k: int(v) for k, v in data.get("nnum_to_year", {}).items() if v})
            _registry_loaded = True
            log.info("FAA registry loaded from cache: %d 525-series aircraft", len(_nnum_to_sn))
            return True
        except Exception as e:
            log.warning("FAA cache read failed, will re-download: %s", e)

    if _req is None:
        log.error("urllib.request not available; FAA registry not loaded")
        return False

    log.info("Downloading FAA aircraft registry from %s …", _FAA_ZIP_URL)
    try:
        # FAA blocks the default Python urllib User-Agent — must send a
        # browser-like UA or the request returns HTTP 403 Forbidden.
        req = _req.Request(
            _FAA_ZIP_URL,
            headers={"User-Agent":
                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/120.0 Safari/537.36"},
        )
        resp = _req.urlopen(req, timeout=120)
        raw_bytes = resp.read()

        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))

        # ── Step 1: Build CODE → MODEL from ACFTREF.txt ─────────────────────
        acftref_name = next(
            (n for n in zf.namelist() if "ACFTREF" in n.upper() and n.endswith(".txt")),
            None,
        )
        code_to_model: dict[str, str] = {}
        if acftref_name:
            with zf.open(acftref_name) as f:
                raw = f.read()
            # FAA files now ship with UTF-8 BOM; strip it so headers parse
            try:
                ref_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                ref_text = raw.decode("latin-1")
            ref_lines = ref_text.splitlines()
            ref_headers = [h.strip() for h in ref_lines[0].split(",")]
            try:
                code_col  = ref_headers.index("CODE")
                model_col = ref_headers.index("MODEL")
                mfr_col   = ref_headers.index("MFR")
            except ValueError:
                code_col = model_col = mfr_col = None

            if code_col is not None:
                for line in ref_lines[1:]:
                    parts = line.split(",")
                    if len(parts) <= max(code_col, model_col, mfr_col):
                        continue
                    mfr   = parts[mfr_col].strip().upper()
                    model = parts[model_col].strip().upper()
                    # Only keep Cessna Citation 525-family models
                    if "CESSNA" in mfr and any(k in model for k in
                                                ("CJ", "CITATION C", "525")):
                        code_to_model[parts[code_col].strip()] = model
            log.info("ACFTREF: loaded %d Cessna A320/737-family model codes", len(code_to_model))
        else:
            log.warning("ACFTREF.txt not found in FAA zip; sub-variant resolution unavailable")

        # ── Step 2: Parse MASTER.txt for 525-series registrations ───────────
        master_name = next(
            (n for n in zf.namelist() if "MASTER" in n.upper() and n.endswith(".txt")),
            None,
        )
        if not master_name:
            log.error("MASTER.txt not found in FAA zip; names=%s", zf.namelist())
            return False

        with zf.open(master_name) as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        lines = text.splitlines()
        headers = [h.strip() for h in lines[0].split(",")]

        try:
            nnum_idx  = headers.index("N-NUMBER")
            sn_idx    = headers.index("SERIAL NUMBER")
            mfr_idx   = headers.index("MFR MDL CODE")
            year_idx  = headers.index("YEAR MFR")
        except ValueError:
            # Fallback: try without optional columns
            try:
                nnum_idx = headers.index("N-NUMBER")
                sn_idx   = headers.index("SERIAL NUMBER")
                mfr_idx  = year_idx = None
            except ValueError:
                log.error("FAA MASTER.txt missing expected columns; got: %s", headers)
                return False

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(nnum_idx, sn_idx):
                continue
            raw_nnum = parts[nnum_idx].strip().upper()
            raw_sn   = parts[sn_idx].strip().upper()
            if not raw_nnum or not raw_sn:
                continue
            # Only retain 525-series
            if not raw_sn.startswith("525"):
                continue
            normalized_sn = _normalize_faa_sn(raw_sn)
            nnum = f"N{raw_nnum}"
            _nnum_to_sn[nnum] = normalized_sn
            _sn_to_nnum[normalized_sn] = nnum

            # Model name from ACFTREF lookup
            if mfr_idx is not None and len(parts) > mfr_idx:
                mfr_code = parts[mfr_idx].strip()
                model = code_to_model.get(mfr_code, "")
                if model:
                    _nnum_to_model[nnum] = model

            # Year of manufacture
            if year_idx is not None and len(parts) > year_idx:
                raw_year = parts[year_idx].strip()
                if raw_year.isdigit():
                    _nnum_to_year[nnum] = int(raw_year)

        # Persist cache
        _CACHE_PATH.write_text(
            json.dumps({
                "nnum_to_sn":    _nnum_to_sn,
                "sn_to_nnum":    _sn_to_nnum,
                "nnum_to_model": _nnum_to_model,
                "nnum_to_year":  _nnum_to_year,
            }),
            encoding="utf-8",
        )
        log.info(
            "FAA registry cached: %d 525-series aircraft, %d with model names",
            len(_nnum_to_sn), len(_nnum_to_model),
        )
        _registry_loaded = True
        return True

    except Exception as e:
        log.error("FAA registry download failed: %s", e)
        return False


def nnum_to_sn(nnum: str) -> Optional[str]:
    """Return the Tamarack-format S/N for a given N-number, or None."""
    if not _registry_loaded:
        load_faa_registry()
    return _nnum_to_sn.get(nnum.upper().strip())


def sn_to_nnum_lookup(sn: str) -> Optional[str]:
    """Return the N-number for a given Tamarack S/N, or None."""
    if not _registry_loaded:
        load_faa_registry()
    return _sn_to_nnum.get(sn.upper().strip())


def fleet_status(nnum: str) -> Optional[dict]:
    """
    Return fleet membership status for an N-number, or None if not a Tamarack aircraft.

    Returns dict:
        sn           — Tamarack serial number
        install_date — date winglets were installed
        remove_date  — date winglets were removed, or None if still active
        active       — True if currently flying with ATLAS
    """
    sn = nnum_to_sn(nnum)
    if sn is None:
        return None
    sn_upper = sn.upper()

    if sn_upper in _ACTIVE_SNS:
        return {
            "sn":           sn,
            "install_date": FLEET_ACTIVE[sn_upper],
            "remove_date":  None,
            "active":       True,
            "subvariant":   FLEET_ACTIVE_SUBVARIANT.get(sn_upper, ""),
        }
    if sn_upper in _REMOVED_SNS:
        install, remove = FLEET_REMOVED[sn_upper]
        return {
            "sn":           sn,
            "install_date": install,
            "remove_date":  remove,
            "active":       False,
            "subvariant":   FLEET_REMOVED_SUBVARIANT.get(sn_upper, ""),
        }
    return None


# ── Worldwide production fleet totals (per sub-variant) ──────────────────────
# Denominator for fleet-penetration calculation.  Approximations from public
# production records; update as Cessna delivery numbers refresh.
# CJ4 (525C) is intentionally excluded — not in ATLAS scope.
FLEET_TOTALS_WORLD: dict[str, int] = {
    "CJ":      360,   # 525-0001..0359
    "CJ1":     240,   # 525-0360..0599
    "CJ1PLUS": 200,   # 525-0600..0799
    "M2":      280,   # 525-0800+ (still in production)
    "CJ2":     245,   # 525A-0001..~0193
    "CJ2PLUS": 290,   # 525A-~0194+ (still in production)
    "CJ3":     415,   # 525B-0001..~0500
    "CJ3PLUS": 290,   # 525B-~0501+ (still in production)
}

# Display order for thermometer rows (low → high baseline range)
FLEET_DISPLAY_ORDER: tuple[str, ...] = (
    "CJ", "CJ1", "CJ1PLUS", "M2", "CJ2", "CJ2PLUS", "CJ3", "CJ3PLUS",
)

# Human-readable labels for each sub-variant
FLEET_LABELS: dict[str, str] = {
    "CJ":      "CJ",
    "CJ1":     "CJ1",
    "CJ1PLUS": "CJ1+",
    "M2":      "M2",
    "CJ2":     "CJ2",
    "CJ2PLUS": "CJ2+",
    "CJ3":     "CJ3",
    "CJ3PLUS": "CJ3+",
}


def fleet_penetration() -> list[dict]:
    """
    Return per-sub-variant ATLAS fleet penetration stats.

    For each in-scope sub-variant (CJ4 excluded), counts how many serial numbers
    in FLEET_ACTIVE belong to that bucket and divides by the worldwide
    production total in FLEET_TOTALS_WORLD.

    Returns list of dicts in FLEET_DISPLAY_ORDER:
        key          sub-variant key  (e.g. 'CJ2PLUS')
        label        display name     (e.g. 'CJ2+')
        installed    count of currently-active ATLAS installs in this bucket
        removed      count of hulls that previously had ATLAS but had it removed
        total        worldwide production total (denominator)
        pct          installed / total, rounded to 1 decimal
        removed_pct  removed / total, rounded to 1 decimal
    """
    counts:   dict[str, int] = {k: 0 for k in FLEET_DISPLAY_ORDER}
    removed:  dict[str, int] = {k: 0 for k in FLEET_DISPLAY_ORDER}
    for sv in FLEET_ACTIVE_SUBVARIANT.values():
        if sv in counts:
            counts[sv] += 1
    for sv in FLEET_REMOVED_SUBVARIANT.values():
        if sv in removed:
            removed[sv] += 1

    out: list[dict] = []
    for key in FLEET_DISPLAY_ORDER:
        installed   = counts[key]
        removed_n   = removed[key]
        total       = FLEET_TOTALS_WORLD.get(key, 0)
        pct         = round(100.0 * installed / total, 1) if total else 0.0
        removed_pct = round(100.0 * removed_n / total, 1) if total else 0.0
        out.append({
            "key":         key,
            "label":       FLEET_LABELS.get(key, key),
            "installed":   installed,
            "removed":     removed_n,
            "total":       total,
            "pct":         pct,
            "removed_pct": removed_pct,
        })
    return out


def fleet_penetration_by_perfgroup() -> list[dict]:
    """
    Same as fleet_penetration(), but aggregated into the 5 canonical
    performance groups (CJ/CJ1, CJ1+/M2, CJ2, CJ2+, CJ3/CJ3+).

    Used by the main dashboard's ATLAS Fleet Penetration thermometers.
    Order follows atlas_config.PERF_GROUPS (low → high baseline).
    """
    from atlas_config import PERF_GROUPS

    per_sub = {r["key"]: r for r in fleet_penetration()}
    out: list[dict] = []
    for g in PERF_GROUPS:
        installed   = sum(per_sub.get(sv, {}).get("installed", 0) for sv in g["subvariants"])
        removed_n   = sum(per_sub.get(sv, {}).get("removed",   0) for sv in g["subvariants"])
        total       = sum(per_sub.get(sv, {}).get("total",     0) for sv in g["subvariants"])
        pct         = round(100.0 * installed / total, 1) if total else 0.0
        removed_pct = round(100.0 * removed_n / total, 1) if total else 0.0
        out.append({
            "key":         g["key"],
            "label":       g["label"],
            "installed":   installed,
            "removed":     removed_n,
            "total":       total,
            "pct":         pct,
            "removed_pct": removed_pct,
        })
    return out
