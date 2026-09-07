"""
chat.py — AI chat backend for the A320/737 Sightings dashboard.

Uses OpenAI gpt-4o-mini with function/tool calling to let users ask analytical
questions about A320/737-family flight activity and trigger Teams notifications, watch
list edits, and history backfills from natural language.

Set OPENAI_API_KEY in the .env to enable. See config.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "sightings.db"

# Lazy OpenAI client (None until first use; allows the module to import cleanly
# when the API key isn't set).
_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.OPENAI_ENABLED:
            raise RuntimeError("OPENAI_API_KEY is not set")
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _completion_with_retry(client, *, attempts: int = 3, **kwargs):
    """
    Call chat.completions.create with a short exponential backoff on transient
    OpenAI errors (429 / 5xx / connection). Re-raises the last error if all
    attempts fail so the endpoint can surface a clean 500.
    """
    import time as _time
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:                            # noqa: BLE001
            status = getattr(e, "status_code", None) or getattr(e, "http_status", None)
            transient = status in (429, 500, 502, 503, 504) or status is None
            last_exc = e
            if not transient or i == attempts - 1:
                raise
            _time.sleep(0.8 * (2 ** i))   # 0.8s, 1.6s
    raise last_exc  # pragma: no cover


_TOOL_RESULT_LIMIT = 12000


def _truncate_result(result_str: str) -> str:
    """
    Cap an oversized tool-result JSON string before feeding it back to the LLM,
    with an explicit flag so the model knows data was cut and can tell the user
    to narrow the query instead of silently reasoning on partial data.
    """
    if len(result_str) <= _TOOL_RESULT_LIMIT:
        return result_str
    return (
        result_str[:_TOOL_RESULT_LIMIT]
        + "\n\n[TRUNCATED: this tool result exceeded the size limit and was cut. "
          "The data above is PARTIAL. Tell the user the result was truncated and "
          "suggest narrowing the query — a shorter time window, a lower limit, or "
          "more specific filters — for complete results.]"
    )




# ────────────────────────────────────────────────────────────────────────────
# Tool implementations — small Python functions the LLM can call
# ────────────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ago(window_days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    return cutoff.isoformat()


def _icao_list(v) -> list[str]:
    """Normalize an ICAO input into a de-duped upper-case list.

    Accepts:
      - None → []
      - str  → single ICAO, or a comma / whitespace / semicolon separated list
      - list/tuple/set → iterable of ICAOs
    """
    if v is None:
        return []
    if isinstance(v, str):
        raw = [t for t in v.replace(";", ",").replace(" ", ",").split(",") if t]
    else:
        raw = list(v)
    out, seen = [], set()
    for t in raw:
        s = str(t).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _enrich_local_times(rows: list[dict]) -> list[dict]:
    """
    Mutate (and return) a list of sighting dicts in-place by adding:
        arrived_local   — e.g. "5:07 PM PDT" using the dest airport's TZ
        departed_local  — e.g. "3:55 PM PDT" using the origin airport's TZ
    Empty string if the airport's TZ can't be resolved or the UTC field is blank.
    """
    import airports as _ap
    for r in rows:
        au = r.get("arrived_utc")
        du = r.get("departed_utc")
        dest_icao   = r.get("dest_icao")   or ""
        origin_icao = r.get("origin_icao") or ""
        r["arrived_local"]  = _ap.local_time_at_icao(au or "", dest_icao)   if au else ""
        r["departed_local"] = _ap.local_time_at_icao(du or "", origin_icao) if du else ""
    return rows


def tool_query_sightings(
    subvariant:  str | None = None,
    ac_type:     str | None = None,
    tail:        str | None = None,
    origin:      str | list[str] | None = None,
    dest:        str | list[str] | None = None,
    operator:    str | None = None,
    region:      str | None = None,
    min_distance_nm: int | None = None,
    max_distance_nm: int | None = None,
    days_back:   int = 30,
    limit:       int = 25,
) -> dict:
    """Filtered query against the sightings table.

    `origin` and `dest` accept either a single ICAO string or a list of ICAOs
    (or a comma-separated string) so metro clusters can be queried in one call.
    `region` narrows to a sales-region bucket: 'NA' | 'EU_UK' | 'OTHER'.
    """
    where, params = ["arrived_utc >= ?"], [_ago(days_back)]
    if subvariant:
        where.append("UPPER(ac_subvariant) = ?")
        params.append(subvariant.upper())
    if ac_type:
        where.append("UPPER(ac_type) = ?")
        params.append(ac_type.upper())
    if tail:
        where.append("UPPER(tail_number) = ?")
        params.append(tail.upper())
    origins = _icao_list(origin)
    if origins:
        where.append(f"UPPER(origin_icao) IN ({','.join(['?']*len(origins))})")
        params.extend(origins)
    dests = _icao_list(dest)
    if dests:
        where.append(f"UPPER(dest_icao) IN ({','.join(['?']*len(dests))})")
        params.extend(dests)
    if operator:
        where.append("operator LIKE ?")
        params.append(f"%{operator}%")
    if region and region.upper() in ("NA", "EU_UK", "OTHER"):
        where.append("region = ?")
        params.append(region.upper())
    if min_distance_nm is not None:
        where.append("distance_nm >= ?")
        params.append(min_distance_nm)
    if max_distance_nm is not None:
        where.append("distance_nm <= ?")
        params.append(max_distance_nm)

    sql = (
        f"SELECT tail_number, ac_type, ac_subvariant, origin_icao, dest_icao, "
        f"       departed_utc, arrived_utc, operator, distance_nm, "
        f"       top_altitude_ft, time_to_10k_sec, avg_climb_rate_fpm, "
        f"       is_tamarack_fleet "
        f"FROM v_sightings_dedup WHERE {' AND '.join(where)} "
        f"ORDER BY id DESC LIMIT {max(1, min(limit, 200))}"
    )
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    _enrich_local_times(rows)
    return {"count": len(rows), "rows": rows}


def tool_top_operators(
    window_days: int = 30,
    by:          str = "trips",
    subvariant:  str | None = None,
    limit:       int = 10,
) -> dict:
    """Rank operators by activity. by in {'trips', 'distance', 'long_missions'}."""
    where, params = ["arrived_utc >= ?", "operator IS NOT NULL", "operator != ''"], [_ago(window_days)]
    if subvariant:
        where.append("UPPER(ac_subvariant) = ?")
        params.append(subvariant.upper())

    if by == "distance":
        order = "SUM(COALESCE(distance_nm,0)) DESC"
        metric_sql = "SUM(COALESCE(distance_nm,0)) AS metric"
    elif by == "long_missions":
        order = "SUM(CASE WHEN distance_nm > 1300 THEN 1 ELSE 0 END) DESC"
        metric_sql = "SUM(CASE WHEN distance_nm > 1300 THEN 1 ELSE 0 END) AS metric"
    else:   # trips
        order = "COUNT(*) DESC"
        metric_sql = "COUNT(*) AS metric"

    sql = (
        f"SELECT operator, COUNT(DISTINCT tail_number) AS tails, "
        f"       COUNT(*) AS trips, "
        f"       ROUND(AVG(COALESCE(distance_nm,0))) AS avg_nm, "
        f"       MAX(COALESCE(distance_nm,0)) AS max_nm, "
        f"       {metric_sql} "
        f"FROM v_sightings_dedup WHERE {' AND '.join(where)} "
        f"GROUP BY operator ORDER BY {order} LIMIT {limit}"
    )
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return {"window_days": window_days, "by": by, "rows": rows}


def tool_tail_summary(tail: str) -> dict:
    """All-time stats for one N-number."""
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM v_sightings_dedup WHERE UPPER(tail_number)=? ORDER BY id DESC",
            (tail.upper(),),
        ).fetchall()]
    if not rows:
        return {"tail": tail.upper(), "found": False}

    _enrich_local_times(rows)

    distances = [r["distance_nm"] for r in rows if r.get("distance_nm")]
    climbs    = [r["avg_climb_rate_fpm"] for r in rows if r.get("avg_climb_rate_fpm")]
    tops      = [r["top_altitude_ft"]   for r in rows if r.get("top_altitude_ft")]
    routes = {}
    for r in rows:
        k = f'{r.get("origin_icao") or "?"}\u2192{r.get("dest_icao") or "?"}'
        routes[k] = routes.get(k, 0) + 1

    return {
        "tail":           tail.upper(),
        "found":          True,
        "trips":          len(rows),
        "subvariant":     rows[0].get("ac_subvariant"),
        "ac_type":        rows[0].get("ac_type"),
        "serial":         rows[0].get("serial_number"),
        "operator":       rows[0].get("operator"),
        "tamarack_fleet": rows[0].get("is_tamarack_fleet"),
        "avg_mission_nm": int(sum(distances)/len(distances)) if distances else None,
        "max_mission_nm": max(distances) if distances else None,
        "avg_climb_fpm":  int(sum(climbs)/len(climbs))       if climbs else None,
        "avg_top_alt_ft": int(sum(tops)/len(tops))           if tops else None,
        "top_routes":     sorted(routes.items(), key=lambda x: -x[1])[:5],
        "last_seen":      rows[0].get("arrived_utc"),
        "last_seen_local": rows[0].get("arrived_local") or "",
    }


def tool_route_pair_stats(origin, dest, days_back: int = 180) -> dict:
    """All sightings between two airports OR two clusters of airports (either direction).

    `origin` and `dest` may each be a single ICAO string, a list of ICAOs, or a
    comma-separated string. This lets you query e.g. NYC-metro (KTEB, KHPN, KJFK,
    KLGA, KEWR, KFRG, KMMU) → S-FL-metro (KMIA, KOPF, KFXE, KFLL, KBCT, KPBI)
    in one call.
    """
    origins = _icao_list(origin)
    dests   = _icao_list(dest)
    if not origins or not dests:
        return {
            "origin": origins, "dest": dests, "days_back": days_back,
            "trips": 0, "error": "origin and dest must each contain at least one ICAO",
        }
    op_ph = ','.join(['?']*len(origins))
    de_ph = ','.join(['?']*len(dests))
    sql = (
        "SELECT tail_number, ac_subvariant, operator, distance_nm, "
        "       top_altitude_ft, avg_climb_rate_fpm, "
        "       origin_icao, dest_icao, departed_utc, arrived_utc "
        "FROM v_sightings_dedup WHERE arrived_utc >= ? AND "
        f"((UPPER(origin_icao) IN ({op_ph}) AND UPPER(dest_icao) IN ({de_ph})) OR "
        f" (UPPER(origin_icao) IN ({de_ph}) AND UPPER(dest_icao) IN ({op_ph}))) "
        "ORDER BY id DESC LIMIT 100"
    )
    params = [_ago(days_back), *origins, *dests, *dests, *origins]
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    _enrich_local_times(rows)
    operators = sorted({r["operator"] for r in rows if r.get("operator")})
    tails     = sorted({r["tail_number"] for r in rows if r.get("tail_number")})
    # Per-pair breakdown so the LLM can call out which airports actually saw traffic.
    by_pair: dict[str, int] = {}
    for r in rows:
        k = f'{r.get("origin_icao") or "?"}\u2192{r.get("dest_icao") or "?"}'
        by_pair[k] = by_pair.get(k, 0) + 1
    return {
        "origin": origins, "dest": dests, "days_back": days_back,
        "trips":    len(rows),
        "unique_tails":     len(tails),
        "unique_operators": len(operators),
        "operators":        operators[:20],
        "tails":            tails[:30],
        "pair_breakdown":   sorted(by_pair.items(), key=lambda x: -x[1]),
        "recent_rows":      rows[:15],
    }


def tool_airport_traffic(icao: str, days_back: int = 30) -> dict:
    """All CJ activity at one airport (as origin or destination)."""
    code = icao.upper()
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT tail_number, ac_subvariant, operator, origin_icao, dest_icao, "
            "       distance_nm, departed_utc, arrived_utc FROM v_sightings_dedup "
            "WHERE arrived_utc >= ? AND (UPPER(origin_icao)=? OR UPPER(dest_icao)=?) "
            "ORDER BY id DESC LIMIT 100",
            (_ago(days_back), code, code),
        ).fetchall()]
    _enrich_local_times(rows)
    operators = {}
    for r in rows:
        op = r.get("operator") or ""
        if op:
            operators[op] = operators.get(op, 0) + 1
    return {
        "icao":   code,
        "days_back": days_back,
        "trips":  len(rows),
        "unique_tails":    len({r["tail_number"] for r in rows if r.get("tail_number")}),
        "top_operators":   sorted(operators.items(), key=lambda x: -x[1])[:10],
        "recent_rows":     rows[:20],
    }


def tool_climb_stats(subvariant: str, days_back: int = 90) -> dict:
    """Mean/median/p90 climb rate + altitude reached, per sub-model."""
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT avg_climb_rate_fpm, peak_climb_rate_fpm, top_altitude_ft, "
            "       time_to_10k_sec, distance_nm "
            "FROM v_sightings_dedup WHERE arrived_utc >= ? AND UPPER(ac_subvariant)=? "
            "AND avg_climb_rate_fpm IS NOT NULL",
            (_ago(days_back), subvariant.upper()),
        ).fetchall()]
    if not rows:
        return {"subvariant": subvariant.upper(), "n": 0}
    def _p(vals, p):
        sv = sorted(vals); k = int(round((len(sv)-1)*p/100))
        return sv[k] if sv else None
    avgs = [r["avg_climb_rate_fpm"] for r in rows if r["avg_climb_rate_fpm"]]
    tops = [r["top_altitude_ft"]    for r in rows if r["top_altitude_ft"]]
    t10  = [r["time_to_10k_sec"]    for r in rows if r["time_to_10k_sec"]]
    return {
        "subvariant":            subvariant.upper(),
        "n":                     len(rows),
        "avg_climb_mean_fpm":    int(sum(avgs)/len(avgs)),
        "avg_climb_median_fpm":  _p(avgs, 50),
        "avg_climb_p90_fpm":     _p(avgs, 90),
        "top_alt_median_ft":     _p(tops, 50),
        "top_alt_p90_ft":        _p(tops, 90),
        "time_to_10k_median_sec":_p(t10, 50),
    }


def tool_wat_analysis(elevation_ft: float, oat_c: float,
                      actual_weight_lb: int | None = None) -> dict:
    """
    Full WAT (Weight-Altitude-Temperature) dispatch analysis using the
    canonical tamarack-wat-tables package. Compares Flatwing vs Tamarack
    max takeoff weight + temperature envelope at the given field conditions.
    """
    from wat_lookup import wat_analysis
    return wat_analysis(elevation_ft, oat_c, actual_weight_lb)


def tool_wat_max_weight(elevation_ft: float, oat_c: float,
                        mod: str = "flatwing") -> dict:
    """
    Single max-takeoff-weight lookup (lb). `mod` is 'flatwing' or 'tamarack'.
    Flaps Up configuration. Backed by tamarack-wat-tables canonical tables.
    """
    from wat_lookup import wat_max_weight, MTOW
    w = wat_max_weight(elevation_ft, oat_c, mod)
    return {
        "mod":         mod,
        "elevation_ft": elevation_ft,
        "oat_c":       oat_c,
        "max_weight_lb": w,
        "mtow_lb":     MTOW,
        "limited":     w < MTOW,
    }


def tool_add_to_watch(airports: list[str] | None = None,
                      tails:    list[str] | None = None) -> dict:
    """Add airports and/or tails to the Teams watch list."""
    import watch_airports as _wa
    import watch_tails    as _wt
    out = {}
    if airports:
        current = _wa.get_watch_list()
        for a in airports:
            s = (a or "").strip().upper()
            if s and s not in current:
                current.append(s)
        out["airports"] = _wa.set_watch_list(current)
    if tails:
        out["tails"] = _wt.add_tails(tails)
    if not out:
        out["note"] = "no airports or tails supplied"
    return out


def tool_get_watch_lists() -> dict:
    """Return current watch list state."""
    import watch_airports as _wa
    import watch_tails    as _wt
    return {
        "airports": _wa.get_watch_list(),
        "tails":    _wt.get_watch_list(),
        "teams_enabled": config.TEAMS_ENABLED,
    }


def tool_send_teams_card(title: str, summary: str,
                         facts: list[list[str]] | None = None) -> dict:
    """
    Post a custom Adaptive Card to the configured Teams webhook.
    `facts` is list of [name, value] pairs.
    """
    import teams_notifier as _tn
    if not config.TEAMS_ENABLED:
        return {"sent": False, "error": "TEAMS_WEBHOOK_URL not configured"}
    fact_pairs = [(str(k), str(v)) for k, v in (facts or [])]
    payload = _tn._adaptive_card(
        title       = title,
        subtitle    = summary,
        facts       = fact_pairs,
        action_url  = config.DASHBOARD_URL,
        action_label= "Open dashboard",
    )
    ok = _tn.send(payload)
    return {"sent": ok}


def tool_start_history_backfill(days: int = 180) -> dict:
    """Kick off the historical FlightAware backfill in the background."""
    import backfill_history
    started = backfill_history.start(days)
    return {"started": started, "days": days, "state": backfill_history.state}


def tool_m2_yaw_damper_suspects(days: int = 90,
                                min_distance_nm: int = 300,
                                max_alt_ft: int = 28000,
                                min_low_flights: int = 3) -> dict:
    """
    M2 (C25M) tails that consistently fly at or below FL280 on missions
    longer than min_distance_nm. Per the M2 AFM, an inoperative yaw damper
    imposes a max-altitude limit of FL280 — so habitual low-altitude
    long legs flag a likely deferred yaw-damper squawk (sales opener).
    """
    import database as _db
    rows = _db.get_m2_yaw_damper_suspects(
        days=days,
        min_distance_nm=min_distance_nm,
        max_alt_ft=max_alt_ft,
        min_low_flights=min_low_flights,
    )
    return {"window_days": days, "count": len(rows), "tails": rows}


def tool_top_prospects(days: int = 30, region: str | None = None,
                       limit: int = 10) -> dict:
    """
    Top ATLAS sales prospects, scored by composite range + fuel-stop-chain +
    high-DA-airport signal. Optionally filter by sales region
    ('NA' | 'EU_UK' | 'OTHER'). Returns the ranked tails with their signals,
    operator, and best long trip so the LLM can suggest who to call.
    """
    import database as _db
    reg = region.upper() if region and region.upper() in ("NA", "EU_UK", "OTHER") else None
    rows = _db.get_prospects(days=days, region=reg)
    top = rows[: max(1, min(limit, 25))]
    slim = [{
        "rank":            i + 1,
        "tail_number":     p["tail_number"],
        "label":           p["label"],
        "operator":        p["operator"],
        "composite_score": p["composite_score"],
        "trips_hot":       p["trips_hot"],
        "trips_warm":      p["trips_warm"],
        "n_chains":        p["n_chains"],
        "high_da_count":   p["high_da_count"],
        "signals":         p["signals"],
        "max_distance_nm": p["max_distance_nm"],
        "best_origin":     p["best_origin"],
        "best_dest":       p["best_dest"],
        "last_seen":       p["last_seen"],
        "detail_url":      f"{config.DASHBOARD_URL}/tail/{p['tail_number']}",
    } for i, p in enumerate(top)]
    return {"window_days": days, "region": reg or "all",
            "count": len(slim), "prospects": slim}


def tool_fuel_stop_chains(days: int = 90, tail: str | None = None,
                          region: str | None = None, limit: int = 20) -> dict:
    """
    Fuel-stop chains — consecutive same-tail legs joined by a short ground
    stop where the combined distance is a range-pressure signal ATLAS would
    have flown non-stop. Optionally scope to one `tail` or a `region`.
    Each chain includes the ATLAS classification (range_win / operational /
    beyond) so the LLM can tell a true range-eliminated fuel stop from an
    operational one.
    """
    import database as _db
    reg = region.upper() if region and region.upper() in ("NA", "EU_UK", "OTHER") else None
    chains = _db.get_fuel_stop_chains(days=days, tail_number=(tail.upper() if tail else None),
                                      region=reg)
    return {"window_days": days, "region": reg or "all",
            "tail": tail.upper() if tail else None,
            "count": len(chains), "chains": chains[: max(1, min(limit, 50))]}


def tool_mustang_activity(days: int = 30, limit: int = 25) -> dict:
    """
    Adjacent-tier Citation Mustang (C510) activity — up-purchase signal for
    operators likely to move up into a CJ. Returns recent Mustang landings +
    chain hits (an operator making fuel-stop chains on a Mustang is outgrowing
    the airplane — a warm CJ / ATLAS conversation).
    """
    import database as _db
    data = _db.get_mustang_activity(days=days, limit=max(1, min(limit, 200)))
    flights = data.get("flights", [])[: max(1, min(limit, 50))]
    return {
        "window_days":    days,
        "total_flights":  data.get("total_flights", 0),
        "unique_tails":   data.get("unique_tails"),
        "chain_hits":     data.get("chain_hits"),
        "flights":        flights,
    }


def tool_jetnet_owner(nnumber: str, refresh: bool = False) -> dict:
    """
    Registered owner + operator + contact for a tail from JETNET. Reads the
    local cache first; when `refresh` is true (or the tail has never been
    fetched), makes a live JETNET call. Returns a clear status when JETNET
    isn't entitled yet so the assistant can say so instead of inventing data.
    """
    if not config.JETNET_ACTIVE:
        return {"nnumber": nnumber.upper(), "available": False,
                "reason": "JETNET not configured / entitlement pending"}
    import jetnet_enrichment as _je
    n = nnumber.strip().upper()
    row = _je.get_owner_cached(n)
    if refresh or not (row and row.get("fetched_at")):
        row = _je.fetch_and_store_owner(n) or row
    if not row or not (row.get("owner") or row.get("operator")):
        return {"nnumber": n, "available": False,
                "reason": "no JETNET record found for this tail"}
    return {
        "nnumber":   n,
        "available": True,
        "owner":     row.get("owner"),
        "operator":  row.get("operator"),
        "contact":   _je.contact_line(row) or None,
        "phone":     row.get("phone"),
        "email":     row.get("email"),
        "city":      row.get("city"),
        "state":     row.get("state"),
        "country":   row.get("country"),
        "fetched_at": (row.get("fetched_at") or "")[:10],
    }


def tool_jetnet_history(nnumber: str, days: int | None = None) -> dict:
    """
    Ownership / transaction history for a tail from JETNET (sales, leases,
    fractional moves). `days` limits how far back to look. Returns a clear
    status when JETNET isn't entitled yet.
    """
    if not config.JETNET_ACTIVE:
        return {"nnumber": nnumber.upper(), "available": False,
                "reason": "JETNET not configured / entitlement pending"}
    from sources import jetnet as _jn
    n = nnumber.strip().upper()
    history = _jn.lookup_history(n, days=days)
    if history is None:
        return {"nnumber": n, "available": False,
                "reason": "no JETNET history found for this tail"}
    return {"nnumber": n, "available": True,
            "count": len(history), "history": history[:50]}



# ────────────────────────────────────────────────────────────────────────────
# Tool registry — what the LLM sees
# ────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "query_sightings",
        "description": "Filtered query against the sightings table. Use this for arbitrary lookups by sub-model, tail, route, operator, distance bucket, etc. `origin`/`dest` accept a single ICAO or an array of ICAOs (metro cluster).",
        "parameters": {
            "type": "object",
            "properties": {
                "subvariant":      {"type": "string", "enum": ["CJ","CJ1","CJ1PLUS","M2","CJ2","CJ2PLUS","CJ3","CJ3PLUS"]},
                "ac_type":         {"type": "string", "enum": ["C525","C25A","C25B","C25M"]},
                "tail":            {"type": "string", "description": "N-number"},
                "origin":          {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "ICAO code, or array of ICAO codes (metro cluster)"},
                "dest":            {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "ICAO code, or array of ICAO codes (metro cluster)"},
                "operator":        {"type": "string", "description": "substring match"},
                "region":          {"type": "string", "enum": ["NA","EU_UK","OTHER"], "description": "Sales-region bucket: NA (US/CA/MX), EU_UK (EU-27 + UK + EFTA), OTHER"},
                "min_distance_nm": {"type": "integer"},
                "max_distance_nm": {"type": "integer"},
                "days_back":       {"type": "integer", "default": 30},
                "limit":           {"type": "integer", "default": 25, "maximum": 200},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "top_operators",
        "description": "Rank operators by trip count, total distance, or count of long missions (>1300 nm).",
        "parameters": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "default": 30},
                "by":          {"type": "string", "enum": ["trips","distance","long_missions"], "default": "trips"},
                "subvariant":  {"type": "string", "enum": ["CJ","CJ1","CJ1PLUS","M2","CJ2","CJ2PLUS","CJ3","CJ3PLUS"]},
                "limit":       {"type": "integer", "default": 10},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "tail_summary",
        "description": "Deep stats for one N-number: trips, sub-model, average mission length, climb performance, top routes, ATLAS-fleet status.",
        "parameters": {"type": "object", "required": ["tail"],
                       "properties": {"tail": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "route_pair_stats",
        "description": "All sightings between two ICAO airports OR two clusters of airports (either direction). Pass arrays to compare metros (e.g. NYC-area → S-FL-area) in one call — returns a per-pair breakdown too.",
        "parameters": {"type": "object", "required": ["origin","dest"],
                       "properties": {
                           "origin": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "ICAO code, or array of ICAO codes"},
                           "dest":   {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "ICAO code, or array of ICAO codes"},
                           "days_back": {"type": "integer", "default": 180}}},
    }},
    {"type": "function", "function": {
        "name": "airport_traffic",
        "description": "All A320/737-family activity at one airport (as origin OR destination) within a recent window.",
        "parameters": {"type": "object", "required": ["icao"],
                       "properties": {"icao": {"type": "string"},
                                      "days_back": {"type": "integer", "default": 30}}},
    }},
    {"type": "function", "function": {
        "name": "climb_stats",
        "description": "Aggregate climb-rate / cruise-altitude / time-to-10k statistics for one sub-model.",
        "parameters": {"type": "object", "required": ["subvariant"],
                       "properties": {
                           "subvariant": {"type": "string", "enum": ["CJ","CJ1","CJ1PLUS","M2","CJ2","CJ2PLUS","CJ3","CJ3PLUS"]},
                           "days_back": {"type": "integer", "default": 90}}},
    }},
    {"type": "function", "function": {
        "name": "wat_analysis",
        "description": "Full WAT (Weight-Altitude-Temperature) dispatch analysis for a Cessna 525 family aircraft. Use this for ANY question about max takeoff weight, hot-day performance, payload restriction, or ATLAS payload/temperature benefit at a specific airport+OAT. Returns Flatwing vs Tamarack max weight, temperature ceilings, ATLAS gain in lb and degrees C, plus a sales narrative.",
        "parameters": {"type": "object", "required": ["elevation_ft","oat_c"],
                       "properties": {
                           "elevation_ft":    {"type": "number", "description": "Pressure altitude or field elevation in feet"},
                           "oat_c":           {"type": "number", "description": "Outside air temperature in °C"},
                           "actual_weight_lb":{"type": "integer", "description": "Optional reference dispatch weight in lb (defaults to MTOW 10400)"}}},
    }},
    {"type": "function", "function": {
        "name": "wat_max_weight",
        "description": "Look up max takeoff weight (lb) at a single field elevation + OAT for either Flatwing or Tamarack configuration (Flaps Up).",
        "parameters": {"type": "object", "required": ["elevation_ft","oat_c"],
                       "properties": {
                           "elevation_ft": {"type": "number"},
                           "oat_c":        {"type": "number"},
                           "mod":          {"type": "string", "enum": ["flatwing","tamarack"], "default": "flatwing"}}},
    }},
    {"type": "function", "function": {
        "name": "add_to_watch",
        "description": "Add airports and/or tails to the Teams watch list. Either parameter may be omitted.",
        "parameters": {"type": "object",
                       "properties": {
                           "airports": {"type": "array", "items": {"type": "string"}},
                           "tails":    {"type": "array", "items": {"type": "string"}}}},
    }},
    {"type": "function", "function": {
        "name": "get_watch_lists",
        "description": "Return current airport and tail watch lists plus whether Teams webhook is configured.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "send_teams_card",
        "description": "Post a custom Adaptive Card to the Teams channel. Use this when the user asks to notify/broadcast/share something with the team.",
        "parameters": {"type": "object", "required": ["title","summary"],
                       "properties": {
                           "title":   {"type": "string"},
                           "summary": {"type": "string"},
                           "facts":   {"type": "array",
                                       "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                                       "description": "List of [name, value] pairs shown as a fact-set in the card."}}},
    }},
    {"type": "function", "function": {
        "name": "start_history_backfill",
        "description": "Trigger the FlightAware historical backfill job in the background. Defaults to 180 days; capped at 730.",
        "parameters": {"type": "object",
                       "properties": {"days": {"type": "integer", "default": 180}}},
    }},
    {"type": "function", "function": {
        "name": "m2_yaw_damper_suspects",
        "description": "List M2 (C25M) tails that consistently fly at or below FL280 on missions longer than min_distance_nm. AFM imposes a max-altitude limit of FL280 when the yaw damper is INOP, so habitual low cruise on long legs flags a likely deferred yaw-damper squawk — a sales-conversation opener. Returns tail-by-tail counts and route examples.",
        "parameters": {"type": "object",
                       "properties": {
                           "days":            {"type": "integer", "default": 90, "description": "Lookback window in days"},
                           "min_distance_nm": {"type": "integer", "default": 300, "description": "Only count missions longer than this"},
                           "max_alt_ft":      {"type": "integer", "default": 28000, "description": "Top-altitude threshold (FL280 = 28000)"},
                           "min_low_flights": {"type": "integer", "default": 3, "description": "Only report tails with at least this many qualifying low-alt long legs"}}},
    }},
    {"type": "function", "function": {
        "name": "top_prospects",
        "description": "Top ATLAS sales prospects ranked by composite score (range pressure + fuel-stop chains + high-DA airports). Use for 'who should I call', 'best prospects', 'hottest leads'. Optionally scope to a sales region. Returns tails with their signals, operator, best long trip, and a dashboard link.",
        "parameters": {"type": "object",
                       "properties": {
                           "days":   {"type": "integer", "default": 30},
                           "region": {"type": "string", "enum": ["NA","EU_UK","OTHER"], "description": "Sales-region bucket"},
                           "limit":  {"type": "integer", "default": 10, "maximum": 25}}},
    }},
    {"type": "function", "function": {
        "name": "fuel_stop_chains",
        "description": "Fuel-stop chains: consecutive same-tail legs joined by a short ground stop where combined distance is a range signal ATLAS would have flown non-stop. Each chain is classified range_win (flat-wing can't, ATLAS can — a true eliminated fuel stop), operational (both could non-stop, stop was for payload/crew/fuel), or beyond (even ATLAS needs the stop). Optionally scope to one tail or a region.",
        "parameters": {"type": "object",
                       "properties": {
                           "days":   {"type": "integer", "default": 90},
                           "tail":   {"type": "string", "description": "Optional single N-number filter"},
                           "region": {"type": "string", "enum": ["NA","EU_UK","OTHER"]},
                           "limit":  {"type": "integer", "default": 20, "maximum": 50}}},
    }},
    {"type": "function", "function": {
        "name": "mustang_activity",
        "description": "Adjacent-tier Citation Mustang (C510) activity — an up-purchase signal for operators likely to move up into a CJ. A Mustang making fuel-stop chains means the operator is outgrowing the airplane (warm CJ/ATLAS conversation). Returns recent Mustang landings and chain hits.",
        "parameters": {"type": "object",
                       "properties": {
                           "days":  {"type": "integer", "default": 30},
                           "limit": {"type": "integer", "default": 25, "maximum": 50}}},
    }},
    {"type": "function", "function": {
        "name": "jetnet_owner",
        "description": "Registered owner, operator, and contact (phone/email/city) for a tail from JETNET. Use for 'who owns', 'who operates', 'contact for', 'phone number for' a tail. Reads the local cache first; set refresh=true to force a live JETNET lookup. If JETNET isn't entitled yet the tool returns available=false with a reason — relay that honestly, never invent owner data.",
        "parameters": {"type": "object", "required": ["nnumber"],
                       "properties": {
                           "nnumber": {"type": "string", "description": "N-number"},
                           "refresh": {"type": "boolean", "default": False, "description": "Force a live JETNET refresh instead of using cache"}}},
    }},
    {"type": "function", "function": {
        "name": "jetnet_history",
        "description": "Ownership / transaction history for a tail from JETNET (sales, leases, fractional moves). Use for 'ownership history', 'has this changed hands', 'transaction history'. If JETNET isn't entitled yet the tool returns available=false — relay honestly.",
        "parameters": {"type": "object", "required": ["nnumber"],
                       "properties": {
                           "nnumber": {"type": "string", "description": "N-number"},
                           "days":    {"type": "integer", "description": "Optional lookback window in days"}}},
    }},
]

_TOOL_FUNCS = {
    "query_sightings":        tool_query_sightings,
    "top_operators":          tool_top_operators,
    "tail_summary":           tool_tail_summary,
    "route_pair_stats":       tool_route_pair_stats,
    "airport_traffic":        tool_airport_traffic,
    "climb_stats":            tool_climb_stats,
    "wat_analysis":           tool_wat_analysis,
    "wat_max_weight":         tool_wat_max_weight,
    "add_to_watch":           tool_add_to_watch,
    "get_watch_lists":        tool_get_watch_lists,
    "send_teams_card":        tool_send_teams_card,
    "start_history_backfill": tool_start_history_backfill,
    "m2_yaw_damper_suspects": tool_m2_yaw_damper_suspects,
    "top_prospects":          tool_top_prospects,
    "fuel_stop_chains":       tool_fuel_stop_chains,
    "mustang_activity":       tool_mustang_activity,
    "jetnet_owner":           tool_jetnet_owner,
    "jetnet_history":         tool_jetnet_history,
}


SYSTEM_PROMPT = """You are the AI analyst for **A320/737 Sightings**, a sales-intelligence dashboard for Tamarack Aerospace's ATLAS Active Winglet retrofit program for the Cessna Citation 525 family.

