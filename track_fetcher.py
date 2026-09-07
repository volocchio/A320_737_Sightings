"""
track_fetcher.py — Fetch per-flight position track from FlightAware AeroAPI and
compute climb-performance metrics.

For each completed sighting we pull GET /flights/{fa_flight_id}/track and
derive:
    top_altitude_ft       — peak altitude reached (cruise altitude)
    time_to_10k_sec       — seconds from first position to 10,000 ft
    time_to_top_sec       — seconds to top of climb (level-off)
    avg_climb_rate_fpm    — mean fpm 0 → 10,000 ft (or to top if lower)
    peak_climb_rate_fpm   — best 60-second window
    climb_gradient_pct    — initial climb gradient over first 3 minutes

These metrics let us tell a prospect:
    "Your CJ took 14 min to FL350 at 1,200 fpm average. With ATLAS you'd
     have been there in 11 min at 1,650 fpm, or pushed up to FL410."

Throttle: FlightAware AeroAPI allows ~50 requests/min on the base tier.
We sleep 1.5s between calls to stay comfortably under that.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

import config

log = logging.getLogger(__name__)

BASE_URL = "https://aeroapi.flightaware.com/aeroapi"
THROTTLE_SECONDS = 1.5

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "x-apikey": config.FLIGHTAWARE_API_KEY,
        "Accept":   "application/json; charset=UTF-8",
    }
)


def fetch_flight_track(fa_flight_id: str) -> list[dict] | None:
    """
    Pull the position track for a completed FlightAware flight.
    Returns a list of position dicts (chronological) or None on failure.

    Each position dict normalized to:
        {ts: datetime, alt_ft: int|None, gs_kt: int|None, lat: float, lon: float}
    """
    if not config.FLIGHTAWARE_ACTIVE or not fa_flight_id:
        return None

    try:
        resp = _SESSION.get(
            f"{BASE_URL}/flights/{fa_flight_id}/track", timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("FlightAware track fetch failed for %s: %s", fa_flight_id, exc)
        return None

    positions = resp.json().get("positions") or []
    out: list[dict] = []
    for p in positions:
        try:
            ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        alt_raw = p.get("altitude")
        # FA reports altitude in HUNDREDS OF FEET (flight-level convention)
        alt_ft  = int(alt_raw) * 100 if isinstance(alt_raw, (int, float)) else None
        gs      = p.get("groundspeed")
        gs_kt   = int(gs) if isinstance(gs, (int, float)) else None
        lat     = p.get("latitude")
        lon     = p.get("longitude")
        if lat is None or lon is None:
            continue
        out.append({
            "ts":      ts,
            "alt_ft":  alt_ft,
            "gs_kt":   gs_kt,
            "lat":     float(lat),
            "lon":     float(lon),
        })

    out.sort(key=lambda x: x["ts"])
    return out


# ── Distance validation ────────────────────────────────────────────────────
# For flights whose origin→dest great-circle distance approaches or exceeds
# the CJ family's physical range, we can't trust the recorded ICAO pair
# blindly (bad codes or an unrecorded fuel stop can inflate distance_nm).
# Instead we fetch the actual position track and check:
#   1. The endpoint-to-endpoint great-circle matches the recorded distance
#      (within tolerance) — confirms the ICAO pair is right.
#   2. There's no on-ground gap in the middle — confirms it's one continuous
#      leg, not two legs stitched together.
# A flight passing both checks is trusted at face value.

# Gap heuristic: an unrecorded fuel stop leaves a hole in the track where
# either (a) consecutive positions are >20 min apart AND at low altitude,
# or (b) the aircraft descends to <3000 ft AGL somewhere in the middle of
# what should be a cruise leg.
_GAP_MAX_INTERVAL_SEC = 20 * 60
_GAP_MAX_ALT_FT       = 3000
_ENDPOINT_TOL_NM      = 50      # position vs. ICAO tolerance
_TRACK_VS_RECORD_TOL  = 0.15    # ±15% band on recorded distance_nm


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_km * 2 * math.asin(math.sqrt(a)) / 1.852


def validate_distance_from_track(
    fa_flight_id: str,
    recorded_nm: float | None,
    origin_lat_lon: tuple[float, float] | None = None,
    dest_lat_lon: tuple[float, float] | None = None,
) -> dict:
    """
    Fetch the FA track and audit the recorded distance.

    Returns dict:
      status                — 'validated', 'phantom', or 'no_track'
      track_distance_nm     — sum of great-circle segments along the actual
                              track (best estimate of true flown distance)
      endpoint_distance_nm  — great-circle from first to last track position
      reason                — short human-readable diagnosis

    'validated' means: track is continuous (no on-ground gap), and the
    recorded distance matches the track endpoints within tolerance.
    'phantom' means: there's a mid-track ground gap OR the recorded distance
    disagrees with the endpoints — in either case, trust track_distance_nm
    instead of the raw origin/dest ICAO computation.
    """
    track = fetch_flight_track(fa_flight_id)
    if not track or len(track) < 2:
        return {"status": "no_track", "track_distance_nm": None,
                "endpoint_distance_nm": None, "reason": "no track available"}

    # Sum along the track (actual flown distance)
    segs = 0.0
    for a, b in zip(track, track[1:]):
        segs += _haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"])
    track_nm = round(segs, 1)

    first, last = track[0], track[-1]
    endpoint_nm = round(_haversine_nm(first["lat"], first["lon"],
                                       last["lat"],  last["lon"]), 1)

    # Gap detection: any consecutive pair >20 min apart at low altitude,
    # OR a mid-track dip below 3000 ft after climb-out and before descent.
    ground_gap = False
    for a, b in zip(track, track[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt > _GAP_MAX_INTERVAL_SEC:
            alt_a = a.get("alt_ft") or 0
            alt_b = b.get("alt_ft") or 0
            if alt_a < _GAP_MAX_ALT_FT and alt_b < _GAP_MAX_ALT_FT:
                ground_gap = True
                break

    # Mid-cruise dip: if the aircraft climbed above 10k, then dropped below
    # 3k, then climbed above 10k again, that's a landing in between.
    if not ground_gap:
        phase = "climb"  # → "cruise" once above 10k, → "descent" when back below 10k after cruise
        cruised = False
        for p in track:
            alt = p.get("alt_ft") or 0
            if not cruised and alt > 10_000:
                cruised = True
            elif cruised and alt < _GAP_MAX_ALT_FT:
                # After cruise, dropped low — check whether we climb again
                phase = "descent"
                remaining = track[track.index(p) + 1:]
                if any((q.get("alt_ft") or 0) > 10_000 for q in remaining):
                    ground_gap = True
                break

    # Endpoint sanity vs. recorded ICAO positions (only if we have them)
    endpoint_mismatch = False
    if origin_lat_lon and dest_lat_lon:
        d_orig = _haversine_nm(first["lat"], first["lon"], *origin_lat_lon)
        d_dest = _haversine_nm(last["lat"],  last["lon"],  *dest_lat_lon)
        if d_orig > _ENDPOINT_TOL_NM or d_dest > _ENDPOINT_TOL_NM:
            endpoint_mismatch = True

    # Recorded vs. track disagreement
    record_mismatch = False
    if recorded_nm and recorded_nm > 0 and track_nm > 0:
        ratio = abs(track_nm - recorded_nm) / recorded_nm
        if ratio > _TRACK_VS_RECORD_TOL:
            record_mismatch = True

    if ground_gap:
        return {"status": "phantom", "track_distance_nm": track_nm,
                "endpoint_distance_nm": endpoint_nm,
                "reason": "on-ground gap detected mid-track"}
    if endpoint_mismatch:
        return {"status": "phantom", "track_distance_nm": track_nm,
                "endpoint_distance_nm": endpoint_nm,
                "reason": "track endpoints don't match recorded ICAOs"}
    if record_mismatch:
        return {"status": "phantom", "track_distance_nm": track_nm,
                "endpoint_distance_nm": endpoint_nm,
                "reason": f"track {track_nm} nm disagrees with recorded {recorded_nm} nm"}

    return {"status": "validated", "track_distance_nm": track_nm,
            "endpoint_distance_nm": endpoint_nm, "reason": "continuous track, endpoints match"}


# ── Sustained-plateau detection (shared by ICA and "top altitude") ──────────
# A flight is considered to be "at" an altitude only if it stays within
# ±_PLATEAU_ALT_TOL_FT for at least _MIN_PLATEAU_SECONDS. Brief level-offs
# (early step climbs, ATC step assignments, flap retraction shelves) are
# ignored. This is the operationally relevant definition for "cruise alt"
# and matches how Tamarack sales talks about climb-gradient performance.
_MIN_PLATEAU_SECONDS  = 900    # 15 minutes
_MAX_WINDOW_SECONDS   = 2400   # 40 minutes — scan cap
_PLATEAU_ALT_TOL_FT   = 500    # natural drift over 30 min


def _find_sustained_top(alt_track: list[dict]) -> int | None:
    """
    Return the highest altitude (rounded to nearest 100 ft) sustained for at
    least _MIN_PLATEAU_SECONDS within ±_PLATEAU_ALT_TOL_FT. Returns None if
    no such plateau exists (e.g. continuous climb-descend mission).
    """
    if not alt_track:
        return None
    best: int | None = None
    for i, anchor in enumerate(alt_track):
        window_alts = [anchor["alt_ft"]]
        window_end  = anchor
        for p in alt_track[i + 1:]:
            dt = (p["ts"] - anchor["ts"]).total_seconds()
            if dt > _MAX_WINDOW_SECONDS:
                break
            window_alts.append(p["alt_ft"])
            window_end = p
        span = (window_end["ts"] - anchor["ts"]).total_seconds()
        if span < _MIN_PLATEAU_SECONDS:
            continue
        if max(window_alts) - min(window_alts) > _PLATEAU_ALT_TOL_FT:
            continue
        plateau_alt = int(round(sum(window_alts) / len(window_alts) / 100.0)) * 100
        if best is None or plateau_alt > best:
            best = plateau_alt
    return best


def _detect_initial_cruise(
    alt_track: list[dict], top_alt: int,
) -> tuple[int | None, int | None]:
    """
    Find the FIRST SUSTAINED cruise altitude before any later step-climb to
        the final top altitude. "Sustained" = level-off held for at least 15
    minutes; brief level-offs (ATC step assignments, flap retraction shelves,
    short holds) are ignored.

    Conditions for a valid initial cruise plateau:
            • Altitude stays within ±500 ft for at least 15 minutes (900 s).
      • Plateau altitude is at or above 10,000 ft.
      • Final top altitude is at least 1,500 ft above the plateau (otherwise
        the plateau IS the cruise and no "initial cruise altitude" applies).

    Returns (initial_cruise_alt_ft, time_to_initial_cruise_sec) or (None, None).
    """
    if not alt_track or top_alt is None:
        return None, None
    elig = [p for p in alt_track if p["alt_ft"] >= 10_000]
    if len(elig) < 3:
        return None, None
    t0 = alt_track[0]["ts"]
    for i, anchor in enumerate(elig):
        window_alts = [anchor["alt_ft"]]
        window_end  = anchor
        for p in elig[i + 1:]:
            dt = (p["ts"] - anchor["ts"]).total_seconds()
            if dt > _MAX_WINDOW_SECONDS:
                break
            window_alts.append(p["alt_ft"])
            window_end = p
        span = (window_end["ts"] - anchor["ts"]).total_seconds()
        if span < _MIN_PLATEAU_SECONDS:
            continue
        if max(window_alts) - min(window_alts) > _PLATEAU_ALT_TOL_FT:
            continue
        plateau_alt = int(round(sum(window_alts) / len(window_alts) / 100.0)) * 100
        # Skip if plateau is essentially the top (i.e. no step climb happened)
        if top_alt - plateau_alt < 1500:
            return None, None
        # Use the FIRST time the aircraft reached this plateau altitude
        for p in alt_track:
            if p["alt_ft"] >= plateau_alt - 300:
                return plateau_alt, int((p["ts"] - t0).total_seconds())
        return plateau_alt, int((anchor["ts"] - t0).total_seconds())
    return None, None


def compute_climb_metrics(track: list[dict]) -> dict:
    """
    Derive climb-performance metrics from a chronological position track.
    Returns dict (all keys present, value None when not derivable).
    """
    result = {
        "top_altitude_ft":             None,
        "sustained_top_alt_ft":        None,
        "time_to_10k_sec":             None,
        "time_to_top_sec":             None,
        "avg_climb_rate_fpm":          None,
        "peak_climb_rate_fpm":         None,
        "climb_gradient_pct":          None,
        "initial_cruise_alt_ft":       None,
        "time_to_initial_cruise_sec":  None,
    }
    if not track:
        return result

    alt_track = [p for p in track if p["alt_ft"] is not None]
    if len(alt_track) < 2:
        return result

    t0       = alt_track[0]["ts"]
    alt_start = alt_track[0]["alt_ft"]
    # Raw top = highest altitude touched. Used for per-row display and a
    # fallback when no sustained plateau exists.
    top_alt   = max(p["alt_ft"] for p in alt_track)
    result["top_altitude_ft"] = top_alt
    # Sustained top = highest altitude held for ≥ 15 min within ±500 ft.
    # This is the operationally honest "cruise" altitude used by the climb
    # comparison chart. None when no real plateau exists (e.g. short hop).
    result["sustained_top_alt_ft"] = _find_sustained_top(alt_track)

    # Time to first position at or above 10,000 ft
    for p in alt_track:
        if p["alt_ft"] >= 10_000:
            result["time_to_10k_sec"] = int((p["ts"] - t0).total_seconds())
            break

    # Time to first position at top altitude (top of climb)
    for p in alt_track:
        if p["alt_ft"] >= top_alt - 200:   # within 200 ft of peak
            result["time_to_top_sec"] = int((p["ts"] - t0).total_seconds())
            break

    # Average climb rate from start to 10k (or to top if lower)
    climb_end_alt = min(10_000, top_alt)
    end_point = next(
        (p for p in alt_track if p["alt_ft"] >= climb_end_alt - 200), None
    )
    if end_point and end_point["ts"] > t0:
        dt_min   = (end_point["ts"] - t0).total_seconds() / 60.0
        delta_ft = end_point["alt_ft"] - alt_start
        if dt_min > 0:
            result["avg_climb_rate_fpm"] = int(round(delta_ft / dt_min))

    # Peak 60-second window climb rate (sliding window over consecutive points)
    peak_fpm = 0
    for i in range(len(alt_track) - 1):
        anchor = alt_track[i]
        for j in range(i + 1, len(alt_track)):
            dt_sec = (alt_track[j]["ts"] - anchor["ts"]).total_seconds()
            if dt_sec < 60:
                continue
            if dt_sec > 120:
                break
            d_alt = alt_track[j]["alt_ft"] - anchor["alt_ft"]
            fpm   = d_alt * 60 / dt_sec
            if fpm > peak_fpm:
                peak_fpm = fpm
            break
    if peak_fpm > 0:
        result["peak_climb_rate_fpm"] = int(round(peak_fpm))

    # Initial climb gradient (first 3 minutes, ft of climb / nm flown × 100)
    cutoff_idx = None
    for i, p in enumerate(alt_track):
        if (p["ts"] - t0).total_seconds() >= 180:
            cutoff_idx = i
            break
    if cutoff_idx and cutoff_idx > 0:
        gs_avg = sum(
            p["gs_kt"] for p in alt_track[:cutoff_idx] if p["gs_kt"]
        ) / max(1, sum(1 for p in alt_track[:cutoff_idx] if p["gs_kt"]))
        if gs_avg > 0:
            dt_hr      = (alt_track[cutoff_idx]["ts"] - t0).total_seconds() / 3600
            d_alt_ft   = alt_track[cutoff_idx]["alt_ft"] - alt_start
            dist_nm    = gs_avg * dt_hr
            if dist_nm > 0:
                result["climb_gradient_pct"] = round(
                    (d_alt_ft / 6076.12) / dist_nm * 100, 1
                )

    # Step-climb detection (initial level-off altitude + time to reach it)
    init_alt, init_t = _detect_initial_cruise(alt_track, top_alt)
    result["initial_cruise_alt_ft"]      = init_alt
    result["time_to_initial_cruise_sec"] = init_t

    return result


def backfill_one_sighting(conn, row: dict) -> bool:
    """
    Fetch + compute + update climb metrics for one sighting row.
    Returns True if metrics were written (even if some fields are None).

    Resolution order for the FA canonical flight id:
      1. row['fa_flight_id']            — set at ingest / by prior enrichment
      2. row['flight_id']  (FA source)  — legacy path; FA rows never populated
                                           fa_flight_id before that column existed
      3. On-the-fly `enrich_sighting(tail, arrived_utc)` for adsb.lol / opensky /
         adsbexchange rows — persist the result so we only pay the FA call once.
    """
    source = (row.get("source") or "").lower()
    fa_id  = row.get("fa_flight_id") or ""
    if not fa_id and source == "flightaware":
        fa_id = row.get("flight_id") or ""

    if not fa_id and source in ("adsblol", "opensky", "adsbexchange"):
        tail    = row.get("tail_number") or ""
        arrived = row.get("arrived_utc") or ""
        if tail and arrived and config.FLIGHTAWARE_ACTIVE:
            try:
                from sources import flightaware as _fa
                enr = _fa.enrich_sighting(tail, arrived)
            except Exception:  # noqa: BLE001
                enr = None
            if enr and enr.get("fa_flight_id"):
                fa_id = enr["fa_flight_id"]
                conn.execute(
                    "UPDATE sightings SET fa_flight_id=? WHERE id=?",
                    (fa_id, row["id"]),
                )

    if not fa_id:
        # Nothing to fetch — mark attempted so backfill_pending doesn't spin.
        conn.execute(
            "UPDATE sightings SET track_fetched_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), row["id"]),
        )
        return False

    track = fetch_flight_track(fa_id)
    metrics = compute_climb_metrics(track or [])
    conn.execute(
        "UPDATE sightings SET track_fetched_at=?, top_altitude_ft=?, "
        "sustained_top_alt_ft=?, "
        "time_to_10k_sec=?, time_to_top_sec=?, avg_climb_rate_fpm=?, "
        "peak_climb_rate_fpm=?, climb_gradient_pct=?, "
        "initial_cruise_alt_ft=?, time_to_initial_cruise_sec=? WHERE id=?",
        (
            datetime.utcnow().isoformat(),
            metrics["top_altitude_ft"],
            metrics["sustained_top_alt_ft"],
            metrics["time_to_10k_sec"],
            metrics["time_to_top_sec"],
            metrics["avg_climb_rate_fpm"],
            metrics["peak_climb_rate_fpm"],
            metrics["climb_gradient_pct"],
            metrics["initial_cruise_alt_ft"],
            metrics["time_to_initial_cruise_sec"],
            row["id"],
        ),
    )
    return True


def backfill_pending(limit: int = 50, force: bool = False) -> dict:
    """
    Process up to `limit` sightings that need climb metrics.

    Default mode: only rows with no climb metrics yet (track_fetched_at IS NULL).
    Force mode:   rows that have a top_altitude_ft but no initial_cruise_alt_ft
                  yet. Use this once after adding step-climb columns to
                  back-populate historical FA sightings.

    Throttled at THROTTLE_SECONDS between FA API calls.
    Returns summary {processed, with_metrics, fa_skipped, force}.
    """
    import database
    processed     = 0
    with_metrics  = 0
    fa_skipped    = 0

    with database._connect() as conn:
        if force:
            # "Needs processing" = row has a fetched track (top_altitude_ft set)
            # but the new sustained_top_alt_ft column hasn't been populated yet.
            # This marker gets set on every successful re-fetch, so chained
            # batches walk strictly forward through the dataset and never
            # re-process the same row.
            rows = conn.execute(
                "SELECT id, flight_id, fa_flight_id, source, tail_number, "
                "       arrived_utc, top_altitude_ft "
                "FROM sightings "
                "WHERE source='flightaware' "
                "  AND top_altitude_ft IS NOT NULL "
                "  AND sustained_top_alt_ft IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, flight_id, fa_flight_id, source, tail_number, "
                "       arrived_utc, top_altitude_ft "
                "FROM sightings "
                "WHERE track_fetched_at IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        for row in rows:
            row_d = dict(row)
            source = (row_d.get("source") or "").lower()
            # A row hits the FA API when its source is FA OR when we've already
            # resolved a fa_flight_id for it OR when we'll try on-the-fly
            # enrichment (adsblol / opensky / adsbexchange).
            will_hit_fa = (
                source == "flightaware"
                or bool(row_d.get("fa_flight_id"))
                or source in ("adsblol", "opensky", "adsbexchange")
            )
            wrote = backfill_one_sighting(conn, row_d)
            processed += 1
            if wrote:
                refetched = conn.execute(
                    "SELECT top_altitude_ft FROM sightings WHERE id=?",
                    (row_d["id"],),
                ).fetchone()
                if refetched and refetched["top_altitude_ft"]:
                    with_metrics += 1
            else:
                fa_skipped += 1
            conn.commit()
            if will_hit_fa:
                time.sleep(THROTTLE_SECONDS)

    return {
        "processed":    processed,
        "with_metrics": with_metrics,
        "fa_skipped":   fa_skipped,
        "force":        force,
    }