Today's date: {today}.

# Citation 525 family sub-models you must know (authoritative)
| Serial range        | Sub-model | ICAO |
|---------------------|-----------|------|
| 525-0001..0359      | CJ        | C525 |
| 525-0360..0599      | CJ1       | C525 |
| 525-0600..0799      | CJ1+      | C525 |
| 525-0800+           | M2        | C25M |
| 525A-0001..~0193    | CJ2       | C25A |
| 525A-~0194+         | CJ2+      | C25A |
| 525B-0001..0582     | CJ3       | C25B |
| 525B-0583+          | CJ3+      | C25B |

CJ4 (525C / C25C) is a separate product line, **not in ATLAS scope** — never recommend it as an ATLAS prospect.

# What ATLAS does (why we sell it)
- Increases range (e.g. +300 nm on CJ, +200 nm on CJ2)
- Improves climb gradient (better OEI performance, faster to cruise altitude)
- Raises hot-day WAT payload ceiling (more useful load at high-DA airports)
- Eliminates fuel stops on borderline missions

# Database columns available
sightings: tail_number, ac_type, ac_subvariant, origin_icao, dest_icao, arrived_utc, operator, distance_nm, serial_number, is_tamarack_fleet (1=ATLAS active, 2=ATLAS removed, NULL=not Tamarack), top_altitude_ft, time_to_10k_sec, avg_climb_rate_fpm, peak_climb_rate_fpm, climb_gradient_pct.

# Metro / city → airport clusters (ALWAYS broaden a city or region name to the cluster)
When a user names a city, metro, or region (e.g. "NYC", "Manhattan", "South Florida", "Bay Area"), do NOT restrict to a single ICAO. Pass the whole cluster as an array to `route_pair_stats` / `query_sightings`, then explain in your reply *which* airports actually saw traffic. CJs almost never operate at slot-controlled airline hubs (KJFK, KLGA, KLAX, KORD) — they use nearby GA/business-jet fields.

| Metro / region        | Cluster (broaden to this) |
|-----------------------|---------------------------|
| NYC / New York / NY   | KTEB, KHPN, KJFK, KLGA, KEWR, KFRG, KMMU, KISP |
| South Florida / Miami | KMIA, KOPF, KFXE, KFLL, KBCT, KPBI, KTMB |
| LA / Los Angeles      | KVNY, KBUR, KSMO, KLGB, KHHR, KLAX |
| Bay Area / SF         | KSFO, KOAK, KSJC, KHWD, KCCR, KLVK, KPAO, KSQL |
| DC / Washington       | KIAD, KDCA, KBWI, KHEF, KMTN, KGAI, KJYO |
| Chicago               | KORD, KMDW, KPWK, KDPA, KUGN, KARR |
| Boston                | KBOS, KBED, KOWD, KLWM, KHYA |
| Dallas / DFW          | KDFW, KDAL, KADS, KRBD, KAFW |
| Houston               | KIAH, KHOU, KSGR, KDWH, KEFD |
| Denver                | KDEN, KAPA, KBJC, KFTG |
| Aspen / Vail area     | KASE, KEGE, KRIL, KGWS, KTEX |
| Sun Valley / Ketchum  | KSUN, KHLE |
| Naples / SW FL        | KAPF, KRSW, KFMY, KPGD |
| Phoenix               | KPHX, KSDL, KDVT, KGYR, KCHD, KFFZ |
| Las Vegas             | KLAS, KVGT, KHND |
| Seattle               | KSEA, KBFI, KRNT, KPAE |
| London (UK)           | EGLL, EGLC, EGKB, EGGW, EGSS, EGLF, EGTK |
| Paris                 | LFPB, LFPG, LFPO, LFPN |

Rule of thumb: if a broad query returns zero, expand the cluster further and/or lengthen the window before concluding "no activity." Never tell the user a route has zero flights without noting which specific ICAOs you queried, and offer a broadened alternative if you found nothing.

# Behavior
- Use tools to ground every numeric claim in real data; do not guess.
- Be brief and sales-focused. Surface dollar value when you can (e.g. fuel-stop burden, climb-time gain).
- For "who should I call", "best leads", "hottest prospects", use `top_prospects` (optionally region-scoped).
- For "who owns / operates / contact / phone for" a tail, use `jetnet_owner`. For "ownership history / has it changed hands", use `jetnet_history`. If either returns `available: false`, say plainly that JETNET data isn't available yet (entitlement pending) — NEVER invent an owner, phone, or email.
- For range-pressure evidence on a tail or region, use `fuel_stop_chains` and lead with `range_win` chains (the true ATLAS-eliminated fuel stops), not `operational` ones.
- For European questions, pass `region: "EU_UK"` to `query_sightings` / `top_prospects` / `fuel_stop_chains`.
- **Always include local time alongside UTC when displaying flight events.** Every sighting row exposes `arrived_local` (e.g. "5:07 PM PDT" at the destination airport) and `departed_local` (at the origin). Show them like: `Arrival: 2026-06-25 5:07 PM PDT (00:07 UTC)`. If the local field is empty (TZ couldn't be resolved), show UTC alone — don't fabricate.
- When the user asks you to *notify*, *post*, *tell the team*, or similar, call `send_teams_card` with a useful title + summary + facts list.
- When the user names airports or tails to watch, call `add_to_watch`.
- When asked broad questions like "what's notable", lead with the 2-3 most actionable findings.
- ALWAYS reference the correct ICAO ↔ sub-model mapping above. Never recommend ATLAS for CJ4.
- Prefer bullet lists and small markdown tables in your replies. Stay under ~300 words unless the user wants depth.
"""


def chat_once(messages: list[dict]) -> dict:
    """
    Run one chat turn with tool calling. `messages` should include all prior
    user/assistant turns plus the new user message.

    Returns dict:
        reply       — final assistant text
        tool_calls  — list of {name, args, result} that were executed
        messages    — updated message list to persist for next turn
    """
    if not config.OPENAI_ENABLED:
        return {
            "reply":      "AI chat is not configured. Set OPENAI_API_KEY in the .env to enable.",
            "tool_calls": [],
            "messages":   messages,
        }

    client = _get_client()
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    full_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=today)},
        *messages,
    ]
    executed: list[dict[str, Any]] = []

    # Tool-call loop (max 4 rounds to avoid runaway)
    for _round in range(4):
        resp = _completion_with_retry(
            client,
            model       = config.OPENAI_MODEL,
            messages    = full_messages,
            tools       = TOOLS,
            tool_choice = "auto",
            temperature = 0.2,
        )
        choice = resp.choices[0]
        msg    = choice.message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return {
                "reply":      msg.content or "",
                "tool_calls": executed,
                "messages":   messages + [{"role": "assistant", "content": msg.content or ""}],
            }

        # Append the assistant's tool-request turn
        full_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })

        # Execute each tool, append its result
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            func = _TOOL_FUNCS.get(name)
            if not func:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = func(**args)
                except Exception as e:   # noqa: BLE001
                    result = {"error": f"{type(e).__name__}: {e}"}
            executed.append({"name": name, "args": args, "result": result})
            # Truncate huge result payloads before feeding back to the LLM
            result_str = _truncate_result(json.dumps(result, default=str))
            full_messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result_str,
            })

    # Tool-loop guard hit — final synthesis without more tools
    resp = _completion_with_retry(
        client,
        model       = config.OPENAI_MODEL,
        messages    = full_messages,
        temperature = 0.2,
    )
    final = resp.choices[0].message.content or ""
    return {
        "reply":      final,
        "tool_calls": executed,
        "messages":   messages + [{"role": "assistant", "content": final}],
    }


def chat_stream(messages: list[dict]):
    """
    Streaming variant of ``chat_once``. A generator yielding event dicts the
    SSE endpoint serializes to the browser:

        {"type": "status", "text": "Running query_sightings…"}  tool activity
        {"type": "token",  "text": "…"}                          answer token
        {"type": "done",   "reply": str, "tool_calls": [...],
                           "messages": [...]}                    final turn state
        {"type": "error",  "error": str}                         fatal error

    Tool-resolution rounds are accumulated (tools need the full call before
    they can run), but the visible answer streams token-by-token. Falls back
    cleanly when OpenAI isn't configured.
    """
    if not config.OPENAI_ENABLED:
        msg = "AI chat is not configured. Set OPENAI_API_KEY in the .env to enable."
        yield {"type": "token", "text": msg}
        yield {"type": "done", "reply": msg, "tool_calls": [], "messages": messages}
        return

    client = _get_client()
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    full_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=today)},
        *messages,
    ]
    executed:   list[dict[str, Any]] = []
    final_parts: list[str] = []

    for _round in range(5):
        try:
            stream = _completion_with_retry(
                client,
                model       = config.OPENAI_MODEL,
                messages    = full_messages,
                tools       = TOOLS,
                tool_choice = "auto",
                temperature = 0.2,
                stream      = True,
            )
        except Exception as e:                            # noqa: BLE001
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
            return

        tool_acc: dict[int, dict[str, str]] = {}
        content_acc: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                content_acc.append(delta.content)
                yield {"type": "token", "text": delta.content}
            for tc in (delta.tool_calls or []):
                idx = tc.index or 0
                entry = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    entry["id"] = tc.id
                if tc.function and tc.function.name:
                    entry["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    entry["arguments"] += tc.function.arguments

        assistant_content = "".join(content_acc)

        # No tool calls this round → the streamed content IS the final answer.
        if not tool_acc:
            final_parts.append(assistant_content)
            reply = "".join(final_parts)
            yield {"type": "done", "reply": reply, "tool_calls": executed,
                   "messages": messages + [{"role": "assistant", "content": reply}]}
            return

        # Append the assistant's tool-request turn, then run each tool.
        ordered = [tool_acc[i] for i in sorted(tool_acc)]
        full_messages.append({
            "role":    "assistant",
            "content": assistant_content or "",
            "tool_calls": [
                {"id": t["id"] or f"call_{i}", "type": "function",
                 "function": {"name": t["name"], "arguments": t["arguments"] or "{}"}}
                for i, t in enumerate(ordered)
            ],
        })
        for i, t in enumerate(ordered):
            name = t["name"]
            yield {"type": "status", "text": f"Running {name}\u2026"}
            try:
                args = json.loads(t["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            func = _TOOL_FUNCS.get(name)
            if not func:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = func(**args)
                except Exception as e:                    # noqa: BLE001
                    result = {"error": f"{type(e).__name__}: {e}"}
            executed.append({"name": name, "args": args, "result": result})
            full_messages.append({
                "role":         "tool",
                "tool_call_id": t["id"] or f"call_{i}",
                "content":      _truncate_result(json.dumps(result, default=str)),
            })

    # Tool-loop guard hit — final synthesis stream without more tools.
    try:
        stream = _completion_with_retry(
            client,
            model       = config.OPENAI_MODEL,
            messages    = full_messages,
            temperature = 0.2,
            stream      = True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                final_parts.append(delta.content)
                yield {"type": "token", "text": delta.content}
    except Exception as e:                                # noqa: BLE001
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return

    reply = "".join(final_parts)
    yield {"type": "done", "reply": reply, "tool_calls": executed,
           "messages": messages + [{"role": "assistant", "content": reply}]}
