"""
webapp.py — lightweight Flask status dashboard for A320/737 Sightings

Runs on port 8737 alongside the polling daemon.
Exposes:
  GET /                      — HTML dashboard (last sightings + daemon status)
  GET /export/prospects.csv  — CSV export of prospect scores
  GET /health                — JSON health check
  GET /_version              — git SHA + start time of the running process
"""

import csv
import html
import json
from pathlib import Path
import io
import json as _json
import sqlite3
import subprocess
import time as _time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock as _Lock

from flask import Flask, jsonify, Response, request, redirect, url_for

import airports
import config
import costs
import database
import sales_plan
import tamarack_fleet
import teams_notifier
import guest_access
import watch_airports
import usage_tracker
import watch_tails
import notify_settings

app = Flask(__name__)

SIM_RESULTS_PATHS = [
    Path("/tmp/tma_a320_bin_sim.json"),
    Path("./tma_a320_bin_sim.json"),
]


def _load_sim_result_index() -> dict[tuple[str, str], dict]:
    for path in SIM_RESULTS_PATHS:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                return {
                    (str(r.get("distance_bin") or ""), str(r.get("altitude_bin") or "")): r
                    for r in data.get("rows", [])
                }
        except Exception:
            continue
    return {}

# ── Usage tracking: "Who are you?" cookie + page-view logger ────────────────

_IDENTIFY_COOKIE = "525_user"
_GUEST_COOKIE    = "525_guest"
_SKIP_TRACKING_PREFIXES = ("/health", "/_version", "/admin/", "/webhook/", "/api/")


def _valid_guest_token() -> dict | None:
    """Return the guest token row if the request carries a valid unexpired 525_guest cookie."""
    gt = request.cookies.get(_GUEST_COOKIE, "").strip()
    if not gt:
        return None
    return guest_access.get_token(gt)


# ── /api/* auth + rate-limit guard ──────────────────────────────────────────
# The /api/chat endpoint drives paid OpenAI calls, so it MUST require the same
# identify or guest cookie the rest of the dashboard uses, plus a per-actor
# sliding-window rate limit. In-memory buckets are fine for a single container —
# they reset on restart, which is acceptable for an anti-abuse floor.

_RL_LOCK: _Lock = _Lock()
_RL_HISTORY: dict[str, deque] = {}


def _current_actor() -> tuple[str | None, str]:
    """Return (actor_key, display_label) for the current request.

    actor_key is a stable per-identity string used as the rate-limit bucket key.
    Returns (None, "anonymous") when the caller has no valid cookie — the
    /api/* guard treats that as unauthenticated.
    """
    g = _valid_guest_token()
    if g is not None:
        return (f"guest:{g['token'][:8]}", f"guest:{g.get('label') or 'guest'}")
    u = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if u:
        return (f"user:{u}", u)
    return (None, "anonymous")


def _rate_limit_ok(bucket: str, max_calls: int, window_seconds: int) -> tuple[bool, int]:
    """Sliding-window rate limit. Returns (allowed, retry_after_seconds)."""
    now = _time.monotonic()
    cutoff = now - window_seconds
    with _RL_LOCK:
        dq = _RL_HISTORY.get(bucket)
        if dq is None:
            dq = deque()
            _RL_HISTORY[bucket] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_calls:
            retry = int(dq[0] + window_seconds - now) + 1
            return (False, max(retry, 1))
        dq.append(now)
        return (True, 0)


def _guard_api(bucket_name: str, per_min: int, per_hour: int):
    """Enforce auth + two-window rate limit on an /api/* endpoint.

    Returns None when the request may proceed, or a Flask (response, status)
    tuple to short-circuit the handler.
    """
    actor_key, _display = _current_actor()
    if actor_key is None:
        return jsonify({
            "error": "authentication required",
            "detail": "identify yourself in the A320/737 Sightings dashboard first",
        }), 401
    ok_m, retry_m = _rate_limit_ok(f"{bucket_name}:min:{actor_key}", per_min, 60)
    if not ok_m:
        return jsonify({
            "error": f"rate limit exceeded ({per_min}/min)",
            "retry_after_seconds": retry_m,
        }), 429
    ok_h, retry_h = _rate_limit_ok(f"{bucket_name}:hr:{actor_key}", per_hour, 3600)
    if not ok_h:
        return jsonify({
            "error": f"rate limit exceeded ({per_hour}/hour)",
            "retry_after_seconds": retry_h,
        }), 429
    return None


@app.before_request
def _track_page_view():
    """Log every page load if the user has identified themselves via the cookie picker.

    Guest visitors (525_guest cookie w/ valid token) are logged to the PRIVATE
    guest_access_log table instead of the team-visible page_views table —
    so they never appear in the daily digest or /activity page.
    """
    path = request.path
    if any(path.startswith(p) for p in _SKIP_TRACKING_PREFIXES):
        return
    # Guest path takes precedence over identify cookie, so a guest who
    # happens to have an old team cookie in the same browser still stays
    # invisible to the team-visible digest.
    guest = _valid_guest_token()
    if guest is not None:
        try:
            guest_access.log_access(
                guest["token"], guest.get("label", ""), path,
                remote_addr=request.remote_addr or "",
                user_agent=request.headers.get("User-Agent", ""),
            )
        except Exception:
            pass
        return
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if user:
        usage_tracker.record_view(user, path)


@app.post("/identify")
def identify():
    """Set the 525_user cookie from the 'Who are you?' picker."""
    from flask import redirect, make_response
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect("/", code=303)
    resp = make_response(redirect("/", code=303))
    # Cookie lasts 1 year; SameSite=Lax so it works with normal navigation.
    resp.set_cookie(_IDENTIFY_COOKIE, name, max_age=365 * 86400,
                    samesite="Lax", httponly=False)
    return resp


@app.get("/identify/change")
def identify_change():
    """Clear the identity cookie so the picker re-appears."""
    from flask import redirect, make_response
    resp = make_response(redirect("/", code=303))
    resp.delete_cookie(_IDENTIFY_COOKIE)
    return resp


def _identify_modal_html() -> str:
    """Return the 'Who are you?' full-screen modal.
    Only shown when the 525_user cookie is NOT set AND no valid guest cookie.
    Also injects a tiny 'change identity' link into pages where the cookie IS set.
    Guests get a small 'Guest access — expires in Xm' pill instead.
    """
    # Guest wins over identity — keep guest cover intact even if a stale team
    # cookie is present on the same browser.
    guest = _valid_guest_token()
    if guest is not None:
        try:
            exp = datetime.fromisoformat(guest["effective_expires_at"])
            mins_left = max(0, int((exp - datetime.now(timezone.utc)).total_seconds() // 60))
        except Exception:
            mins_left = 0
        label = guest.get("label") or "guest"
        return (
            f'<div style="position:fixed;bottom:18px;left:18px;z-index:49;'
            f'background:#3f2a08;border:1px solid #b45309;border-radius:6px;'
            f'padding:5px 12px;font-size:11px;color:#fbbf24;">'
            f'\U0001F3AB Guest access &middot; <strong>{label}</strong> &middot; '
            f'expires in {mins_left}m'
            f'</div>'
        )
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if user:
        # Already identified — just show a subtle indicator + change link
        return (
            f'<div style="position:fixed;bottom:18px;left:18px;z-index:49;'
            f'background:#1e293b;border:1px solid #1e3a5f;border-radius:6px;'
            f'padding:5px 12px;font-size:11px;color:#94a3b8;">'
            f'Logged in as <strong style="color:#e2e8f0;">{user}</strong> &nbsp;'
            f'<a href="/identify/change" style="color:#60a5fa;">change</a>'
            f'</div>'
        )
    # Not identified — show the modal
    buttons = ""
    for name in usage_tracker.TEAM_ROSTER:
        buttons += (
            f'<button type="submit" name="name" value="{name}" '
            f'style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;'
            f'border-radius:8px;padding:14px 24px;font-size:15px;font-weight:600;'
            f'cursor:pointer;transition:background 0.15s,border-color 0.15s;" '
            f'onmouseover="this.style.background=\'#334155\';this.style.borderColor=\'#60a5fa\'" '
            f'onmouseout="this.style.background=\'#1e293b\';this.style.borderColor=\'#334155\'"'
            f'>{name}</button>'
        )
    return (
        f'<div id="identify-modal" style="position:fixed;inset:0;z-index:9999;'
        f'background:rgba(15,23,42,0.95);display:flex;align-items:center;'
        f'justify-content:center;">'
        f'<form method="post" action="/identify" style="text-align:center;max-width:480px;">'
        f'<div style="font-size:22px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">'
        f'Welcome to A320/737 Sightings</div>'
        f'<div style="font-size:14px;color:#94a3b8;margin-bottom:24px;">'
        f'Who are you? (one-time — stored in a cookie)</div>'
        f'<div style="display:flex;flex-direction:column;gap:10px;">'
        f'{buttons}'
        f'</div>'
        f'</form></div>'
    )

# Capture the git commit this process started with — used by trigger_deploy.py
# to verify a deploy actually restarted the container.
try:
    _GIT_COMMIT = subprocess.run(
        ["git", "-C", "/app", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip() or "unknown"
except Exception:
    _GIT_COMMIT = "unknown"
_STARTED_AT = datetime.now(timezone.utc).isoformat()

# Shared state written by main.py
daemon_state: dict = {
    "status": "starting",       # "running" | "error" | "starting"
    "last_poll_utc": None,
    "last_error": None,
    "sightings_total": 0,
    "active_sources": [],
}

DB_PATH = Path(__file__).parent / "sightings.db"

A320_FAMILY_TYPES = {"A318", "A319", "A320", "A321", "A19N", "A20N", "A21N"}
B737_FAMILY_TYPES = {"B736", "B737", "B738", "B739", "B37M", "B38M", "B39M", "B3XM"}


def _normalize_family(family: str | None) -> str | None:
    fam = (family or "").strip().upper().replace("-", "")
    if fam in {"A320", "AIRBUS", "AIRBUS320"}:
        return "A320"
    if fam in {"737", "B737", "BOEING", "BOEING737"}:
        return "B737"
    return None


def _family_sql(family: str | None) -> tuple[str, tuple]:
    fam = _normalize_family(family)
    if not fam:
        return "", ()
    types = sorted(A320_FAMILY_TYPES if fam == "A320" else B737_FAMILY_TYPES)
    return " AND UPPER(COALESCE(ac_type, '')) IN (" + ",".join("?" for _ in types) + ")", tuple(types)


def _family_label(family: str | None) -> str:
    fam = _normalize_family(family)
    return {"A320": "A320 Family", "B737": "Boeing 737 Family"}.get(fam, "All Narrowbodies")


def _family_query_suffix(family: str | None) -> str:
    fam = _normalize_family(family)
    return f"?family={fam}" if fam else ""


def _family_filter_html(active: str | None, base_path: str) -> str:
    fam = _normalize_family(active)
    def pill(label: str, value: str | None) -> str:
        is_active = fam == value or (fam is None and value is None)
        href = base_path + (f"?family={value}" if value else "")
        bg = "#2563eb" if is_active else "#1e293b"
        border = "#60a5fa" if is_active else "#334155"
        return f'<a href="{href}" style="display:inline-block;background:{bg};border:1px solid {border};color:#fff;padding:7px 12px;border-radius:999px;font-size:12px;font-weight:700;text-decoration:none;">{label}</a>'
    return '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 14px;align-items:center;"><span style="color:#94a3b8;font-size:12px;text-transform:uppercase;letter-spacing:.8px;">Family</span>' + pill("All", None) + pill("A320 Family", "A320") + pill("737 Family", "B737") + '</div>'


def _mission_bins_html(bins: list[dict]) -> str:
    if not bins:
        return '<div class="card" style="margin-bottom:18px;"><h2>Mission Bin Bridge</h2><div style="color:#94a3b8;">No distance/altitude bins yet for this filter.</div></div>'
    sim_index = _load_sim_result_index()
    rows = []
    chart_points = []
    for b in bins[:10]:
        key = (str(b.get("distance_bin") or ""), str(b.get("altitude_bin") or ""))
        sim = sim_index.get(key)
        if sim and sim.get("fuel_saved_pct_avg") is not None:
            raw_delta = float(sim.get("fuel_saved_pct_avg") or 0)
            savings = -raw_delta
            color = "#22c55e" if savings >= 0 else "#ef4444"
            route = f'{html.escape(sim.get("sim_dep_icao") or "")}→{html.escape(sim.get("sim_arr_icao") or "")}'
            chart_points.append({
                "distance_nm": float(sim.get("representative_distance_nm") or b.get("avg_distance_nm") or 0),
                "savings_pct": savings,
                "altitude_bin": str(sim.get("altitude_bin") or b.get("altitude_bin") or ""),
                "flights": int(b.get("count") or 0),
                "route": route,
            })
            route_line = f'<div style="color:#94a3b8;font-size:11px;">Rep route: {route}; 70/85/95% MTOW</div>'
            status = (
                f'<span style="color:{color};font-weight:800;">{savings:+.2f}% saved</span>'
                f'{route_line}'
                f'<div style="color:#f59e0b;font-size:11px;">Calibration pending</div>'
            )
        else:
            status = '<span style="color:#f59e0b;font-weight:700;">Awaiting sim run</span>'
        rows.append(
            f'<tr><td>{html.escape(b.get("distance_bin") or "—")}</td>'
            f'<td>{html.escape(b.get("altitude_bin") or "—")}</td>'
            f'<td style="text-align:right;color:#60a5fa;font-weight:800;">{int(b.get("count") or 0):,}</td>'
            f'<td style="text-align:right;">{int(b.get("avg_distance_nm") or 0):,} nm</td>'
            f'<td style="text-align:right;">FL{round((b.get("avg_altitude_ft") or 0)/100)}</td>'
            f'<td>{status}</td></tr>'
        )
    chart_html = _mission_bins_summary_chart(chart_points)
    return '<div class="card" style="margin-bottom:18px;"><h2>Mission Bin Bridge</h2><div style="color:#94a3b8;font-size:12px;margin-bottom:10px;">One row = one <b>distance × altitude</b> bin. Repeated stage lengths are not duplicates; they are the same distance band flown at different altitude bands. Status loads latest Tamarack_Mission_Analysis workup when available.</div><table><thead><tr><th>Stage Length Bin</th><th>Altitude Bin</th><th style="text-align:right;">Flights in Bin</th><th style="text-align:right;">Avg Dist</th><th style="text-align:right;">Avg FL</th><th>Sim Status</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' + chart_html + '</div>'


def _mission_bins_summary_chart(points: list[dict]) -> str:
    points = [p for p in points if p.get("distance_nm") and p.get("savings_pct") is not None]
    if not points:
        return ""
    width, height = 920, 280
    ml, mr, mt, mb = 58, 22, 26, 42
    max_x = max(p["distance_nm"] for p in points) or 1
    min_y = min(0, min(p["savings_pct"] for p in points))
    max_y = max(1, max(p["savings_pct"] for p in points))
    pad_y = max(0.5, (max_y - min_y) * 0.15)
    min_y -= pad_y
    max_y += pad_y
    def sx(x: float) -> float:
        return ml + (x / max_x) * (width - ml - mr)
    def sy(y: float) -> float:
        return height - mb - ((y - min_y) / (max_y - min_y)) * (height - mt - mb)
    colors = {
        "< FL250": "#60a5fa",
        "FL250–310": "#22c55e",
        "FL310–350": "#eab308",
        "FL350–390": "#f97316",
        "FL390+": "#ef4444",
    }
    parts = [
        '<div style="margin-top:18px;border-top:1px solid #334155;padding-top:14px;">',
        '<div style="font-size:13px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Summary: fuel savings vs representative distance</div>',
        f'<svg width="100%" viewBox="0 0 {width} {height}" role="img" aria-label="Fuel savings summary chart">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#0f172a"/>',
        f'<line x1="{ml}" y1="{height-mb}" x2="{width-mr}" y2="{height-mb}" stroke="#475569"/>',
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{height-mb}" stroke="#475569"/>',
    ]
    for i in range(5):
        y = min_y + (max_y - min_y) * i / 4
        parts.append(f'<line x1="{ml}" y1="{sy(y):.1f}" x2="{width-mr}" y2="{sy(y):.1f}" stroke="#1e293b"/>')
        parts.append(f'<text x="{ml-8}" y="{sy(y)+4:.1f}" text-anchor="end" fill="#94a3b8" font-size="11">{y:.1f}%</text>')
    zero_y = sy(0)
    parts.append(f'<line x1="{ml}" y1="{zero_y:.1f}" x2="{width-mr}" y2="{zero_y:.1f}" stroke="#64748b" stroke-dasharray="4 4"/>')
    for p in points:
        radius = min(16, max(5, (p.get("flights") or 0) ** 0.5 / 2.5))
        color = colors.get(p.get("altitude_bin"), "#a78bfa")
        label = html.escape(f'{p.get("route", "")} {p.get("altitude_bin", "")}: {p["savings_pct"]:.2f}% saved')
        parts.append(f'<circle cx="{sx(p["distance_nm"]):.1f}" cy="{sy(p["savings_pct"]):.1f}" r="{radius:.1f}" fill="{color}" stroke="#e2e8f0" stroke-width="1"><title>{label}</title></circle>')
    parts.append(f'<text x="{width/2:.0f}" y="{height-12}" text-anchor="middle" fill="#94a3b8" font-size="12">Representative distance (nm)</text>')
    parts.append(f'<text x="14" y="{height/2:.0f}" transform="rotate(-90 14 {height/2:.0f})" text-anchor="middle" fill="#94a3b8" font-size="12">Fuel saved (%)</text>')
    lx = width - 160
    ly = 28
    for i, (name, color) in enumerate(colors.items()):
        parts.append(f'<circle cx="{lx}" cy="{ly+i*18}" r="5" fill="{color}"/><text x="{lx+12}" y="{ly+i*18+4}" fill="#cbd5e1" font-size="11">{html.escape(name)}</text>')
    parts.append('</svg></div>')
    return ''.join(parts)


def _opportunity_feed_html(items: list[dict]) -> str:
    if not items:
        return ""
    cards = []
    for it in items:
        tags = "".join(f'<span style="display:inline-block;background:#334155;color:#cbd5e1;border:1px solid #475569;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:800;margin-right:4px;margin-bottom:4px;">{html.escape(t)}</span>' for t in it.get("tags", []))
        route = f'{html.escape(it.get("origin_icao") or "—")} → {html.escape(it.get("dest_icao") or "—")}'
        dist = f'{int(it.get("distance_nm") or 0):,} nm' if it.get("distance_nm") else "—"
        alt = f'FL{round((it.get("altitude_ft") or 0)/100)}' if it.get("altitude_ft") else "—"
        link = it.get("tracking_url") or ""
        link_html = f'<a href="{html.escape(link)}" target="_blank">track</a>' if link else ""
        cards.append(
            f'<div style="background:#111827;border:1px solid #334155;border-radius:8px;padding:10px;">'
            f'<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;"><div>{tags}</div><div style="color:#60a5fa;font-weight:800;font-size:12px;">{int(it.get("score") or 0)}</div></div>'
            f'<div style="font-weight:800;margin-top:4px;color:#e2e8f0;">{html.escape(it.get("operator") or "Unknown")}</div>'
            f'<div style="color:#cbd5e1;font-size:13px;margin-top:3px;">{html.escape(it.get("tail_number") or "—")} · {html.escape(it.get("type") or "—")} · {route} · {dist} · {alt} {link_html}</div>'
            f'<div style="color:#94a3b8;font-size:12px;margin-top:5px;line-height:1.35;">{html.escape(it.get("why") or "Observed pattern worth reviewing")}</div>'
            f'</div>'
        )
    return '<section style="background:#1e293b;border-radius:10px;padding:14px;margin:0 0 18px;"><div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px;"><h2 style="font-size:16px;margin:0;color:#e2e8f0;">Opportunity Feed</h2><div style="font-size:12px;color:#94a3b8;">Observed mission signals, not simulator claims yet</div></div><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;">' + "".join(cards) + '</div></section>'


def _recent_sightings(limit: int = 500, offset: int = 0, family: str | None = None) -> list[dict]:
    if not DB_PATH.exists():
        return []
    family_sql, family_args = _family_sql(family)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT tail_number, ac_type, ac_subvariant, origin_icao, dest_icao,
               departed_utc, arrived_utc, operator, tracking_url, source, notified_at,
               distance_nm, serial_number, is_tamarack_fleet,
               top_altitude_ft, time_to_10k_sec, time_to_top_sec, avg_climb_rate_fpm,
               peak_climb_rate_fpm, climb_gradient_pct,
               initial_cruise_alt_ft, time_to_initial_cruise_sec
        FROM v_sightings_dedup
        WHERE 1=1{family_sql}
        ORDER BY id DESC LIMIT ? OFFSET ?
        """,
        (*family_args, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _recent_eu_sightings(limit: int = 200, offset: int = 0, family: str | None = None) -> list[dict]:
    """Latest EU_UK-region sightings, newest first. Same schema as _recent_sightings."""
    if not DB_PATH.exists():
        return []
    family_sql, family_args = _family_sql(family)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT tail_number, ac_type, ac_subvariant, origin_icao, dest_icao,
               departed_utc, arrived_utc, operator, tracking_url, source, notified_at,
               distance_nm, serial_number, is_tamarack_fleet,
               top_altitude_ft, time_to_10k_sec, time_to_top_sec, avg_climb_rate_fpm,
               peak_climb_rate_fpm, climb_gradient_pct,
               initial_cruise_alt_ft, time_to_initial_cruise_sec
        FROM v_sightings_dedup
        WHERE region = 'EU_UK'{family_sql}
        ORDER BY id DESC LIMIT ? OFFSET ?
        """,
        (*family_args, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _sightings_total_eu(family: str | None = None) -> int:
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(DB_PATH)
    family_sql, family_args = _family_sql(family)
    n = conn.execute(
        f"SELECT COUNT(*) FROM v_sightings_dedup WHERE region = 'EU_UK'{family_sql}",
        family_args,
    ).fetchone()[0]
    conn.close()
    return int(n)


def _sightings_total(family: str | None = None) -> int:
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(DB_PATH)
    family_sql, family_args = _family_sql(family)
    n = conn.execute(f"SELECT COUNT(*) FROM v_sightings_dedup WHERE 1=1{family_sql}", family_args).fetchone()[0]
    conn.close()
    return int(n)


def _atlas_badge(is_fleet: int | None, serial: str | None) -> str:
    """Return an HTML badge for ATLAS fleet status, or empty string."""
    sn_tip = f' title="{serial}"' if serial else ""
    if is_fleet == 1:
        return f'<span{sn_tip} style="background:#16a34a22;color:#22c55e;border:1px solid #16a34a55;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;white-space:nowrap;cursor:default;">ATLAS</span>'
    if is_fleet == 2:
        return f'<span{sn_tip} style="background:#33415522;color:#94a3b8;border:1px solid #64748b55;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;white-space:nowrap;cursor:default;">ATLAS&#160;rmvd</span>'
    return ""


def _type_label(ac_type: str, subvariant: str = "") -> str:
    """
    Return the best available sub-model label for an aircraft type.
    Always resolves to one of the 8 in-scope sub-models:
        CJ, CJ1, CJ1+, M2, CJ2, CJ2+, CJ3, CJ3+
    When the exact sub-variant isn't known (foreign tails not in FAA cache),
    we pick the most likely sub-model for that ICAO type.
    """
    from atlas_config import ATLAS_SUBVARIANT
    if subvariant:
        sv = ATLAS_SUBVARIANT.get(subvariant.upper())
        if sv:
            return sv["label"]
    # ICAO-level fallback: pick the canonical (most common) sub-model per ICAO
    fallback = {
        "C525": "CJ",    # 525-xxxx — could be CJ / CJ1 / CJ1+ / M2; CJ most common
        "C25A": "CJ2",   # 525A-xxxx — CJ2 or CJ2+; CJ2 the base model
        "C25B": "CJ3",   # 525B-xxxx — CJ3 or CJ3+; CJ3 the base model
        "C25M": "M2",    # 525-0800+ — only one sub-model in this ICAO
    }
    return fallback.get((ac_type or "").upper(), ac_type or "—")


def _render_sighting_row_html(r: dict) -> str:
    """
    Render one sighting `<tr>...</tr>` matching the 16-column homepage layout.
    Shared by the homepage stream and the /eu-insights flight-listing table.
    """
    track = f'<a href="{r["tracking_url"]}" target="_blank" style="color:#60a5fa;">Track</a>' if r.get("tracking_url") else "—"
    dist = r.get("distance_nm")
    dist_cell = f'<span style="color:#f59e0b;font-weight:600;">{int(dist)} nm</span>' if dist else "—"
    dep = r.get("departed_utc")
    arr = r.get("arrived_utc")
    dur_cell = '<span style="color:#64748b;">—</span>'
    block_cell = '<span style="color:#64748b;">—</span>'
    if dep and arr:
        try:
            _d = datetime.fromisoformat(dep.replace("Z", "+00:00"))
            _a = datetime.fromisoformat(arr.replace("Z", "+00:00"))
            total_min = int(round((_a - _d).total_seconds() / 60))
            if 5 <= total_min <= 20 * 60:
                hh, mm = divmod(total_min, 60)
                dur_cell = (
                    f'<span style="font-family:monospace;color:#e2e8f0;font-weight:600;">'
                    f'{hh:02d}:{mm:02d}</span>'
                )
                if dist:
                    kts = round(dist / (total_min / 60))
                    block_cell = (
                        f'<span style="color:#60a5fa;font-weight:600;" '
                        f'title="Block speed = trip distance ÷ trip duration">'
                        f'{kts} kts</span>'
                    )
        except Exception:
            pass

    cost = costs.flight_cost(
        distance_nm = dist,
        subvariant  = r.get("ac_subvariant", ""),
        ac_type     = r.get("ac_type", ""),
    )
    if cost:
        programs_usd = cost["engine_usd"] + cost["parts_usd"]
        fuel_tip = (f'{cost["fuel_gal"]:,} gal @ ${costs.FUEL_PRICE_USD_PER_GAL:.2f}/gal '
                    f'· {cost["hours"]:.1f} hr @ {cost["gph"]} gph')
        prog_tip = (f'Engine reserve ${cost["engine_usd"]:,} '
                    f'+ Parts reserve ${cost["parts_usd"]:,} '
                    f'· {cost["hours"]:.1f} hr')
        fuel_cell = (
            f'<span title="{fuel_tip}" style="color:#f59e0b;font-weight:600;cursor:help;">'
            f'${cost["fuel_usd"]:,}</span>'
        )
        programs_cell = (
            f'<span title="{prog_tip}" style="color:#22c55e;font-weight:600;cursor:help;">'
            f'${programs_usd:,}</span>'
        )
    else:
        fuel_cell     = '<span style="color:#64748b;">—</span>'
        programs_cell = '<span style="color:#64748b;">—</span>'

    top_alt   = r.get("top_altitude_ft")
    t10k      = r.get("time_to_10k_sec")
    t_top     = r.get("time_to_top_sec")
    avg_fpm   = r.get("avg_climb_rate_fpm")
    peak      = r.get("peak_climb_rate_fpm")
    init_alt  = r.get("initial_cruise_alt_ft")
    init_t    = r.get("time_to_initial_cruise_sec")
    if top_alt:
        fl        = f'FL{int(round(top_alt/100)):03d}'
        t_top_str = f'{t_top // 60} min' if t_top else '—'
        t10k_str  = f'{t10k // 60} min' if t10k else '—'
        fpm_str   = f'{avg_fpm:,} fpm' if avg_fpm else '—'
        ctip = (f'Top: {top_alt:,} ft ({fl})  |  '
                f'0→top: {t_top_str}  |  '
                f'0→10k: {t10k_str}  |  '
                f'Avg climb: {avg_fpm or 0:,} fpm  |  '
                f'Peak: {peak or 0:,} fpm')
        if init_alt and init_t is not None:
            init_fl   = f'FL{int(round(init_alt/100)):03d}'
            init_min  = init_t // 60
            top_min   = (t_top // 60) if t_top else 0
            ctip = (f'Step climb: {init_fl} @ {init_min} min '
                    f'→ {fl} @ {top_min} min  |  ' + ctip)
            climb_cell = (
                f'<span title="{ctip}" style="cursor:help;display:inline-block;line-height:1.25;">'
                f'<span style="color:#fbbf24;font-weight:600;">{init_fl}</span>'
                f'<span style="color:#94a3b8;font-size:10px;"> &nbsp;{init_min} min</span>'
                f'<br>'
                f'<span style="color:#60a5fa;font-weight:700;">{fl}</span>'
                f'<span style="color:#94a3b8;font-size:10px;"> &nbsp;{t_top_str} · {fpm_str}</span>'
                f'</span>'
            )
        else:
            climb_cell = (
                f'<span title="{ctip}" style="cursor:help;">'
                f'<span style="color:#60a5fa;font-weight:700;">{fl}</span>'
                f'<br><span style="color:#94a3b8;font-size:10px;">'
                f'{t_top_str} ↑ · {fpm_str}</span>'
                f'</span>'
            )
    else:
        climb_cell = '<span style="color:#64748b;">—</span>'

    return (
        f"<tr>"
        f"<td>{r.get('tail_number') or '—'}</td>"
        f"<td>{_type_label(r.get('ac_type',''), r.get('ac_subvariant',''))}</td>"
        f"<td>{r.get('origin_icao') or '—'}</td>"
        f"<td>{r.get('dest_icao') or '—'}</td>"
        f"<td>{dist_cell}</td>"
        f"<td>{dur_cell}</td>"
        f"<td>{block_cell}</td>"
        f"<td>{fuel_cell}</td>"
        f"<td>{programs_cell}</td>"
        f"<td>{climb_cell}</td>"
        f"<td>{(r.get('arrived_utc') or '')[:16].replace('T',' ')} UTC</td>"
        f"<td style='color:#94a3b8;font-size:12px;'>{airports.local_time_at_icao(r.get('arrived_utc',''), r.get('dest_icao',''))}</td>"
        f"<td>{r.get('operator') or '—'}</td>"
        f"<td>{r.get('source','').replace('adsbexchange','ADS-B Exchange').replace('flightaware','FlightAware').replace('opensky','OpenSky')}</td>"
        f"<td>{track}</td>"
        f"</tr>"
    )


@app.get("/health")
def health():
    payload = dict(daemon_state)
    payload["faa_registry_loaded"] = tamarack_fleet._registry_loaded
    payload["faa_registry_count"]  = len(tamarack_fleet._nnum_to_sn)
    return jsonify(payload), 200 if daemon_state["status"] == "running" else 503


@app.get("/_version")
def version():
    """Returns the git SHA this running process was started with.
    trigger_deploy.py polls this to confirm a deploy actually restarted the container."""
    return jsonify({"commit": _GIT_COMMIT, "started_at": _STARTED_AT})


@app.post("/webhook/deploy")
def webhook_deploy():
    try:
        import os, subprocess, threading
        expected = os.getenv("DEPLOY_SECRET", "")
        secret = request.headers.get("X-Deploy-Secret", "")
        if not expected or secret != expected:
            return jsonify({"error": "unauthorized"}), 401

        def _do_deploy():
            import time, os, subprocess
            time.sleep(2)
            token = os.getenv("GITHUB_TOKEN", "")
            if token:
                subprocess.run(
                    ["git", "-C", "/app", "remote", "set-url", "origin",
                     f"https://{token}@github.com/volocchio/A320_737_Sightings.git"],
                    capture_output=True,
                )
                # Configure git globally so pip can install from private GitHub
                # repos (e.g. tamarack-wat-tables) via git+https:// without
                # embedding the token in requirements.txt.
                subprocess.run(
                    ["git", "config", "--global",
                     f"url.https://{token}@github.com/.insteadOf",
                     "https://github.com/"],
                    capture_output=True,
                )
            subprocess.run(["git", "-C", "/app", "pull"], capture_output=True)
            # If requirements.txt changed, install any new packages before restart
            # so the new Python process starts with all dependencies present.
            subprocess.run(
                ["pip", "install", "--quiet", "--no-cache-dir",
                 "-r", "/app/requirements.txt"],
                capture_output=True,
            )
            # Code is volume-mounted so no full image rebuild needed — just
            # restart the container so the new Python process picks up the
            # updated source files (and freshly-installed packages).
            subprocess.Popen(["docker", "restart", "a320737_sightings"])

        threading.Thread(target=_do_deploy, daemon=True).start()
        return jsonify({"status": "deploying"}), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/webhook/git-status")
def git_status():
    """Diagnostic: show git log and pull output. Protected by DEPLOY_SECRET."""
    import os, subprocess
    expected = os.getenv("DEPLOY_SECRET", "")
    if not expected or request.args.get("secret", "") != expected:
        return jsonify({"error": "unauthorized"}), 401
    log = subprocess.run(["git", "-C", "/app", "log", "--oneline", "-5"],
                         capture_output=True, text=True)
    pull = subprocess.run(["git", "-C", "/app", "pull", "--dry-run"],
                          capture_output=True, text=True)
    docker_ps = subprocess.run(["docker", "ps", "-a", "--format",
                                 "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                                capture_output=True, text=True)
    token_set = bool(os.getenv("GITHUB_TOKEN"))
    return jsonify({
        "git_log": log.stdout.strip(),
        "pull_dry_run": pull.stdout.strip() or pull.stderr.strip(),
        "github_token_set": token_set,
        "git_returncode": log.returncode,
        "docker_ps": docker_ps.stdout.strip() or docker_ps.stderr.strip(),
    })


@app.post("/webhook/restart")
def webhook_restart():
    """Fast restart: git pull already done, just restart the container."""
    import os, subprocess, threading
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401

    def _do_restart():
        import time
        time.sleep(1)
        subprocess.Popen(["docker", "restart", "a320737_sightings"])

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"status": "restarting"}), 202


# Module-level state for the async rescrub job (one at a time)
_rescrub_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "summary": None}


@app.post("/admin/rescrub-subvariants")
def rescrub_subvariants():
    """
    Force-rescrub ac_type, ac_subvariant, serial_number, is_tamarack_fleet
    for every sighting with a tail_number. Used after a resolver bug fix.
    Runs in a background thread (job takes >100s, exceeds Cloudflare timeout).
    Protected by DEPLOY_SECRET (header or ?secret=).
    Poll GET /admin/rescrub-status?secret=... for completion.
    """
    import os, threading as _t
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if _rescrub_state["status"] == "running":
        return jsonify({"error": "already running", "started_at": _rescrub_state["started_at"]}), 409

    def _run():
        _rescrub_state["status"]      = "running"
        _rescrub_state["started_at"]  = datetime.now(timezone.utc).isoformat()
        _rescrub_state["finished_at"] = None
        _rescrub_state["summary"]     = None
        try:
            _rescrub_state["summary"] = database.rescrub_fleet_status()
            _rescrub_state["status"]  = "done"
        except Exception as e:
            _rescrub_state["summary"] = {"error": str(e)}
            _rescrub_state["status"]  = "error"
        finally:
            _rescrub_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    _t.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "poll": "/admin/rescrub-status"}), 202


@app.get("/admin/rescrub-status")
def rescrub_status():
    """Poll the most recent rescrub job. Protected by DEPLOY_SECRET."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_rescrub_state), 200


@app.post("/admin/reload-faa")
def reload_faa():
    """
    Force-reload the FAA aircraft registry from cache (or download from FAA
    if cache missing). Returns load status + record count.
    Protected by DEPLOY_SECRET.
    """
    import os, logging, io as _io
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401

    # Capture log output during the load attempt so the caller can see why it failed
    buf = _io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        ok = tamarack_fleet.load_faa_registry(force=True)
    except Exception as e:
        return jsonify({
            "ok":     False,
            "error":  f"{type(e).__name__}: {e}",
            "logs":   buf.getvalue(),
            "registry_loaded": tamarack_fleet._registry_loaded,
            "registry_count":  len(tamarack_fleet._nnum_to_sn),
            "cache_path":      str(tamarack_fleet._CACHE_PATH),
            "cache_exists":    tamarack_fleet._CACHE_PATH.exists(),
        }), 500
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    return jsonify({
        "ok":               ok,
        "registry_loaded":  tamarack_fleet._registry_loaded,
        "registry_count":   len(tamarack_fleet._nnum_to_sn),
        "cache_path":       str(tamarack_fleet._CACHE_PATH),
        "cache_exists":     tamarack_fleet._CACHE_PATH.exists(),
        "logs":             buf.getvalue(),
    }), 200


@app.get("/admin/anomalous-rows")
def anomalous_rows():
    """
    Diagnostic: surface rows whose ac_type and ac_subvariant disagree on what
    serial-letter prefix they should belong to. Useful for catching ingest-time
    misclassifications. Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401

    # Expected ac_type for each sub-variant key
    expected_type = {
        "CJ": "C525", "CJ1": "C525", "CJ1PLUS": "C525", "M2": "C25M",
        "CJ2": "C25A", "CJ2PLUS": "C25A",
        "CJ3": "C25B", "CJ3PLUS": "C25B",
    }
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT id, tail_number, ac_type, ac_subvariant, serial_number, "
            "origin_icao, dest_icao, arrived_utc, source, operator "
            "FROM sightings "
            "WHERE ac_subvariant IS NOT NULL AND ac_subvariant != ''"
        ).fetchall()
    bad = []
    for r in rows:
        sv = (r["ac_subvariant"] or "").upper()
        ac = (r["ac_type"] or "").upper()
        exp = expected_type.get(sv)
        if exp and exp != ac:
            bad.append(dict(r))
    return jsonify({"count": len(bad), "rows": bad[:50]}), 200


@app.get("/admin/null-subvariant-sample")
def null_subvariant_sample():
    """
    Diagnostic: dump distinct (tail_number, ac_type, serial_number) tuples
    for sightings with NULL ac_subvariant, plus what the resolver currently
    returns for each. Lets us see why the rescrub left ~200 rows unresolved.
    Protected by DEPLOY_SECRET.
    """
    import os, tamarack_fleet as _tf
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    _tf.load_faa_registry()
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT tail_number, ac_type, serial_number "
            "FROM sightings "
            "WHERE (ac_subvariant IS NULL OR ac_subvariant = '') "
            "  AND tail_number IS NOT NULL AND tail_number != '' "
            "ORDER BY ac_type, tail_number"
        ).fetchall()
    out = []
    for r in rows:
        tail = r["tail_number"]
        sn   = r["serial_number"] or ""
        sv_from_sn    = _tf.subvariant_from_sn(sn) if sn else ""
        sv_from_tail  = _tf.nnum_to_subvariant(tail)
        faa_model     = _tf.nnum_to_faa_model(tail)
        out.append({
            "tail":          tail,
            "ac_type":       r["ac_type"],
            "stored_sn":     sn,
            "lookup_sn":     _tf.nnum_to_sn(tail) or "",
            "faa_model":     faa_model,
            "sv_from_sn":    sv_from_sn,
            "sv_from_tail":  sv_from_tail,
        })
    return jsonify({"count": len(out), "rows": out}), 200



@app.get("/admin/sighting-type-counts")
def sighting_type_counts():
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT ac_type, ac_subvariant, COUNT(*) AS n, "
            "COUNT(serial_number) AS with_sn, "
            "COUNT(DISTINCT tail_number) AS tails "
            "FROM sightings GROUP BY ac_type, ac_subvariant "
            "ORDER BY n DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows]), 200


@app.post("/api/share-to-teams")
def api_share_to_teams():
    """
    Push an arbitrary chat reply (or any text snippet) as an Adaptive Card
    into the configured Teams channel. Body: {"title": str, "text": str}.
    Requires an identify or guest cookie; rate-limited per actor.
    """
    gate = _guard_api("share", per_min=5, per_hour=20)
    if gate is not None:
        return gate
    if not config.TEAMS_ENABLED:
        return jsonify({"sent": False, "error": "TEAMS_WEBHOOK_URL not configured"}), 400
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "A320/737 Sightings \u2014 shared from chat")[:120]
    text  = (body.get("text")  or "").strip()
    if not text:
        return jsonify({"sent": False, "error": "text is required"}), 400
    # Truncate to keep Adaptive Card payload reasonable
    if len(text) > 4000:
        text = text[:4000] + "\u2026"
    # Clean control characters / overly long bodies to reduce Teams renderer failures.
    text = teams_notifier._clean_text(text, limit=2500)
    payload = teams_notifier._adaptive_card(
        title       = title,
        subtitle    = "",
        facts       = [],
        action_url  = config.DASHBOARD_URL,
        action_label= "Open A320/737 Sightings dashboard",
    )
    # Stuff the body text into the card directly so it renders inline
    payload["attachments"][0]["content"]["body"].insert(1, {
        "type": "TextBlock", "wrap": True, "text": text, "spacing": "Small",
    })
    ok = teams_notifier.send(payload)
    return jsonify({"sent": ok}), 200 if ok else 502


@app.post("/api/chat")
def api_chat():
    """
    AI chat endpoint. Body: {"messages": [{"role": "user"|"assistant", "content": "..."}, ...]}
    Returns {"reply": str, "tool_calls": [...], "messages": [...]}

    Requires an identify or guest cookie (the same one the dashboard sets on
    first visit) and is rate-limited per actor to cap OpenAI spend from abuse
    or a runaway browser tab.
    """
    gate = _guard_api("chat", per_min=5, per_hour=30)
    if gate is not None:
        return gate
    import chat as _chat
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return jsonify({"error": "messages must be an array"}), 400
    # Trim absurdly long histories
    if len(messages) > 40:
        messages = messages[-40:]
    try:
        result = _chat.chat_once(messages)
    except Exception as e:   # noqa: BLE001
        # Surface as much detail as possible — OpenAI errors carry .body / .response
        detail = str(e)
        for attr in ("body", "response", "args"):
            v = getattr(e, attr, None)
            if v:
                detail = f"{detail}  |  {attr}={str(v)[:500]}"
        return jsonify({"error": f"{type(e).__name__}: {detail}"}), 500
    return jsonify(result), 200


@app.post("/api/chat/stream")
def api_chat_stream():
    """
    Streaming AI chat endpoint (Server-Sent Events). Same body/auth/rate-limit
    as /api/chat, but the answer streams token-by-token so the widget can
    render it live. Each SSE line is `data: {json}` where json.type is one of
    status | token | done | error. The widget falls back to /api/chat if this
    endpoint or the stream fails.
    """
    gate = _guard_api("chat", per_min=5, per_hour=30)
    if gate is not None:
        return gate
    import chat as _chat
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return jsonify({"error": "messages must be an array"}), 400
    if len(messages) > 40:
        messages = messages[-40:]

    def _gen():
        try:
            for event in _chat.chat_stream(messages):
                yield f"data: {_json.dumps(event, default=str)}\n\n"
        except Exception as e:   # noqa: BLE001
            err = {"type": "error", "error": f"{type(e).__name__}: {e}"}
            yield f"data: {_json.dumps(err)}\n\n"

    return Response(_gen(), mimetype="text/event-stream", headers={
        "Cache-Control":     "no-cache",
        "X-Accel-Buffering": "no",   # disable proxy buffering (nginx); harmless elsewhere
        "Connection":        "keep-alive",
    })


@app.get("/watch")
def watch_form():
    """
    Public form to manage the airport + tail watch lists. When any new
    sighting's origin/dest matches a watched airport, or its tail matches
    a watched tail, a card is posted to the Teams channel.
    """
    cur_airports = watch_airports.get_watch_list()
    cur_tails    = watch_tails.get_watch_list()
    teams_state = "configured" if config.TEAMS_ENABLED else "NOT configured"
    teams_color = "#22c55e" if config.TEAMS_ENABLED else "#f97316"

    cur_interval = notify_settings.get_min_interval_minutes()
    freq_options_html = ""
    for m, label in notify_settings.INTERVAL_CHOICES:
        sel = " selected" if m == cur_interval else ""
        freq_options_html += f'<option value="{m}"{sel}>{label}</option>'

    airports_html = ""
    for c in cur_airports:
        airports_html += f'<span style="background:#1e3a5f;color:#60a5fa;padding:4px 10px;border-radius:4px;margin:0 6px 6px 0;display:inline-block;font-family:monospace;font-weight:700;">{c}</span>'
    if not airports_html:
        airports_html = '<span style="color:#94a3b8;">No airports being watched yet.</span>'

    tails_html = ""
    for t in cur_tails:
        tails_html += f'<span style="background:#3f2a08;color:#fbbf24;padding:4px 10px;border-radius:4px;margin:0 6px 6px 0;display:inline-block;font-family:monospace;font-weight:700;">{t}</span>'
    if not tails_html:
        tails_html = '<span style="color:#94a3b8;">No tails being watched yet.</span>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Watch list — A320/737 Sightings</title>
<style>
  body {{ background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif;padding:32px;max-width:900px;margin:0 auto; }}
  h1   {{ font-size:22px;margin-bottom:6px; }}
  .sub {{ color:#94a3b8;font-size:13px;margin-bottom:24px; }}
  textarea {{ width:100%;height:90px;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:12px;font-family:monospace;font-size:14px; }}
  button {{ background:#1d4ed8;color:#fff;border:none;padding:10px 22px;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;margin-top:12px; }}
  select {{ width:100%;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:10px 12px;font-size:14px; }}
  .panel {{ background:#1e293b;border-radius:8px;padding:16px 20px;margin-bottom:18px; }}
  .lbl {{ font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px; }}
  a {{ color:#60a5fa; }}
</style></head>
<body>
  <div style="margin-bottom:16px;"><a href="/">← Back to dashboard</a></div>
  <h1>📌 Teams watch list</h1>
  <div class="sub">When a A320/737-family aircraft matches the watch list (lands at/departs from a watched airport, OR its tail number matches a watched tail), the team gets a Teams card with a link back here.</div>

  <div class="panel">
    <div class="lbl">Currently watching — airports ({len(cur_airports)})</div>
    {airports_html}
  </div>

  <div class="panel">
    <div class="lbl">Edit airports</div>
    <form method="post" action="/watch">
      <textarea name="codes" placeholder="KAPS, KEGE, KSUN, KJAC, KASE, KSZT">{', '.join(cur_airports)}</textarea>
      <div style="font-size:11px;color:#94a3b8;margin-top:6px;">ICAO codes, comma or space separated. Case insensitive.</div>
      <button type="submit">Save airports</button>
    </form>
  </div>

  <div class="panel">
    <div class="lbl">Currently watching — tails ({len(cur_tails)})</div>
    {tails_html}
    <div style="font-size:11px;color:#94a3b8;margin-top:10px;">Tip: you can add prospect tails to this list directly from the home dashboard — checkbox each tail in the <em>Top Prospects</em> panel and click <em>Save selected to Teams watch list</em>.</div>
  </div>

  <div class="panel">
    <div class="lbl">Edit tails</div>
    <form method="post" action="/watch/tails">
      <textarea name="tails" placeholder="N123AB, N456CD, N789EF">{', '.join(cur_tails)}</textarea>
      <div style="font-size:11px;color:#94a3b8;margin-top:6px;">N-numbers, comma or space separated. Submitting replaces the entire tail list.</div>
      <button type="submit">Save tails</button>
    </form>
  </div>

  <div class="panel">
    <div class="lbl">Card frequency</div>
    <div style="font-size:13px;color:#cbd5e1;margin-bottom:10px;line-height:1.6;">
      Limit how often a card is posted for the <strong>same tail</strong>. Distinct tails always notify;
      this only throttles repeat cards for an aircraft you've already been alerted about.
    </div>
    <form method="post" action="/watch/frequency">
      <select name="min_interval_minutes">{freq_options_html}</select>
      <div style="font-size:11px;color:#94a3b8;margin-top:6px;">Currently: {notify_settings.interval_label(cur_interval)}</div>
      <button type="submit">Save frequency</button>
    </form>
  </div>

  <div class="panel">
    <div class="lbl">Teams webhook</div>
    <div style="margin-bottom:8px;">Status: <strong style="color:{teams_color};">{teams_state}</strong></div>
    <div style="font-size:11px;color:#94a3b8;line-height:1.6;">
      One-time setup in Teams: open the target chat/channel → <code>⋯</code> → <strong>Workflows</strong> → search <code>webhook</code> →
      pick <em>"Send webhook alerts to a chat"</em> (group/1:1 chats) or <em>"Send webhook alerts to a channel"</em> (Teams channels) → finish the flow.<br>
      Then push the generated URL with:<br>
      <code style="display:inline-block;margin-top:4px;">POST /admin/set-env  {{"key":"TEAMS_WEBHOOK_URL","value":"&lt;url&gt;"}}</code><br>
      …followed by <code>POST /webhook/restart</code> so the container picks up the new value.
    </div>
  </div>

  <div style="display:flex;gap:24px;"><a href="/">← Back to dashboard</a><a href="/activity">📊 Team activity report</a></div>
</body></html>"""


@app.post("/watch")
def watch_save():
    """Save (replace) the airport watch list."""
    raw = request.form.get("codes", "") or ""
    import re
    codes = re.split(r"[\s,;]+", raw.strip())
    watch_airports.set_watch_list(codes)
    from flask import redirect
    return redirect("/watch", code=303)


@app.post("/watch/tails")
def watch_tails_save():
    """Save (replace) the tail watch list."""
    raw = request.form.get("tails", "") or ""
    import re
    tails = re.split(r"[\s,;]+", raw.strip())
    watch_tails.set_watch_list(tails)
    from flask import redirect
    return redirect("/watch", code=303)


@app.post("/watch/tails/add")
def watch_tails_add():
    """Add the selected tails (multi-select from prospects panel).

    Redirects to /watch on success so the user gets immediate visible
    confirmation that the tails landed in the list.
    """
    selected = request.form.getlist("tails")
    if selected:
        watch_tails.add_tails(selected)
    from flask import redirect
    return redirect("/watch", code=303)


@app.post("/watch/frequency")
def watch_frequency_save():
    """Save the per-tail Teams card cooldown (minutes)."""
    raw = request.form.get("min_interval_minutes", "0") or "0"
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = 0
    notify_settings.set_min_interval_minutes(minutes)
    from flask import redirect
    return redirect("/watch", code=303)


@app.get("/activity")
def activity_report():
    """
    Team activity report — same content as the 4pm daily digest card,
    viewable in a browser at any time. Gated to identified team members
    (same visibility model as the Danny DM digest).
    """
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if not user:
        from flask import redirect
        return redirect("/", code=303)

    digest = usage_tracker.get_daily_digest(tz_name="America/Los_Angeles")

    # Per-user cards
    if digest["users"]:
        rows_html = ""
        for u in digest["users"]:
            top = ", ".join(f"{p} ({n})" for p, n in u["top_pages"]) or "—"
            rows_html += (
                f'<tr>'
                f'<td style="padding:10px 12px;font-weight:600;color:#e2e8f0;">{u["name"]}</td>'
                f'<td style="padding:10px 12px;text-align:right;color:#60a5fa;font-variant-numeric:tabular-nums;">{u["sessions"]}</td>'
                f'<td style="padding:10px 12px;text-align:right;color:#60a5fa;font-variant-numeric:tabular-nums;">{u["total_minutes"]}m</td>'
                f'<td style="padding:10px 12px;text-align:right;color:#60a5fa;font-variant-numeric:tabular-nums;">{u["pages_viewed"]}</td>'
                f'<td style="padding:10px 12px;color:#94a3b8;font-variant-numeric:tabular-nums;">{u["last_seen"]}</td>'
                f'<td style="padding:10px 12px;color:#94a3b8;font-size:12px;">{top}</td>'
                f'</tr>'
            )
        users_block = f"""
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead>
            <tr style="border-bottom:1px solid #334155;">
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">User</th>
              <th style="padding:10px 12px;text-align:right;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Sessions</th>
              <th style="padding:10px 12px;text-align:right;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Active</th>
              <th style="padding:10px 12px;text-align:right;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Views</th>
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Last seen</th>
              <th style="padding:10px 12px;text-align:left;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Top pages</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        """
    else:
        users_block = '<div style="color:#94a3b8;padding:10px 0;">No one has loaded the dashboard yet today.</div>'

    # No-shows
    if digest["no_shows"]:
        chips = ""
        for n in digest["no_shows"]:
            chips += (
                f'<span style="background:#3f2a08;color:#fbbf24;padding:4px 10px;'
                f'border-radius:4px;margin:0 6px 6px 0;display:inline-block;font-size:13px;">'
                f'{n}</span>'
            )
        no_shows_block = chips
    else:
        no_shows_block = '<span style="color:#22c55e;">Full team was active today. 🎯</span>'

    active_count = len(digest["users"])
    team_size    = digest["team_size"]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Team activity — A320/737 Sightings</title>
<meta http-equiv="refresh" content="60">
<style>
  body {{ background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif;padding:32px;max-width:1100px;margin:0 auto; }}
  h1   {{ font-size:22px;margin-bottom:6px; }}
  .sub {{ color:#94a3b8;font-size:13px;margin-bottom:24px; }}
  .panel {{ background:#1e293b;border-radius:8px;padding:16px 20px;margin-bottom:18px; }}
  .lbl {{ font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px; }}
  a {{ color:#60a5fa; }}
  .kpi {{ display:inline-block;margin-right:24px; }}
  .kpi .n {{ font-size:24px;font-weight:700;color:#e2e8f0;font-variant-numeric:tabular-nums; }}
  .kpi .l {{ font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px; }}
</style></head>
<body>
  <div style="margin-bottom:16px;"><a href="/">← Back to dashboard</a></div>
  <h1>📊 Team activity — {digest["date"]}</h1>
  <div class="sub">Same content as the 4pm daily digest card (posted to Danny's DM). Auto-refreshes every 60s. Local time zone: America/Los_Angeles.</div>

  <div class="panel">
    <div class="kpi"><div class="n">{active_count}</div><div class="l">Active today</div></div>
    <div class="kpi"><div class="n">{team_size}</div><div class="l">Team size</div></div>
    <div class="kpi"><div class="n">{len(digest["no_shows"])}</div><div class="l">No-shows</div></div>
  </div>

  <div class="panel">
    <div class="lbl">Active users (sorted by minutes)</div>
    {users_block}
  </div>

  <div class="panel">
    <div class="lbl">Not seen today</div>
    {no_shows_block}
  </div>

  <div style="color:#64748b;font-size:11px;margin-top:24px;">
    Sessions = page loads grouped with &lt; {usage_tracker.SESSION_GAP_MINUTES}-minute gaps.
    Active minutes = sum of session durations (single-view session counts as 1m).
    You are logged in as <strong style="color:#e2e8f0;">{user}</strong>.
  </div>
</body></html>"""


@app.post("/admin/test-openai")
def admin_test_openai():
    """
    Minimal direct OpenAI call (no tools, no custom system prompt) — isolates
    whether the key/model itself works vs. my chat_once code. Returns the full
    error JSON body on failure. Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.OPENAI_ENABLED:
        return jsonify({"error": "OPENAI_API_KEY not set"}), 400
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model    = config.OPENAI_MODEL,
            messages = [{"role": "user", "content": "say 'pong'"}],
        )
        return jsonify({
            "ok":     True,
            "model":  config.OPENAI_MODEL,
            "reply":  resp.choices[0].message.content,
            "usage":  resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else str(resp.usage),
        }), 200
    except Exception as e:   # noqa: BLE001
        detail = {
            "type":    type(e).__name__,
            "str":     str(e),
            "body":    getattr(e, "body", None),
            "code":    getattr(e, "code", None),
            "status":  getattr(e, "status_code", None),
            "model_attempted": config.OPENAI_MODEL,
            "key_prefix":      (config.OPENAI_API_KEY or "")[:7] + "...",
        }
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail["response_text"] = resp.text[:2000]
            except Exception:
                detail["response_text"] = None
        return jsonify(detail), 500


@app.post("/admin/pip-install")
def admin_pip_install():
    import os, subprocess
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    try:
        # Ensure git is configured with the GitHub token so pip can fetch
        # any `git+https://github.com/...` requirements from private repos.
        token = os.getenv("GITHUB_TOKEN", "")
        if token:
            subprocess.run(
                ["git", "config", "--global",
                 f"url.https://{token}@github.com/.insteadOf",
                 "https://github.com/"],
                capture_output=True,
            )
        res = subprocess.run(
            ["pip", "install", "--no-cache-dir", "-r", "/app/requirements.txt"],
            capture_output=True, text=True, timeout=180,
        )
        return jsonify({
            "ok":     res.returncode == 0,
            "rc":     res.returncode,
            "stdout": res.stdout[-3000:],
            "stderr": res.stderr[-3000:],
            "note":   "POST /webhook/restart to pick up the new packages",
        }), 200 if res.returncode == 0 else 500
    except Exception as e:   # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/admin/set-env")
def admin_set_env():
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    key   = (body.get("key") or "").strip().upper()
    value = body.get("value") or ""

    ALLOWED = {"TEAMS_WEBHOOK_URL", "TEAMS_DIGEST_WEBHOOK_URL",
               "OPENAI_API_KEY", "XAI_API_KEY",
               "ANTHROPIC_API_KEY", "DASHBOARD_URL", "OPENAI_MODEL",
               "SMTP_PASSWORD", "SMTP_USERNAME", "EMAIL_FROM", "EMAIL_TO",
               "HOURLY_SUMMARY_ENABLED", "HOURLY_SUMMARY_TZ",
               "SUMMARY_HOURS",
               "TRACK_ADJACENT_TYPES",
               "JETNET_USERNAME", "JETNET_PASSWORD",
               "JETNET_API_KEY", "JETNET_BASE_URL"}
    if key not in ALLOWED:
        return jsonify({"error": f"key not in allow-list", "allowed": sorted(ALLOWED)}), 400
    # Empty value is allowed (used to blank out a credential like SMTP_PASSWORD).

    env_path = Path("/app/.env")
    if not env_path.exists():
        env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return jsonify({"error": f".env file not found at {env_path}"}), 500

    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    out_lines = []
    for line in lines:
        # Skip comments and blanks for matching, but preserve them
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip().upper()
            if k == key:
                out_lines.append(f"{key}={value}")
                found = True
                continue
        out_lines.append(line)
    if not found:
        out_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    return jsonify({
        "ok":      True,
        "key":     key,
        "path":    str(env_path),
        "action":  "replaced" if found else "appended",
        "note":    "POST /webhook/restart to pick up the new value",
    }), 200


@app.post("/admin/test-teams")
def test_teams():
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    ok = teams_notifier.send_test()
    return jsonify({"sent": ok}), 200 if ok else 502


@app.post("/admin/test-teams-sighting")
def test_teams_sighting():
    """
    Fire the REAL notify_sighting() code path with a synthetic sighting,
    exercising both matched_airports and matched_tail kwargs.
    Use this to verify landing-alert plumbing end-to-end.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    fake = {
        "tail_number":   "N0TEST",
        "ac_type":       "C25M",
        "ac_subvariant": "M2",
        "origin_icao":   "KSZT",
        "dest_icao":     "KGEG",
        "operator":      "(synthetic — admin test)",
        "distance_nm":   312,
        "arrived_utc":   "2026-06-26T15:55:00Z",
        "tracking_url":  "https://a320737sightings.voloaltro.tech/",
    }
    try:
        ok = teams_notifier.notify_sighting(
            fake,
            matched_airports=["KSZT", "KGEG"],
            matched_tail="N0TEST",
        )
        return jsonify({"sent": ok}), 200 if ok else 502
    except Exception as e:   # noqa: BLE001
        return jsonify({"sent": False, "error": repr(e)}), 500


@app.post("/admin/test-daily-digest")
def test_daily_digest():
    """
    Fire the daily usage digest card on demand (ignores time-of-day).
    Useful for previewing the card format without waiting for 4 PM.
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    digest = usage_tracker.get_daily_digest(tz_name="America/Los_Angeles")
    ok = teams_notifier.notify_daily_digest(digest)
    return jsonify({
        "sent":      ok,
        "date":      digest["date"],
        "active":    len(digest["users"]),
        "no_shows":  digest["no_shows"],
    }), 200 if ok else 502


@app.post("/admin/test-jetnet")
def test_jetnet():
    """
    Diagnostic: verify JETNET creds + a single-tail lookup end-to-end.
    Body (optional): {"nnumber": "N525XX"} — defaults to the top-scored
    prospect if omitted. Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    from sources import jetnet
    diag = jetnet.check_credentials()
    if not diag.get("ok"):
        return jsonify({"stage": "credentials", **diag}), 400
    body = request.get_json(silent=True) or {}
    nn   = (body.get("nnumber") or "").strip().upper()
    if not nn:
        top = database.get_prospects(days=30)[:1]
        nn  = top[0]["tail_number"] if top else ""
    if not nn:
        return jsonify({"stage": "input", "error": "no nnumber and no scored prospects to fall back on"}), 400
    aircraft = jetnet.lookup_aircraft(nn)
    owner    = jetnet.lookup_owner(nn)
    history  = jetnet.lookup_history(nn, days=1825)   # last ~5 yr of transactions
    return jsonify({
        "stage":       "lookup",
        "credentials": diag,
        "nnumber":     nn,
        "aircraft":    aircraft,
        "owner":       owner,
        "history":     history,
    }), 200


@app.post("/admin/refresh-jetnet-owner")
def refresh_jetnet_owner():
    """
    Force a live JETNET refresh + cache upsert for one tail. Body:
    {"nnumber": "N525AB"}. Returns the fresh cached row (or an error).
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    nn   = (body.get("nnumber") or "").strip().upper()
    if not nn:
        return jsonify({"error": "nnumber required"}), 400
    import jetnet_enrichment as _je
    fresh = _je.fetch_and_store_owner(nn)
    if not fresh:
        return jsonify({"nnumber": nn, "ok": False,
                        "reason": "JETNET disabled or tail not found"}), 200
    return jsonify({"nnumber": nn, "ok": True, "owner": fresh}), 200


@app.post("/admin/run-jetnet-sweep")
def run_jetnet_sweep():
    """
    Trigger the JETNET ownership sweep on demand and fire Teams cards for
    any diffs found. Body (optional): {"days": 90, "limit": 500}.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body  = request.get_json(silent=True) or {}
    days  = int(body.get("days",  90))
    limit = int(body.get("limit", 500))
    import jetnet_enrichment as _je
    summary = _je.sweep_active_tails(days=days, limit=limit)
    fired   = 0
    if config.TEAMS_ENABLED and summary.get("diffs"):
        import teams_notifier as _tn
        for diff in summary["diffs"]:
            tail = diff.get("nnumber") or ""
            try:
                recent = []
                if tail:
                    dossier = database.get_tail_detail(tail, days=90)
                    if dossier and dossier.get("flights"):
                        recent = dossier["flights"][:3]
                if _tn.notify_ownership_change(diff, recent_flights=recent):
                    fired += 1
            except Exception:                            # noqa: BLE001
                pass
    return jsonify({
        "checked":    summary.get("checked", 0),
        "changed":    summary.get("changed", 0),
        "cards_sent": fired,
        "elapsed_s":  summary.get("elapsed_s", 0),
        "diffs":      [{"nnumber": d.get("nnumber"),
                        "before":  d.get("before"),
                        "after":   d.get("after")}
                       for d in summary.get("diffs", [])],
    }), 200


@app.post("/admin/enrich-all-owners")
def enrich_all_owners():
    """
    Populate the JETNET owner cache for EVERY tracked tail, in a background
    thread (returns immediately). Body (optional): {"days":3650,"limit":100000,
    "skip_cached":false}. Poll GET /admin/enrich-status for progress.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    days = int(body.get("days", 3650))
    limit = int(body.get("limit", 100_000))
    skip_cached = bool(body.get("skip_cached", False))
    import jetnet_enrichment as _je
    return jsonify(_je.start_bulk_enrich(
        days=days, limit=limit, skip_cached=skip_cached)), 200


@app.get("/admin/enrich-status")
def enrich_status():
    """Progress of the running/last bulk owner-enrichment job."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    import jetnet_enrichment as _je
    return jsonify(_je.bulk_enrich_status()), 200


@app.post("/admin/test-morning-prospects")
def test_morning_prospects():
    """
    Fire the morning top-5 prospects card on demand (ignores time-of-day).
    Useful for previewing the card without waiting for 7 AM PT.
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    top = database.get_prospects(days=30)[:5]
    ok  = teams_notifier.notify_morning_prospects(top, top_n=5)
    return jsonify({
        "sent":  ok,
        "count": len(top),
        "tails": [p["tail_number"] for p in top],
    }), 200 if ok else 502


@app.post("/admin/test-daily-flight-plan")
def test_daily_flight_plan():
    """
    Fire the 8 AM Daily Flight Plan card on demand (ignores time-of-day).
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    plan = sales_plan.build_plan(days=30, limit=6)
    ok   = teams_notifier.notify_daily_flight_plan(plan, top_n=5)
    return jsonify({
        "sent":        ok,
        "call_list":   [p["tail_number"] for p in plan["call_list"]],
        "clusters":    [c["icao"] for c in plan["clusters"]],
        "goal":        plan["goal"],
        "rule_counts": plan["rule_counts"],
    }), 200 if ok else 502


@app.get("/admin/region-counts")
def admin_region_counts():
    """
    Diagnostic: rows per sales-region bucket + a sample of the most-seen
    non-NA airports. Watch this to confirm EU/UK data is flowing in.
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    with database._connect() as conn:
        totals = dict(conn.execute(
            "SELECT COALESCE(region, 'NULL') AS r, COUNT(*) AS n "
            "FROM sightings GROUP BY r"
        ).fetchall())
        eu_recent = [dict(r) for r in conn.execute(
            "SELECT tail_number, ac_type, ac_subvariant, origin_icao, dest_icao, "
            "       arrived_utc, source "
            "FROM sightings WHERE region='EU_UK' "
            "ORDER BY arrived_utc DESC LIMIT 20"
        ).fetchall()]
        top_eu_airports = [dict(r) for r in conn.execute(
            "SELECT dest_icao AS icao, COUNT(*) AS n FROM sightings "
            "WHERE region='EU_UK' AND dest_icao != '' "
            "GROUP BY dest_icao ORDER BY n DESC LIMIT 15"
        ).fetchall()]
    return jsonify({
        "totals":            totals,
        "eu_recent":         eu_recent,
        "top_eu_dest_icaos": top_eu_airports,
    })


@app.post("/admin/backfill-eu-climb")
def admin_backfill_eu_climb():
    """
    Kick the EU climb-profile backfill. Clears `track_fetched_at` on recent
    adsb.lol-sourced EU_UK rows that never got climb metrics, so the periodic
    `track_fetcher.backfill_pending` loop will retry them — resolving
    `fa_flight_id` via FA enrichment and then fetching `/flights/{id}/track`
    for the altitude profile.

    Body: {"days": 14, "limit": 500}   (both optional)
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    try:
        days  = int(body.get("days", 14))
        limit = int(body.get("limit", 500))
    except (TypeError, ValueError):
        return jsonify({"error": "days and limit must be integers"}), 400
    reset = database.reset_eu_track_backfill(days=days, limit=limit)
    return jsonify({
        "reset":            reset,
        "days":             days,
        "limit":            limit,
        "note":             "Rows will be re-processed by the climb backfill loop at ~1 row / 1.5s (~40/min). Watch top_altitude_ft populate on /eu.",
    })


@app.post("/admin/create-guest-link")
def admin_create_guest_link():
    """
    Mint a time-boxed guest link for an external visitor.

    Body: {"minutes": 30, "label": "Bob Smith / XYZ Aerospace"}
    Returns: {"url": "...", "expires_at": "...", "label": "..."}

    The URL sets a private cookie that:
      - Bypasses the "Who are you?" identify modal.
      - Suppresses page-view logging to the team-visible digest.
      - Records access to a PRIVATE log (viewable via /admin/guest-log).

    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    try:
        minutes = int(body.get("minutes", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "minutes must be an integer"}), 400
    label = str(body.get("label", "")).strip()
    row = guest_access.create_token(minutes=minutes, label=label)
    # Force https:// — Flask sees http:// because Caddy terminates TLS upstream.
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    base = f"{scheme}://{request.host}"
    return jsonify({
        "url":              f"{base}/guest/{row['token']}",
        "link_expires_at":  row["link_expires_at"],
        "duration_minutes": row["duration_minutes"],
        "label":            row["label"],
        "note":             "Session clock (duration_minutes) starts the moment the guest first clicks the URL. Until then the link stays valid up to link_expires_at.",
    })


@app.get("/admin/guest-log")
def admin_guest_log():
    """
    Return active guest tokens + recent guest access log.
    Protected by DEPLOY_SECRET (either header or ?secret= query).
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "active_tokens": guest_access.list_active_tokens(),
        "recent_access": guest_access.list_recent_access(200),
    })


@app.post("/admin/revoke-guest-link")
def admin_revoke_guest_link():
    """Immediately expire a specific guest token. Body: {"token": "..."}."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    token = str(body.get("token", "")).strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    removed = guest_access.revoke_token(token)
    return jsonify({"revoked": removed})


@app.get("/guest/<token>")
def guest_activate(token):
    """
    Guest link entry point. Validates the token, starts the session clock
    on first click (if not already started), sets a cookie whose max-age
    matches the remaining session lifetime, then redirects to the home
    dashboard.
    """
    from flask import redirect, make_response
    row = guest_access.get_token(token)
    if not row:
        return (
            "<html><body style='background:#0f172a;color:#e2e8f0;"
            "font-family:Arial,sans-serif;padding:64px;text-align:center;'>"
            "<h1>Link expired</h1>"
            "<p>This guest link is no longer valid.</p>"
            "</body></html>",
            410,
        )
    # Start the session clock on first click (no-op if already activated).
    activated = guest_access.activate_token(token) or row
    try:
        expires = datetime.fromisoformat(activated["effective_expires_at"])
        max_age_s = max(1, int((expires - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        max_age_s = 60
    resp = make_response(redirect("/", code=303))
    # httponly=True so client-side JS on the dashboard can't leak the token.
    resp.set_cookie(_GUEST_COOKIE, token, max_age=max_age_s,
                    samesite="Lax", httponly=True)
    try:
        guest_access.log_access(
            token, activated.get("label", ""), "/guest/activate",
            remote_addr=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception:
        pass
    return resp


@app.post("/admin/test-hourly-summary")
def test_hourly_summary():
    """
    Fire the hourly recap card on demand (ignores time-of-day window).
    Useful to preview formatting after a change without waiting for the top
    of the next hour. Protected by DEPLOY_SECRET.
    Query: ?hours=N (default 1.0)
    """
    import os
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except Exception:                                   # noqa: BLE001
        ZoneInfo = None  # type: ignore
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not config.TEAMS_ENABLED:
        return jsonify({"error": "TEAMS_WEBHOOK_URL not set"}), 400
    try:
        hours = float(request.args.get("hours", "1"))
    except ValueError:
        hours = 1.0
    summary = database.get_hourly_summary(hours=hours)
    label = ""
    if ZoneInfo is not None:
        try:
            local_now = datetime.now(ZoneInfo(config.HOURLY_SUMMARY_TZ))
            try:
                label = local_now.strftime("%-I:00 %p %Z")
            except ValueError:
                label = local_now.strftime("%I:00 %p %Z").lstrip("0")
            label += "  (manual test)"
        except Exception:                               # noqa: BLE001
            pass
    ok = teams_notifier.notify_hourly_summary(summary, local_label=label)
    return jsonify({"sent": ok, "count_window": summary["count_window"],
                    "count_today": summary["count_today"]}), 200 if ok else 502


@app.get("/admin/atlas-counts")
def atlas_counts():
    """
    Diagnostic: per-subvariant counts of sightings grouped by is_tamarack_fleet
    flag (1 = active ATLAS, 2 = removed, NULL = flat-wing/unknown).
    Helps answer "why does CJ2 show no ATLAS data?"
    """
    import os, sqlite3
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(ac_subvariant, ''), '(unknown)') AS sv,
                   ac_type,
                   COALESCE(is_tamarack_fleet, -1) AS flag,
                   COUNT(*) AS n,
                   COUNT(DISTINCT tail_number) AS tails
            FROM sightings
            WHERE distance_nm > 50
            GROUP BY sv, ac_type, flag
            ORDER BY sv, flag
            """
        ).fetchall()
    summary: dict[str, dict] = {}
    for r in rows:
        sv = r["sv"]
        s = summary.setdefault(sv, {"ac_type": r["ac_type"], "atlas": 0, "atlas_tails": 0,
                                    "removed": 0, "flatwing": 0, "flatwing_tails": 0})
        if r["flag"] == 1:
            s["atlas"]          = r["n"]
            s["atlas_tails"]    = r["tails"]
        elif r["flag"] == 2:
            s["removed"]        = r["n"]
        else:
            s["flatwing"]       += r["n"]
            s["flatwing_tails"] += r["tails"]
    return jsonify(summary), 200


@app.get("/admin/atlas-trace")
def atlas_trace():
    """
    Diagnostic: for each S/N in FLEET_ACTIVE_SUBVARIANT (by selected
    sub-variant), walk through FAA registry → sightings DB and report which
    step is failing. Use ?subvariant=CJ2 (or CJ3, CJ3PLUS, etc.).
    """
    import os, sqlite3
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    target_sv = (request.args.get("subvariant") or "CJ2").upper()
    import tamarack_fleet as _tf
    _tf.load_faa_registry()
    sns = [sn for sn, sv in _tf.FLEET_ACTIVE_SUBVARIANT.items() if sv == target_sv]
    out = {"subvariant": target_sv, "fleet_count": len(sns),
           "no_faa_match": [], "no_sightings": [], "matched": [], "mismatched": []}
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for sn in sns:
            nnum = _tf._sn_to_nnum.get(sn) or _tf.sn_to_nnum_lookup(sn)
            if not nnum:
                out["no_faa_match"].append(sn)
                continue
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(is_tamarack_fleet) AS flag "
                "FROM sightings WHERE UPPER(tail_number)=?",
                (nnum.upper(),),
            ).fetchone()
            n = row["n"]
            flag = row["flag"]
            entry = {"sn": sn, "tail": nnum, "sightings": n, "is_tamarack_fleet": flag}
            if n == 0:
                out["no_sightings"].append(entry)
            elif flag != 1:
                out["mismatched"].append(entry)
            else:
                out["matched"].append(entry)
    return jsonify({
        "subvariant":   target_sv,
        "fleet_count":  out["fleet_count"],
        "no_faa_match": len(out["no_faa_match"]),
        "no_sightings": len(out["no_sightings"]),
        "matched":      len(out["matched"]),
        "mismatched":   len(out["mismatched"]),
        "samples":      {
            "no_faa_match_sns":   out["no_faa_match"][:10],
            "no_sightings_pairs": out["no_sightings"][:10],
            "matched_pairs":      out["matched"][:5],
            "mismatched_pairs":   out["mismatched"][:10],
        },
    }), 200


# Module-level state for the async ICA recompute job
_ica_state: dict = {"status": "idle", "started_at": None, "finished_at": None,
                    "nulled": 0, "summary": None}


@app.post("/admin/recompute-ica")
def recompute_ica():
    """
    Re-process initial-cruise-altitude (ICA) on the N most-recent FlightAware
    sightings that currently have ICA set. Use after changing the ICA
    detection algorithm so historical numbers reflect the new definition.
    Spawns a background thread; poll /admin/recompute-ica-status.
    """
    import os, threading
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    if _ica_state["status"] == "running":
        return jsonify({"error": "already running",
                        "started_at": _ica_state["started_at"]}), 409
    try:
        n = int(request.args.get("n", "500"))
    except ValueError:
        n = 500
    n = max(1, min(n, 5000))

    # No nulling needed — force-mode now uses `sustained_top_alt_ft IS NULL`
    # as the trigger, which is set on every successful pass. Chained batches
    # walk strictly forward through the dataset.
    _ica_state.update({
        "status":       "running",
        "started_at":   datetime.now(timezone.utc).isoformat(),
        "finished_at":  None,
        "nulled":       0,
        "summary":      None,
    })

    def _worker(target_n: int):
        try:
            import track_fetcher
            summary = track_fetcher.backfill_pending(force=True, limit=target_n)
            _ica_state["summary"] = summary
            _ica_state["status"]  = "done"
        except Exception as e:                          # noqa: BLE001
            _ica_state["summary"] = {"error": str(e)}
            _ica_state["status"]  = "error"
        finally:
            _ica_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_worker, args=(n,), daemon=True).start()
    return jsonify({"status": "started", "limit": n,
                    "poll": "/admin/recompute-ica-status"}), 202


@app.get("/admin/recompute-ica-status")
def recompute_ica_status():
    """Poll the ICA recompute job. Protected by DEPLOY_SECRET."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_ica_state), 200


@app.get("/admin/ica-coverage")
def ica_coverage():
    """
    Diagnostic: per-sub-variant counts of sightings with vs without ICA
    populated, split by distance bucket. Helps explain why the climb chart
    might be sparse.
    """
    import os, sqlite3
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        totals = conn.execute(
            """
            SELECT
              COUNT(*) AS total_fa,
              SUM(CASE WHEN top_altitude_ft IS NOT NULL THEN 1 ELSE 0 END)            AS has_top_alt,
              SUM(CASE WHEN initial_cruise_alt_ft IS NOT NULL THEN 1 ELSE 0 END)      AS has_ica,
              SUM(CASE WHEN distance_nm >= 500 AND initial_cruise_alt_ft IS NOT NULL
                       THEN 1 ELSE 0 END)                                              AS has_ica_long,
              SUM(CASE WHEN distance_nm >= 200 AND initial_cruise_alt_ft IS NOT NULL
                       THEN 1 ELSE 0 END)                                              AS has_ica_medium
            FROM sightings WHERE source='flightaware'
            """
        ).fetchone()
        per_sv = conn.execute(
            """
            SELECT UPPER(COALESCE(ac_subvariant,'')) AS sv,
                   COUNT(*) AS n,
                   SUM(CASE WHEN initial_cruise_alt_ft IS NOT NULL THEN 1 ELSE 0 END) AS with_ica,
                   SUM(CASE WHEN distance_nm >= 500 AND initial_cruise_alt_ft IS NOT NULL
                            THEN 1 ELSE 0 END) AS with_ica_long
            FROM sightings WHERE source='flightaware' AND ac_subvariant != ''
            GROUP BY sv ORDER BY sv
            """
        ).fetchall()
    return jsonify({
        "totals":         dict(totals) if totals else {},
        "per_subvariant": [dict(r) for r in per_sv],
    }), 200


@app.post("/admin/diag-poll")
def diag_poll():
    """
    Run each enabled source's fetch_landings() synchronously with a wall-clock
    budget per source. Useful when the background poller appears hung
    (last_poll_utc stays empty). Returns timing + count + error for each source.
    Protected by DEPLOY_SECRET.
    """
    import os, threading, time as _time
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    try:
        lookback_min = int(request.args.get("minutes", config.LOOKBACK_MINUTES))
    except ValueError:
        lookback_min = config.LOOKBACK_MINUTES
    budget_s = float(request.args.get("budget_s", "30"))

    from sources import adsbexchange as _ax, flightaware as _fa, opensky as _os
    targets = [
        ("adsbexchange", config.ADSBEXCHANGE_ACTIVE, _ax.fetch_landings),
        ("flightaware",  config.FLIGHTAWARE_ACTIVE,  _fa.fetch_landings),
        ("opensky",      config.OPENSKY_ACTIVE,      _os.fetch_landings),
    ]
    report = []
    for name, active, fn in targets:
        if not active:
            report.append({"source": name, "active": False, "skipped": True})
            continue
        result: dict = {"source": name, "active": True}
        start = _time.monotonic()
        out: dict = {}
        def _runner(_out=out, _fn=fn, _lb=lookback_min):
            try:
                rows = list(_fn(_lb))
                _out["count"] = len(rows)
            except Exception as e:                       # noqa: BLE001
                _out["error"] = f"{type(e).__name__}: {e}"
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=budget_s)
        elapsed = round(_time.monotonic() - start, 2)
        result["elapsed_s"] = elapsed
        if t.is_alive():
            result["hung"] = True
            result["note"] = f"did not return within {budget_s}s budget"
        else:
            result["hung"] = False
            result.update(out)
        report.append(result)

    return jsonify({"lookback_min": lookback_min,
                    "budget_s":     budget_s,
                    "sources":      report}), 200


@app.post("/admin/run-poll")
def run_poll():
    """
    Run main._poll() synchronously in the request thread. If the background
    poller is hung (last_poll_utc stays empty), this surfaces the actual
    exception with a full traceback so we can fix the root cause.
    Protected by DEPLOY_SECRET.
    """
    import os, traceback as _tb
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    try:
        import main as _main
        before = daemon_state.get("sightings_total", 0)
        _main._poll()
        after = daemon_state.get("sightings_total", before)
        return jsonify({
            "ok":             True,
            "sightings_added": max(0, after - before),
            "last_poll_utc":   daemon_state.get("last_poll_utc"),
        }), 200
    except Exception as e:                              # noqa: BLE001
        return jsonify({
            "ok":     False,
            "error":  f"{type(e).__name__}: {e}",
            "trace":  _tb.format_exc(),
        }), 500


@app.post("/admin/backfill-history")
def backfill_history_start():
    """
    Pull historical FlightAware landings going back N days (default 180).
    Runs as a background job; stops early if 5 consecutive days return zero
    results (likely past the FA history retention horizon).
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    try:
        days = int(request.args.get("days", 180))
    except ValueError:
        days = 180
    days = max(1, min(days, 730))   # cap at 2 years
    import backfill_history
    if not backfill_history.start(days):
        return jsonify({"error": "already running",
                        "state": backfill_history.state}), 409
    return jsonify({"status": "started", "days": days,
                    "poll": "/admin/backfill-history-status"}), 202


@app.get("/admin/backfill-history-status")
def backfill_history_status():
    """Poll status of the running/last historical backfill job."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    import backfill_history
    return jsonify(backfill_history.state), 200


@app.post("/admin/backfill-history-stop")
def backfill_history_stop():
    """Signal the running backfill to stop after the current day completes."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    import backfill_history
    backfill_history.stop()
    return jsonify({"status": "stop signal sent"}), 200


@app.post("/admin/backfill-climb")
def backfill_climb():
    """
    Manually kick off a batch of climb-profile backfills (50 sightings).
    The background worker normally does this every 10s, but this endpoint
    lets you trigger an immediate burst.
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    import track_fetcher
    summary = track_fetcher.backfill_pending(limit=limit, force=force)
    return jsonify(summary), 200


@app.post("/admin/delete-cj4")
def delete_cj4():
    """
    Permanently delete all CJ4 (C25C) sightings from the database.
    CJ4 is not in ATLAS scope so we don't want it polluting the dashboards.
    Protected by DEPLOY_SECRET.
    """
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    secret = request.headers.get("X-Deploy-Secret", "") or request.args.get("secret", "")
    if not expected or secret != expected:
        return jsonify({"error": "unauthorized"}), 401
    with database._connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE ac_type = 'C25C' "
            "OR ac_subvariant = 'CJ4'"
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM sightings WHERE ac_type = 'C25C' OR ac_subvariant = 'CJ4'"
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    return jsonify({"deleted": before, "remaining_total": after}), 200


@app.post("/admin/import-sightings")
def import_sightings():
    """Bulk-import sightings from another DB instance. Protected by DEPLOY_SECRET."""
    import os
    expected = os.getenv("DEPLOY_SECRET", "")
    if not expected or request.headers.get("X-Deploy-Secret", "") != expected:
        return jsonify({"error": "unauthorized"}), 401
    rows = request.get_json(force=True) or []
    inserted = 0
    first_error = None
    for r in rows:
        try:
            database.record_sighting(r)
            inserted += 1
        except Exception as e:
            if first_error is None:
                first_error = str(e)
    return jsonify({"imported": inserted, "total": len(rows), "first_error": first_error}), 200


def _fleet_comparison_html(cmp) -> str:
    """
    Render per-performance-group ATLAS fleet vs flat-wing comparison cards.

    `cmp` is a list of dicts from database.get_fleet_comparison_by_perfgroup().
    Five groups: CJ/CJ1, CJ1+/M2, CJ2, CJ2+, CJ3/CJ3+. CJ4 is excluded.
    """
    if not cmp:
        return ""

    cards_html = ""
    for d in cmp:
        atlas = d.get("atlas") or {}
        other = d.get("other") or {}
        adv_nm  = d.get("advantage_nm")
        adv_pct = d.get("advantage_pct")
        label   = d["label"]
        baseline = d["baseline_nm"]
        atlas_range = d["atlas_nm"]

        if not atlas.get("count") and not other.get("count"):
            continue

        # Advantage headline
        if adv_pct is not None and adv_nm is not None:
            sign = "+" if adv_nm >= 0 else ""
            adv_col = "#22c55e" if adv_nm > 0 else "#f87171"
            adv_headline = (
                f'<div style="font-size:22px;font-weight:800;color:{adv_col};">'
                f'{sign}{adv_pct}%</div>'
                f'<div style="font-size:10px;color:#94a3b8;line-height:1.4;">farther on long flights<br>'
                f'({sign}{adv_nm:,} nm avg)</div>'
            )
        else:
            adv_col = "#94a3b8"
            atlas_n      = atlas.get("count", 0) if atlas else 0
            atlas_long_n = atlas.get("long_count", 0) if atlas else 0
            other_long_n = other.get("long_count", 0) if other else 0
            if atlas_n == 0:
                msg = "No ATLAS sightings yet"
            elif atlas_long_n == 0 and other_long_n > 0:
                msg = (f'<strong style="color:#fbbf24;">{other_long_n}</strong> long flight(s) on flat-wing<br>'
                       f'<span style="color:#94a3b8;">ATLAS would nonstop them</span>')
            elif other_long_n == 0:
                msg = "Awaiting flat-wing long missions"
            else:
                msg = "Awaiting more long missions"
            adv_headline = f'<div style="font-size:11px;color:#94a3b8;line-height:1.4;">{msg}</div>'

        def _mini(label: str, stats: dict | None, col: str) -> str:
            if not stats:
                return ""
            long_avg = stats.get("long_avg")
            long_cnt = stats.get("long_count", 0)
            avg      = stats.get("avg", 0)
            n_total  = stats.get("count", 0)
            hot_pct  = stats.get("hot_pct", 0)
            block_kts = stats.get("block_kts")
            return (
                f'<div style="background:#0f172a;border-radius:5px;padding:8px 12px;flex:1;min-width:130px;">'
                f'<div style="font-size:9px;color:{col};text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">{label}</div>'
                f'<div style="font-size:16px;font-weight:700;color:#e2e8f0;">{avg:,} nm</div>'
                f'<div style="font-size:10px;color:#94a3b8;">avg of <strong style="color:#cbd5e1;">{n_total:,}</strong> flights</div>'
                + (f'<div style="font-size:13px;font-weight:600;color:{col};margin-top:3px;">{long_avg:,} nm</div>'
                   f'<div style="font-size:10px;color:#94a3b8;">avg of <strong style="color:#cbd5e1;">{long_cnt:,}</strong> long missions</div>'
                   if long_avg else "")
                + (f'<div style="font-size:10px;color:#f97316;margin-top:2px;">{hot_pct}% exceed baseline</div>'
                   if hot_pct else "")
                + (f'<div style="font-size:10px;color:#60a5fa;margin-top:2px;" '
                   f'title="Per-flight block speed (distance ÷ duration), then simple mean across the fleet">'
                   f'{block_kts} kts block</div>'
                   if block_kts else "")
                + '</div>'
            )

        atlas_mini = _mini("✈ ATLAS fleet", atlas, "#22c55e")
        other_mini = _mini("Flat-wing", other, "#94a3b8")

        # Range bar — visual
        bar_html = ""
        if adv_nm is not None and other.get("long_avg"):
            other_pct = 100
            atlas_pct = min(100 * atlas.get("long_avg", 0) / other["long_avg"], 140)
            bar_html = (
                f'<div style="margin-top:8px;">'
                f'<div style="font-size:9px;color:#64748b;margin-bottom:3px;">'
                f'LONG-MISSION AVG &nbsp;'
                f'<span style="color:#94a3b8;">(ATLAS n={atlas.get("long_count",0):,} · Flat-wing n={other.get("long_count",0):,})</span>'
                f'</div>'
                f'<div style="display:flex;align-items:center;gap:4px;">'
                f'<div style="font-size:9px;color:#94a3b8;width:58px;text-align:right;">Flat-wing</div>'
                f'<div style="background:#334155;border-radius:3px;height:8px;width:{int(other_pct * 1.2)}px;"></div>'
                f'<div style="font-size:9px;color:#94a3b8;">{other.get("long_avg","?"):,} nm</div>'
                f'</div>'
                f'<div style="display:flex;align-items:center;gap:4px;margin-top:2px;">'
                f'<div style="font-size:9px;color:#22c55e;width:58px;text-align:right;">ATLAS</div>'
                f'<div style="background:#16a34a;border-radius:3px;height:8px;width:{int(atlas_pct * 1.2)}px;"></div>'
                f'<div style="font-size:9px;color:#22c55e;">{atlas.get("long_avg","?"):,} nm</div>'
                f'</div></div>'
            )

        # Total sample size pill for the card header
        total_n = (atlas.get("count", 0) if atlas else 0) + (other.get("count", 0) if other else 0)
        sample_pill = (
            f'<div style="font-size:9px;color:#94a3b8;margin-top:3px;">'
            f'<span style="background:#0f172a;border:1px solid #334155;padding:1px 6px;'
            f'border-radius:3px;color:#cbd5e1;font-weight:600;">n = {total_n:,} flights</span>'
            f'</div>'
        )

        cards_html += (
            f'<div style="background:#1e293b;border-radius:8px;padding:14px 18px;'
            f'border-top:3px solid {adv_col};display:flex;flex-direction:column;">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:8px;">'
            f'<div style="min-width:0;">'
            f'<div style="font-size:14px;font-weight:700;color:#e2e8f0;">{label}</div>'
            f'<div style="font-size:10px;color:#64748b;" title="Real-world range at Max Continuous Thrust (Tamarack PM, 2026-06-26)">{baseline:,} nm baseline → {atlas_range:,} nm with ATLAS &nbsp;<span style="color:#475569;">(at MCT)</span></div>'
            f'{sample_pill}'
            f'</div>'
            f'<div style="text-align:right;flex-shrink:0;">{adv_headline}</div>'
            f'</div>'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;flex:1;align-items:stretch;">{atlas_mini}{other_mini}</div>'
            f'{bar_html}'
            f'</div>'
        )

    if not cards_html:
        return ""

    return (
        f'<div style="margin-bottom:20px;">'
        f'<div style="font-size:11px;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:1px;margin-bottom:10px;">'
        f'📡 ATLAS Fleet Performance — When a long flight is required, ATLAS operators fly farther'
        f'</div>'
        f'<div class="fleet-perf-grid" style="display:grid;'
        f'grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;align-items:stretch;">{cards_html}</div>'
        f'<style>'
        f'@media(max-width:1500px){{.fleet-perf-grid{{grid-template-columns:repeat(3,minmax(0,1fr));}}}}'
        f'@media(max-width:1000px){{.fleet-perf-grid{{grid-template-columns:repeat(2,minmax(0,1fr));}}}}'
        f'@media(max-width:640px){{.fleet-perf-grid{{grid-template-columns:1fr;}}}}'
        f'</style>'
        f'</div>'
    )


def _yaw_damper_panel_html() -> str:
    """
    Render M2 yaw-damper suspects (M2 tails consistently flying ≤ FL280 on
    missions > 300 nm — AFM limitation when yaw damper is inop).
    Returns "" when there are no qualifying tails.
    """
    suspects = database.get_m2_yaw_damper_suspects(days=90, min_distance_nm=300,
                                                   max_alt_ft=28000, min_low_flights=3)
    if not suspects:
        return ""
    rows_html = ""
    for s in suspects[:10]:
        ex_html = ""
        for ex in s["examples"]:
            date = (ex["arrived_utc"] or "")[:10]
            ex_html += (
                f'<div style="font-size:11px;color:#94a3b8;">'
                f'{date} · {ex["origin"]} → {ex["dest"]} · '
                f'<strong style="color:#e2e8f0;">{ex["distance_nm"]:,} nm</strong> '
                f'@ <strong style="color:#f97316;">FL{ex["top_alt_ft"]//100:03d}</strong>'
                f'</div>'
            )
        avg_fl = f"FL{s['avg_top_alt_ft']//100:03d}" if s["avg_top_alt_ft"] else "—"
        rows_html += (
            f'<div style="display:flex;gap:14px;padding:10px 0;border-bottom:1px solid #1e3a5f;align-items:flex-start;">'
            f'<div style="flex:0 0 110px;">'
            f'  <a href="/tail/{s["tail_number"]}" style="color:#60a5fa;font-weight:700;font-size:13px;text-decoration:none;">{s["tail_number"]}</a>'
            f'  <div style="font-size:10px;color:#94a3b8;">{s["operator"][:24]}</div>'
            f'</div>'
            f'<div style="flex:0 0 130px;font-size:11px;color:#e2e8f0;">'
            f'  <div><strong style="color:#f97316;">{s["low_alt_legs"]} of {s["total_long_legs"]}</strong> long legs ≤ FL280</div>'
            f'  <div style="color:#94a3b8;">avg top: {avg_fl} · <strong style="color:#fbbf24;">{s["pct_low"]}%</strong></div>'
            f'</div>'
            f'<div style="flex:1;min-width:0;">{ex_html}</div>'
            f'</div>'
        )
    return (
        f'<div style="background:#1e293b;border-radius:8px;padding:16px 20px;margin-bottom:20px;'
        f'border-left:3px solid #f97316;">'
        f'  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
        f'    <div style="font-size:13px;color:#e2e8f0;text-transform:uppercase;letter-spacing:1px;font-weight:700;">'
        f'      🛞 M2 Yaw-Damper Suspects'
        f'    </div>'
        f'    <div style="font-size:10px;color:#94a3b8;">{len(suspects)} tail(s) · last 90 days · long legs only (&gt;300 nm)</div>'
        f'  </div>'
        f'  <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;line-height:1.4;">'
        f'    Per the M2 AFM, an <strong style="color:#f97316;">inoperative yaw damper</strong> imposes a max-altitude '
        f'    limit of <strong>FL280</strong>. These tails are habitually capped at or below FL280 on missions &gt; 300 nm — '
        f'    strong signal of a deferred yaw-damper squawk and a sales-conversation opener.'
        f'  </div>'
        f'  {rows_html}'
        f'</div>'
    )


def _watch_pill_html() -> str:
    """Render the 'Watching: KAPS, KEGE, ... + N tails · manage' pill for the sticky header."""
    codes = watch_airports.get_watch_list()
    tails = watch_tails.get_watch_list()
    teams_dot = "#22c55e" if config.TEAMS_ENABLED else "#64748b"
    teams_tip = "Teams webhook configured" if config.TEAMS_ENABLED else "Teams webhook NOT configured"
    codes_html = (
        ", ".join(f'<span style="font-family:monospace;color:#60a5fa;font-weight:700;">{c}</span>'
                  for c in codes[:5])
        if codes else
        '<span style="color:#94a3b8;">no airports</span>'
    )
    extra = f' <span style="color:#94a3b8;">+{len(codes)-5}</span>' if len(codes) > 5 else ""
    tail_count = f' <span style="color:#fbbf24;font-weight:700;margin-left:6px;">+ {len(tails)} tails</span>' if tails else ""
    return (
        f'<a href="/watch" style="display:inline-flex;align-items:center;gap:10px;'
        f'background:#1e293b;color:#e2e8f0;padding:8px 14px;border-radius:6px;'
        f'font-size:12px;text-decoration:none;border:1px solid #1e3a5f;">'
        f'  <span title="{teams_tip}" style="display:inline-block;width:8px;height:8px;'
        f'border-radius:50%;background:{teams_dot};"></span>'
        f'  <span style="color:#94a3b8;text-transform:uppercase;letter-spacing:1px;font-size:10px;">'
        f'\U0001F4CC Watching</span>'
        f'  <span>{codes_html}{extra}{tail_count}</span>'
        f'  <span style="color:#60a5fa;">\u270f\ufe0f manage</span>'
        f'</a>'
    )


def _chat_widget_html() -> str:
    """Floating bottom-right chat widget powered by the /api/chat endpoint."""
    enabled = config.OPENAI_ENABLED
    teams   = config.TEAMS_ENABLED
    status_dot = "#22c55e" if enabled else "#64748b"
    status_msg = "Ready" if enabled else "OPENAI_API_KEY not set"
    examples_html = (
        "<strong>Examples:</strong><br>"
        "\u2022 <em>who owns N123AB and their phone number</em><br>"
        "\u2022 <em>tell me about N123AB</em><br>"
        "\u2022 <em>show recent A320 arrivals into Phoenix</em>"
    )
    return f"""
<style>
  #chat-fab {{ position:fixed; bottom:18px; right:18px; z-index:50;
              background:#1d4ed8; color:#fff; border:none; border-radius:999px;
              padding:12px 18px; font-size:14px; font-weight:700; cursor:pointer;
              box-shadow:0 4px 14px rgba(0,0,0,0.4); }}
  #chat-fab:hover {{ background:#2563eb; }}
  #chat-panel {{ position:fixed; bottom:80px; right:18px; z-index:51;
                 width:480px; max-width:calc(100vw - 36px);
                 height:620px; max-height:calc(100vh - 110px);
                 background:#0f172a; border:1px solid #1e3a5f; border-radius:10px;
                 box-shadow:0 8px 30px rgba(0,0,0,0.5); display:none;
                 flex-direction:column; overflow:hidden; }}
  #chat-panel.open {{ display:flex; }}
  #chat-head {{ background:#1e293b; padding:8px 12px; display:flex;
                justify-content:space-between; align-items:center; gap:8px;
                border-bottom:1px solid #1e3a5f; }}
  #chat-head h3 {{ margin:0; font-size:13px; font-weight:700; color:#e2e8f0; flex:1; min-width:0; }}
  #chat-head button {{ background:#0f172a; border:1px solid #334155; color:#94a3b8;
                       font-size:11px; padding:4px 8px; border-radius:4px; cursor:pointer;
                       font-weight:600; }}
  #chat-head button:hover {{ background:#1e3a5f; color:#e2e8f0; }}
  #chat-head .close-btn {{ background:none; border:none; font-size:16px; padding:2px 6px; }}
  #chat-examples {{ display:none; background:#1e293b; padding:10px 14px; border-bottom:1px solid #1e3a5f;
                    font-size:12px; color:#94a3b8; line-height:1.7; }}
  #chat-examples.open {{ display:block; }}
  #chat-msgs {{ flex:1; overflow-y:auto; padding:14px; font-size:13px; line-height:1.5;
                user-select:text; -webkit-user-select:text; }}
  #chat-msgs .msg {{ margin-bottom:14px; }}
  #chat-msgs .role-user {{ color:#fbbf24; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }}
  #chat-msgs .role-ai   {{ color:#60a5fa; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }}
  #chat-msgs .body      {{ background:#1e293b; padding:10px 12px; border-radius:6px; color:#e2e8f0;
                           white-space:pre-wrap; user-select:text; -webkit-user-select:text; }}
  #chat-msgs .body table {{ border-collapse:collapse; margin:6px 0; }}
  #chat-msgs .body th, #chat-msgs .body td {{ border:1px solid #334155; padding:4px 8px; font-size:12px; }}
  #chat-msgs .body code {{ background:#0f172a; padding:1px 4px; border-radius:3px; }}
  #chat-msgs .tool {{ color:#94a3b8; font-size:11px; font-style:italic; margin-top:4px; }}
  #chat-msgs .actions {{ display:flex; gap:6px; margin-top:6px; }}
  #chat-msgs .actions button {{ background:#0f172a; color:#94a3b8; border:1px solid #334155;
                                padding:3px 8px; border-radius:4px; font-size:11px; cursor:pointer; }}
  #chat-msgs .actions button:hover {{ background:#1e3a5f; color:#e2e8f0; }}
  #chat-msgs .actions button:disabled {{ opacity:0.6; cursor:default; }}
  #chat-form {{ display:flex; gap:6px; padding:10px; border-top:1px solid #1e3a5f; background:#0f172a; }}
  #chat-input {{ flex:1; background:#1e293b; color:#e2e8f0; border:1px solid #334155;
                 border-radius:6px; padding:8px 12px; font-size:13px; font-family:inherit; }}
  #chat-send {{ background:#1d4ed8; color:#fff; border:none; border-radius:6px; padding:8px 16px; font-weight:700; cursor:pointer; }}
  #chat-send:disabled {{ background:#64748b; cursor:not-allowed; }}
  #chat-status {{ font-size:10px; color:#94a3b8; padding:4px 14px; }}
</style>

<button id="chat-fab" onclick="window.chatToggle()">\U0001F4AC Ask AI</button>
<div id="chat-panel">
  <div id="chat-head">
    <h3><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};vertical-align:middle;margin-right:6px;"></span>A320/737 Sightings AI</h3>
    <button onclick="window.chatToggleExamples()" title="Show examples">\U0001F4A1 Examples</button>
    <button onclick="window.chatClear()" title="Clear conversation">\U0001F5D1 Clear</button>
    <button class="close-btn" onclick="window.chatCloseConfirm()" title="Close chat">\u2715</button>
  </div>
  <div id="chat-examples">{examples_html}</div>
  <div id="chat-msgs"></div>
  <div id="chat-status">{status_msg}</div>
  <form id="chat-form" onsubmit="window.chatSend(event)">
    <input id="chat-input" placeholder="Ask a question or give an instruction\u2026" autocomplete="off" {'' if enabled else 'disabled'}>
    <button id="chat-send" type="submit" {'' if enabled else 'disabled'}>Send</button>
  </form>
</div>

<script>
  (function () {{
    const HIST_KEY = "525_chat_history_v1";
    const TEAMS_ON = {('true' if teams else 'false')};
    let history = [];
    try {{ history = JSON.parse(localStorage.getItem(HIST_KEY) || "[]"); }} catch (e) {{}}

    const $msgs = document.getElementById("chat-msgs");
    const $form = document.getElementById("chat-form");
    const $input = document.getElementById("chat-input");
    const $send = document.getElementById("chat-send");
    const $panel = document.getElementById("chat-panel");
    const $ex    = document.getElementById("chat-examples");

    function renderMarkdown(s) {{
      return (s || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
        .replace(/(^|[\\s>])\\*([^*]+)\\*/g, "$1<em>$2</em>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\\n/g, "<br>");
    }}

    function appendMsg(role, body, tools) {{
      const div = document.createElement("div");
      div.className = "msg";
      const r = (role === "user") ? "\u270D\uFE0F You" : "\U0001F916 AI";
      const cls = (role === "user") ? "role-user" : "role-ai";
      const safeId = "msg-" + Date.now() + "-" + Math.floor(Math.random()*9999);
      let html = `<div class="${{cls}}">${{r}}</div><div class="body" id="${{safeId}}-body">${{renderMarkdown(body)}}</div>`;
      if (tools && tools.length) {{
        const names = tools.map(t => t.name).join(", ");
        html += `<div class="tool">tools used: ${{names}}</div>`;
      }}
      // Action row for AI messages only — Copy + Send to Teams (always shown;
      // server returns a clear error if the webhook is mis-configured)
      if (role !== "user") {{
        html += `<div class="actions">`;
        html += `<button onclick="window.chatCopy('${{safeId}}-body', this)" title="Copy reply to clipboard">\U0001F4CB Copy</button>`;
        html += `<button onclick="window.chatShare('${{safeId}}-body', this)" title="Post this reply to TAG Sales Chat">\U0001F4E8 Send to Teams</button>`;
        html += `</div>`;
      }}
      div.innerHTML = html;
      // Stash raw body text on the element for copy/share
      div.dataset.rawBody = body || "";
      $msgs.appendChild(div);
      $msgs.scrollTop = $msgs.scrollHeight;
      return div;
    }}

    // Replay any persisted conversation
    history.forEach(m => appendMsg(m.role, m.content));
    if (history.length === 0) {{
      // Show examples by default for a fresh chat
      $ex.classList.add("open");
    }}

    window.chatToggle = function () {{ $panel.classList.toggle("open"); localStorage.setItem("525_chat_open", $panel.classList.contains("open") ? "1" : "0"); $input.focus(); }};
    window.chatToggleExamples = function () {{ $ex.classList.toggle("open"); }};

    window.chatCloseConfirm = function () {{
      if (confirm("Are you done with the chat? (Conversation history will be saved either way.)")) {{
        $panel.classList.remove("open");
        localStorage.setItem("525_chat_open", "0");
      }}
    }};

    window.chatClear = function () {{
      if (!confirm("Clear the entire conversation? This wipes history for everyone using this browser.")) return;
      history = [];
      localStorage.removeItem(HIST_KEY);
      $msgs.innerHTML = "";
      $ex.classList.add("open");
    }};

    window.chatCopy = function (bodyId, btn) {{
      const el = document.getElementById(bodyId);
      const text = el && el.parentElement && el.parentElement.dataset.rawBody || (el ? el.innerText : "");
      navigator.clipboard.writeText(text).then(() => {{
        const orig = btn.innerText;
        btn.innerText = "\u2713 Copied";
        btn.disabled = true;
        setTimeout(() => {{ btn.innerText = orig; btn.disabled = false; }}, 1500);
      }}).catch(() => {{ btn.innerText = "\u2715 Failed"; }});
    }};

    window.chatShare = async function (bodyId, btn) {{
      const el = document.getElementById(bodyId);
      const text = el && el.parentElement && el.parentElement.dataset.rawBody || (el ? el.innerText : "");
      if (!text.trim()) return;
      btn.disabled = true;
      const orig = btn.innerText;
      btn.innerText = "Sending\u2026";
      try {{
        const resp = await fetch("/api/share-to-teams", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{title: "A320/737 Sightings \u2014 AI analyst answer", text: text}}),
        }});
        const data = await resp.json();
        btn.innerText = data.sent ? "\u2713 Sent to Teams" : ("\u2715 " + (data.error || "fail"));
        setTimeout(() => {{ btn.innerText = orig; btn.disabled = false; }}, 2500);
      }} catch (e) {{
        btn.innerText = "\u2715 Failed";
        setTimeout(() => {{ btn.innerText = orig; btn.disabled = false; }}, 2500);
      }}
    }};

    // Restore open/closed state from last session — panel stays open across reloads
    if (localStorage.getItem("525_chat_open") === "1") {{
      $panel.classList.add("open");
    }}

    window.chatSend = async function (ev) {{
      ev.preventDefault();
      const text = ($input.value || "").trim();
      if (!text) return;
      appendMsg("user", text);
      history.push({{role: "user", content: text}});
      $input.value = "";
      $send.disabled = true;
      $send.textContent = "...";

      // Create an empty AI message we fill as tokens stream in.
      const aiDiv  = appendMsg("assistant", "");
      const aiBody = aiDiv.querySelector(".body");
      let buffer = "";
      let statusEl = null;
      const setStatus = (t) => {{
        if (!statusEl) {{
          statusEl = document.createElement("div");
          statusEl.className = "tool";
          aiDiv.insertBefore(statusEl, aiDiv.querySelector(".actions"));
        }}
        statusEl.textContent = t;
      }};
      const clearStatus = () => {{ if (statusEl) {{ statusEl.remove(); statusEl = null; }} }};

      const finalize = (reply, toolCalls) => {{
        buffer = reply != null ? reply : buffer;
        aiBody.innerHTML = renderMarkdown(buffer);
        aiDiv.dataset.rawBody = buffer;
        clearStatus();
        if (toolCalls && toolCalls.length) {{
          const names = toolCalls.map(t => t.name).join(", ");
          const te = document.createElement("div");
          te.className = "tool";
          te.textContent = "tools used: " + names;
          aiDiv.insertBefore(te, aiDiv.querySelector(".actions"));
        }}
        history.push({{role: "assistant", content: buffer}});
        if (history.length > 40) history = history.slice(-40);
        localStorage.setItem(HIST_KEY, JSON.stringify(history));
        $msgs.scrollTop = $msgs.scrollHeight;
      }};

      // Non-streaming fallback used if SSE isn't available or errors early.
      const fallback = async () => {{
        const resp = await fetch("/api/chat", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{messages: history.filter(m => m.role !== "assistant" || m.content)}}),
        }});
        const data = await resp.json();
        finalize(data.reply || data.error || "(no reply)", data.tool_calls);
      }};

      try {{
        const resp = await fetch("/api/chat/stream", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{messages: history}}),
        }});
        if (!resp.ok || !resp.body) {{ await fallback(); return; }}

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let sseBuf = "";
        let gotToken = false;
        let doneEvent = null;

        while (true) {{
          const {{value, done}} = await reader.read();
          if (done) break;
          sseBuf += decoder.decode(value, {{stream: true}});
          const parts = sseBuf.split("\\n\\n");
          sseBuf = parts.pop();
          for (const part of parts) {{
            const line = part.trim();
            if (!line.startsWith("data:")) continue;
            let evt;
            try {{ evt = JSON.parse(line.slice(5).trim()); }} catch (_) {{ continue; }}
            if (evt.type === "token") {{
              gotToken = true;
              clearStatus();
              buffer += evt.text;
              aiBody.innerHTML = renderMarkdown(buffer);
              aiDiv.dataset.rawBody = buffer;
              $msgs.scrollTop = $msgs.scrollHeight;
            }} else if (evt.type === "status") {{
              if (!gotToken) setStatus(evt.text);
            }} else if (evt.type === "done") {{
              doneEvent = evt;
            }} else if (evt.type === "error") {{
              if (!gotToken) {{ await fallback(); return; }}
            }}
          }}
        }}

        if (doneEvent) {{
          finalize(doneEvent.reply, doneEvent.tool_calls);
        }} else if (gotToken) {{
          finalize(buffer, null);
        }} else {{
          await fallback();
        }}
      }} catch (e) {{
        try {{ await fallback(); }}
        catch (e2) {{ finalize("Sorry \u2014 chat request failed: " + e.message, null); }}
      }} finally {{
        $send.disabled = false;
        $send.textContent = "Send";
        $input.focus();
      }}
    }};
  }})();
</script>
"""


def _pagination_html(page: int, total_pages: int, per_page: int, total: int) -> str:
    """Render page-size selector + prev/next nav. Preserves chosen per_page."""
    sizes_html = ""
    for size in (25, 50, 100):
        if size == per_page:
            sizes_html += (
                f'<span style="background:#1d4ed8;color:#fff;padding:4px 10px;'
                f'border-radius:4px;font-size:12px;font-weight:700;">{size}</span>'
            )
        else:
            # Switching size resets to page 1
            sizes_html += (
                f'<a href="?per_page={size}&page=1" style="background:#1e293b;'
                f'color:#94a3b8;padding:4px 10px;border-radius:4px;font-size:12px;'
                f'font-weight:600;text-decoration:none;">{size}</a>'
            )

    prev_html = (
        f'<a href="?per_page={per_page}&page={page-1}" style="background:#1e293b;'
        f'color:#60a5fa;padding:4px 12px;border-radius:4px;font-size:12px;'
        f'font-weight:600;text-decoration:none;">\u2190 Prev</a>'
        if page > 1 else
        '<span style="color:#64748b;padding:4px 12px;font-size:12px;">\u2190 Prev</span>'
    )
    next_html = (
        f'<a href="?per_page={per_page}&page={page+1}" style="background:#1e293b;'
        f'color:#60a5fa;padding:4px 12px;border-radius:4px;font-size:12px;'
        f'font-weight:600;text-decoration:none;">Next \u2192</a>'
        if page < total_pages else
        '<span style="color:#64748b;padding:4px 12px;font-size:12px;">Next \u2192</span>'
    )

    start = (page - 1) * per_page + 1 if total else 0
    end   = min(page * per_page, total)

    return (
        f'<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;'
        f'margin-bottom:12px;padding:8px 12px;background:#0f172a;border-radius:6px;'
        f'border:1px solid #1e3a5f;">'
        f'  <span style="font-size:11px;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:1px;">Per page</span>'
        f'  <div style="display:flex;gap:4px;">{sizes_html}</div>'
        f'  <span style="flex:1;"></span>'
        f'  <span style="font-size:12px;color:#94a3b8;">'
        f'    Showing <strong style="color:#e2e8f0;">{start}\u2013{end}</strong> '
        f'of <strong style="color:#e2e8f0;">{total:,}</strong>'
        f'  </span>'
        f'  {prev_html}'
        f'  <span style="font-size:12px;color:#94a3b8;">'
        f'    Page <strong style="color:#e2e8f0;">{page}</strong> / {total_pages}'
        f'  </span>'
        f'  {next_html}'
        f'</div>'
    )


def _fleet_penetration_html() -> str:
    """Render per-performance-group ATLAS adoption thermometers (5 groups).

    Each bar is stacked: solid green = currently-active installs, muted amber
    = had-ATLAS-but-removed. Header surfaces the retention rate
    (active / (active + removed)) — the answer to "but some got removed."
    """
    rows = tamarack_fleet.fleet_penetration_by_perfgroup()
    if not rows:
        return ""

    total_installed = sum(r["installed"] for r in rows)
    total_removed   = sum(r["removed"]   for r in rows)
    total_fleet     = sum(r["total"]     for r in rows)
    total_touched   = total_installed + total_removed
    overall_pct     = round(100.0 * total_installed / total_fleet, 1) if total_fleet else 0.0
    retention_pct   = (round(100.0 * total_installed / total_touched, 1)
                       if total_touched else 0.0)

    # Preserve the canonical PERF_GROUPS order (CJ/CJ1, CJ1+/M2, CJ2, CJ2+,
    # CJ3/CJ3+). Nick: "I want CJ2 to come before CJ2+ in all the ordering."
    rows_sorted = rows

    # Color ramp for the ACTIVE segment (deeper green = higher penetration)
    def _bar_color(pct: float) -> str:
        if pct >= 15: return "#16a34a"  # deep green
        if pct >= 8:  return "#22c55e"  # mid green
        if pct >= 4:  return "#65a30d"  # olive
        return "#a16207"                # amber for lowest tier

    REMOVED_COL = "#7c5b1e"  # muted amber for the "removed" segment

    bars_html = ""
    # Scale: 40% penetration = full bar width
    for r in rows_sorted:
        col          = _bar_color(r["pct"])
        bar_active   = min(r["pct"]         / 40.0 * 100.0, 100.0)
        bar_removed  = min(r["removed_pct"] / 40.0 * 100.0, max(0.0, 100.0 - bar_active))
        removed_tip  = (f"{r['removed']} hull(s) previously had ATLAS but were removed "
                        f"(includes 2018–19 AD-era removals).")
        removed_seg  = (
            f'<div title="{removed_tip}" style="background:{REMOVED_COL};height:100%;'
            f'width:{bar_removed:.1f}%;border-left:1px solid #1e293b;cursor:help;"></div>'
            if r["removed"] else ""
        )
        removed_label = (
            f' &nbsp;<span style="color:{REMOVED_COL};" title="{removed_tip}">'
            f'+ {r["removed_pct"]:.1f}% removed</span>'
            if r["removed"] else ""
        )
        bars_html += (
            f'<div style="display:grid;grid-template-columns:90px 1fr 200px;'
            f'align-items:center;gap:12px;margin-bottom:6px;">'
            f'  <div style="font-weight:700;color:#e2e8f0;font-size:13px;">{r["label"]}</div>'
            f'  <div style="background:#0f172a;border-radius:4px;height:18px;'
            f'overflow:hidden;border:1px solid #1e3a5f;display:flex;align-items:stretch;">'
            f'    <div style="background:{col};height:100%;width:{bar_active:.1f}%;'
            f'transition:width 0.4s;"></div>'
            f'    {removed_seg}'
            f'  </div>'
            f'  <div style="font-size:12px;color:#94a3b8;text-align:right;">'
            f'    <span style="color:{col};font-weight:700;">{r["pct"]:.1f}%</span>'
            f'    {removed_label}'
            f'    <br><span style="font-size:10px;color:#94a3b8;">'
            f'({r["installed"]}{f" + {r['removed']} removed" if r["removed"] else ""} / {r["total"]})'
            f'    </span>'
            f'  </div>'
            f'</div>'
        )

    # Retention badge — green if ≥95%, amber if 90–95%, red below.
    if retention_pct >= 95:
        ret_col = "#22c55e"
    elif retention_pct >= 90:
        ret_col = "#eab308"
    else:
        ret_col = "#ef4444"

    retention_badge = (
        f'<div title="Active installs ÷ (active + ever-removed). '
        f'Removals include 9 hulls — 4 during the 2018-19 FAA AD (since closed) '
        f'plus 5 since 2023." '
        f'style="display:inline-flex;align-items:center;gap:6px;background:#0f172a;'
        f'border:1px solid {ret_col};color:{ret_col};padding:3px 10px;border-radius:999px;'
        f'font-size:11px;font-weight:700;cursor:help;">'
        f'🏆 {retention_pct:.1f}% retention since 2016 '
        f'<span style="color:#94a3b8;font-weight:500;">'
        f'({total_installed} active · {total_removed} removed)</span>'
        f'</div>'
        if total_touched else ""
    )

    return (
        f'<div style="background:#1e293b;border-radius:8px;'
        f'padding:16px 20px;flex:1;min-width:420px;">'
        f'  <div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'margin-bottom:14px;gap:10px;flex-wrap:wrap;">'
        f'    <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:1px;">'
        f'      🌡️ ATLAS Fleet Penetration — % of worldwide production with winglets'
        f'    </div>'
        f'    <div style="font-size:12px;color:#94a3b8;">'
        f'      Overall: <strong style="color:#22c55e;">{overall_pct:.1f}%</strong>'
        f'      &nbsp;<span style="color:#94a3b8;">({total_installed} / {total_fleet})</span>'
        f'    </div>'
        f'  </div>'
        f'  <div style="margin-bottom:10px;">{retention_badge}</div>'
        f'  {bars_html}'
        f'  <div style="font-size:10px;color:#94a3b8;margin-top:10px;line-height:1.5;'
        f'display:flex;gap:14px;flex-wrap:wrap;align-items:center;">'
        f'    <span><span style="display:inline-block;width:10px;height:10px;'
        f'background:#22c55e;border-radius:2px;vertical-align:middle;margin-right:4px;"></span>'
        f'Active ATLAS install</span>'
        f'    <span><span style="display:inline-block;width:10px;height:10px;'
        f'background:{REMOVED_COL};border-radius:2px;vertical-align:middle;margin-right:4px;"></span>'
        f'Removed (had ATLAS, no longer installed)</span>'
        f'    <span style="color:#64748b;">Bar width: 40% penetration = full width. '
        f'Worldwide totals via <code>FLEET_TOTALS_WORLD</code>.</span>'
        f'  </div>'
        f'</div>'
    )


def _airline_insights_html(region: str, title: str, back_href: str, back_label: str) -> str:
    """Render A320/737 operational insights. No CJ/ATLAS/WAT logic."""
    family = _normalize_family(request.args.get("family"))
    data = database.get_airline_insights(region=region, limit=15, family=family)
    compare_region = "EU_UK" if region == "NA" else "NA"
    compare = database.get_airline_insights(region=compare_region, limit=5, family=family)
    stats = database.get_period_stats(region=region, family=family)
    mission_bins = database.get_airline_mission_bins(region=region, family=family or "A320")
    mission_bins_html = _mission_bins_html(mission_bins)
    route_map = database.get_route_map_data(top_n=80, region=region, family=family)
    route_airports_js = _json.dumps(route_map.get("airports", []))
    route_routes_js = _json.dumps(route_map.get("routes", []))
    type_labels_js = _json.dumps([x.get("label") for x in data.get("top_types", [])[:10]])
    type_counts_js = _json.dumps([x.get("n", 0) for x in data.get("top_types", [])[:10]])
    op_labels_js = _json.dumps([x.get("label") for x in data.get("top_operators", [])[:10]])
    op_counts_js = _json.dumps([x.get("n", 0) for x in data.get("top_operators", [])[:10]])
    airport_labels_js = _json.dumps([x.get("label") for x in data.get("top_airports", [])[:10]])
    airport_counts_js = _json.dumps([x.get("n", 0) for x in data.get("top_airports", [])[:10]])
    dist_labels_js = _json.dumps(data.get("dist_hist_labels", []))
    dist_counts_js = _json.dumps(data.get("dist_hist", []))
    block_scatter_js = _json.dumps(data.get("block_scatter", []))
    fl_labels_js = _json.dumps(data.get("fl_hist_labels", []))
    fl_counts_js = _json.dumps(data.get("fl_hist", []))
    map_center_lat = 52 if region == "EU_UK" else 39
    map_center_lon = 10 if region == "EU_UK" else -96
    map_zoom = 4 if region == "EU_UK" else 4
    map_max_zoom = 6 if region == "EU_UK" else 5

    def simple_rows(items, name="Item", third_label="", third_fn=None):
        if not items:
            return '<tr><td colspan="3" style="text-align:center;color:#64748b;padding:14px;">No data yet</td></tr>'
        out = []
        for it in items:
            third = third_fn(it) if third_fn else ""
            out.append(
                f'<tr><td>{it.get("label") or "Unknown"}</td>'
                f'<td style="text-align:right;color:#60a5fa;font-weight:700;">{it.get("n", 0)}</td>'
                f'<td>{third}</td></tr>'
            )
        return "".join(out)

    longest_rows = []
    for r in data.get("longest", []):
        route = f'{r.get("origin_icao") or "—"} → {r.get("dest_icao") or "—"}'
        longest_rows.append(
            f'<tr><td>{r.get("tail_number") or "—"}</td><td>{r.get("type") or "—"}</td>'
            f'<td>{route}</td><td style="text-align:right;color:#f59e0b;font-weight:700;">{int(r.get("distance_nm") or 0):,} nm</td>'
            f'<td>{r.get("operator") or "Unknown"}</td><td>{(r.get("arrived_utc") or "")[:16].replace("T", " ")} UTC</td></tr>'
        )
    longest_html = "".join(longest_rows) or '<tr><td colspan="6" style="text-align:center;color:#64748b;padding:14px;">No distance data yet</td></tr>'
    avg = f'{data["avg_distance"]} nm' if data.get("avg_distance") else "—"
    avg_fl = f'FL{data["avg_fl"]}' if data.get("avg_fl") else "—"
    med_fl = f'FL{data["median_fl"]}' if data.get("median_fl") else "—"
    cmp_avg_fl = f'FL{compare["avg_fl"]}' if compare.get("avg_fl") else "—"
    cmp_avg_dist = f'{compare["avg_distance"]} nm' if compare.get("avg_distance") else "—"
    route_rows = simple_rows(data["top_routes"], third_fn=lambda x: f'{int(x.get("avg_nm") or 0)} nm' if x.get("avg_nm") else "—")
    fam_label = _family_label(family)
    fam_filter = _family_filter_html(family, "/eu-insights" if region == "EU_UK" else "/insights")
    mission_note = (
        f"<strong>{fam_label}</strong> mission sample: {data['total']:,} flights, "
        f"avg distance <strong>{avg}</strong>, avg flight level <strong>{avg_fl}</strong>. "
        "Next layer will feed these mission bins into Tamarack Mission Analysis for fuel, climb, and WAT benefit estimates."
    )
    export_region = region
    export_family = family or "A320"
    export_links = (
        f'<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">'
        f'<a href="/export/mission-bins.csv?region={export_region}&family={export_family}" style="display:inline-block;background:#0f172a;color:#93c5fd;border:1px solid #334155;border-radius:6px;padding:6px 10px;font-size:12px;font-weight:700;">↓ Mission bins CSV</a>'
        f'<a href="/api/mission-bins?region={export_region}&family={export_family}" style="display:inline-block;background:#0f172a;color:#93c5fd;border:1px solid #334155;border-radius:6px;padding:6px 10px;font-size:12px;font-weight:700;">Mission bins JSON</a>'
        f'</div>'
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — A320/737 Sightings</title><meta http-equiv="refresh" content="120">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}} body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}} a{{color:#60a5fa}} h1{{font-size:28px;margin-bottom:6px}} .sub{{color:#94a3b8;margin-bottom:18px}} .nav{{margin-bottom:18px;font-size:14px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}} .stat,.card{{background:#1e293b;border-radius:8px;padding:16px}} .label{{font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px}} .value{{font-size:26px;font-weight:800;color:#60a5fa}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;margin-bottom:18px}} #routeMap{{height:420px;border-radius:8px;border:1px solid #334155;margin-bottom:18px;background:#020617}} .chartbox{{height:320px}} h2{{font-size:15px;margin-bottom:10px}} table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden}} th{{background:#334155;color:#94a3b8;font-size:11px;text-transform:uppercase;text-align:left;padding:9px}} td{{padding:9px;border-bottom:1px solid #334155;font-size:13px}} tr:hover{{background:#243244}}
</style></head><body>
<div class="nav"><a href="{back_href}">← {back_label}</a> &nbsp;·&nbsp; <a href="/">NA Sightings</a> &nbsp;·&nbsp; <a href="/insights">NA Insights</a> &nbsp;·&nbsp; <a href="/eu">EU Sightings</a> &nbsp;·&nbsp; <a href="/eu-insights">EU Insights</a></div>
<h1>{title}</h1><div class="sub">{fam_label} operational patterns · region: <strong>{region}</strong> · auto-refreshes every 2 min</div>{fam_filter}
<div class="card" style="margin-bottom:18px;border-left:4px solid #22c55e;"><h2>Tamarack Mission-Benefit Setup</h2><div style="color:#cbd5e1;line-height:1.45;">{mission_note}</div>{export_links}</div>
{mission_bins_html}
<div class="stats"><div class="stat"><div class="label">Last 24h</div><div class="value">{stats['today']}</div></div><div class="stat"><div class="label">Last 7 days</div><div class="value">{stats['week']}</div></div><div class="stat"><div class="label">All Time</div><div class="value">{data['total']}</div></div><div class="stat"><div class="label">Active Tails</div><div class="value">{data['active_tails']}</div></div><div class="stat"><div class="label">Avg Distance</div><div class="value">{avg}</div></div><div class="stat"><div class="label">Avg Flight Level</div><div class="value">{avg_fl}</div></div><div class="stat"><div class="label">Median Flight Level</div><div class="value">{med_fl}</div></div></div><div class="card" style="margin-bottom:18px;"><h2>NA vs EU altitude context</h2><div style="color:#cbd5e1;line-height:1.45;">Current page: <strong>{region}</strong> avg cruise/top altitude <strong>{avg_fl}</strong>, avg distance <strong>{avg}</strong>. Comparison region <strong>{compare_region}</strong>: avg cruise/top altitude <strong>{cmp_avg_fl}</strong>, avg distance <strong>{cmp_avg_dist}</strong>. EU short-haul flights often cruise lower because of airspace/ATC constraints; treat low FL as operational environment unless distance and route suggest otherwise.</div></div>
<div class="grid"><div class="card chartbox"><h2>Flight Level Distribution</h2><canvas id="flChart"></canvas></div><div class="card chartbox"><h2>Aircraft Mix</h2><canvas id="typeChart"></canvas></div><div class="card chartbox"><h2>Top Operators</h2><canvas id="operatorChart"></canvas></div><div class="card chartbox"><h2>Arrival Airports</h2><canvas id="airportChart"></canvas></div><div class="card chartbox"><h2>Distance Distribution</h2><canvas id="distanceChart"></canvas></div><div class="card chartbox" style="grid-column:1/-1;"><h2>Block Speed vs Distance</h2><canvas id="blockChart"></canvas></div></div><h2>Route Map</h2><div id="routeMap"></div><div class="grid"><div class="card"><h2>Top Aircraft Variants</h2><table><thead><tr><th>Variant</th><th style="text-align:right;">Flights</th><th></th></tr></thead><tbody>{simple_rows(data['top_types'])}</tbody></table></div><div class="card"><h2>Top Operators</h2><table><thead><tr><th>Operator</th><th style="text-align:right;">Flights</th><th></th></tr></thead><tbody>{simple_rows(data['top_operators'])}</tbody></table></div><div class="card"><h2>Top Arrival Airports</h2><table><thead><tr><th>Airport</th><th style="text-align:right;">Arrivals</th><th></th></tr></thead><tbody>{simple_rows(data['top_airports'])}</tbody></table></div><div class="card"><h2>Top Routes</h2><table><thead><tr><th>Route</th><th style="text-align:right;">Flights</th><th>Avg Distance</th></tr></thead><tbody>{route_rows}</tbody></table></div></div>
<h2>Longest Observed Flights</h2><table><thead><tr><th>Tail</th><th>Type</th><th>Route</th><th style="text-align:right;">Distance</th><th>Operator</th><th>Arrived</th></tr></thead><tbody>{longest_html}</tbody></table>
<script>
const typeLabels = {type_labels_js}, typeCounts = {type_counts_js};
const opLabels = {op_labels_js}, opCounts = {op_counts_js};
const airportLabels = {airport_labels_js}, airportCounts = {airport_counts_js};
const distLabels = {dist_labels_js}, distCounts = {dist_counts_js};
const blockScatter = {block_scatter_js};
const flLabels = {fl_labels_js}, flCounts = {fl_counts_js};
const chartOpts = {{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{labels:{{color:'#cbd5e1'}}}}}}, scales:{{x:{{ticks:{{color:'#94a3b8'}},grid:{{color:'#334155'}}}},y:{{ticks:{{color:'#94a3b8'}},grid:{{color:'#334155'}}}}}} }};
function bar(id, labels, data, label, color) {{ const el=document.getElementById(id); if(!el || typeof Chart==='undefined') return; new Chart(el, {{type:'bar', data:{{labels, datasets:[{{label, data, backgroundColor:color, borderColor:color}}]}}, options:chartOpts}}); }}
bar('flChart', flLabels, flCounts, 'Flights', '#f472b6');
bar('typeChart', typeLabels, typeCounts, 'Flights', '#60a5fa');
bar('operatorChart', opLabels, opCounts, 'Flights', '#22c55e');
bar('airportChart', airportLabels, airportCounts, 'Arrivals', '#f59e0b');
bar('distanceChart', distLabels, distCounts, 'Flights', '#a78bfa');
const bel=document.getElementById('blockChart'); if(bel && typeof Chart!=='undefined') new Chart(bel, {{type:'scatter', data:{{datasets:[{{label:'Flights', data:blockScatter, pointRadius:3, pointBackgroundColor:'#38bdf8'}}]}}, options:{{...chartOpts, parsing:false, scales:{{x:{{title:{{display:true,text:'Distance (nm)',color:'#cbd5e1'}},ticks:{{color:'#94a3b8'}},grid:{{color:'#334155'}}}},y:{{title:{{display:true,text:'Block speed (kt)',color:'#cbd5e1'}},ticks:{{color:'#94a3b8'}},grid:{{color:'#334155'}}}}}}}} }});

const routeAirports = {route_airports_js};
const routeRoutes = {route_routes_js};
(function() {{
  const el = document.getElementById('routeMap');
  if (!el || typeof L === 'undefined') return;
  const map = L.map('routeMap', {{ zoomControl: true, scrollWheelZoom: false }}).setView([{map_center_lat}, {map_center_lon}], {map_zoom});
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 12, attribution: '&copy; OpenStreetMap' }}).addTo(map);
  const byIcao = Object.fromEntries(routeAirports.map(a => [a.icao, a]));
  routeRoutes.forEach(r => {{
    const o = byIcao[r.o], d = byIcao[r.d];
    if (!o || !d) return;
    L.polyline([[o.lat,o.lon],[d.lat,d.lon]], {{ color:'#60a5fa', weight: Math.min(8, 1 + r.count), opacity:0.65 }}).bindTooltip(`${{r.o}} → ${{r.d}} · ${{r.count}} flights`).addTo(map);
  }});
  routeAirports.forEach(a => L.circleMarker([a.lat,a.lon], {{ radius:4, color:'#f59e0b', fillColor:'#fbbf24', fillOpacity:.8, weight:1 }}).bindTooltip(a.icao).addTo(map));
  if (routeAirports.length) {{
    const bounds = L.latLngBounds(routeAirports.map(a => [a.lat, a.lon]));
    map.fitBounds(bounds, {{ padding:[30,30], maxZoom: {map_max_zoom} }});
  }}
}})();
</script>
{_chat_widget_html()}
</body></html>"""


@app.get("/")
def dashboard():
    # Pagination: per_page in {25, 50, 100}, page ≥ 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except ValueError:
        per_page = 50
    if per_page not in (25, 50, 100):
        per_page = 50
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    family = _normalize_family(request.args.get("family"))
    total_sightings = _sightings_total(family=family)
    total_pages     = max(1, (total_sightings + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    rows = _recent_sightings(limit=per_page, offset=offset, family=family)
    status = daemon_state["status"]
    last_poll = daemon_state.get("last_poll_utc") or "—"
    last_error = daemon_state.get("last_error") or ""
    stats = database.get_period_stats(family=family)
    opp_feed = database.get_airline_opportunity_feed(region="NA", family=family, limit=6)
    opportunity_html = _opportunity_feed_html(opp_feed)
    # A320/737 app: no inherited ATLAS prospect/fleet-penetration panels.

    status_color = {"running": "#22c55e", "error": "#ef4444", "starting": "#f59e0b"}.get(status, "#888")
    status_dot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{status_color};margin-right:6px;"></span>'

    weekly_html = ""

    rows_html = "".join(_render_sighting_row_html(r) for r in rows)

    if not rows_html:
        rows_html = '<tr><td colspan="15" style="text-align:center;color:#666;padding:24px;">No sightings yet — daemon is polling…</td></tr>'

    error_banner = f'<div style="background:#7f1d1d;color:#fca5a5;padding:10px 20px;font-size:13px;margin-bottom:16px;border-radius:6px;">Last error: {last_error}</div>' if last_error else ""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>A320/737 Sightings</title>
  <meta http-equiv="refresh" content="60">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    /* Bottom padding so the floating "Ask AI" bubble doesn't cover the bottom pagination row */
    body {{ background: #0f172a; color: #e2e8f0; font-family: Arial, sans-serif; padding: 24px 24px 110px; }}
    h1 {{ font-size: 24px; font-weight: bold; margin-bottom: 4px; }}
    .sub {{ color: #94a3b8; font-size: 13px; margin-bottom: 12px; }}
    /* Sticky top header: title + stats + action buttons remain visible while scrolling */
    .sticky-top {{
      position: sticky;
      top: -24px;            /* offset cancels body padding so it pins flush to viewport top */
      margin: -24px -24px 20px -24px;
      padding: 24px 24px 12px 24px;
      background: rgba(15, 23, 42, 0.96);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border-bottom: 1px solid #1e3a5f;
      z-index: 10;
    }}
    .stats {{ display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
    .stat {{ background: #1e293b; border-radius: 8px; padding: 10px 14px; min-width: 100px; }}
    .stat .label {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
    .stat .value {{ font-size: 20px; font-weight: bold; margin-top: 2px; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; }}
    th {{ background: #0f172a; padding: 10px 14px; text-align: left; font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
    td {{ padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #0f172a; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #263148; }}
    a {{ color: #60a5fa; text-decoration: none; }}
    .local-time {{ color: #94a3b8; font-size: 11px; display: block; margin-top: 2px; }}
  </style>
</head>
<body>
  {_identify_modal_html()}
  <div class="sticky-top">
  <h1>A320/737 Sightings</h1>
  <div class="sub">{_family_label(family)} landings in the USA &nbsp;·&nbsp; Auto-refreshes every 60s</div>
  {_family_filter_html(family, "/")}
  {error_banner}
  <div class="stats">
    <div class="stat">
      <div class="label">Daemon</div>
      <div class="value">{status_dot}{status.capitalize()}</div>
    </div>
    <div class="stat">
      <div class="label">Last Poll</div>
      <div class="value" style="font-size:13px;">{str(last_poll)[:16].replace('T',' ') if last_poll != '—' else '—'}</div>
    </div>
    <div class="stat">
      <div class="label">Last Hour</div>
      <div class="value">{stats['hour']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 24h</div>
      <div class="value">{stats['today']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 7 days</div>
      <div class="value">{stats['week']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 30 days</div>
      <div class="value">{stats['month']}</div>
    </div>
    <div class="stat">
      <div class="label">YTD</div>
      <div class="value">{stats['ytd']}</div>
    </div>
    <div class="stat">
      <div class="label">All Time</div>
      <div class="value">{stats['total']}</div>
    </div>
  </div>
  <div style="margin-bottom:4px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
    <a href="/plan" style="display:inline-block;background:#4f46e5;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      🛫 Daily Flight Plan
    </a>
    <a href="/insights{_family_query_suffix(family)}" style="display:inline-block;background:#1d4ed8;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      📊 NA Insights
    </a>
    <a href="/eu{_family_query_suffix(family)}" title="EU/UK sightings stream — same layout as this page, filtered to Europe/UK landings" style="display:inline-block;background:#4338ca;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      EU Sightings
    </a>
    <a href="/eu-insights{_family_query_suffix(family)}" style="display:inline-block;background:#6d28d9;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      📊 EU Insights
    </a>
    {_watch_pill_html()}
  </div>
  </div> <!-- /sticky-top -->

  {opportunity_html}

  <!-- Airline sightings stream. -->

  <!-- Pagination controls (top) -->
  {_pagination_html(page, total_pages, per_page, total_sightings)}

  <table>
    <thead>
      <tr>
        <th>Tail</th><th>Type</th><th>From</th><th>To</th>
        <th>Distance</th><th>Duration</th><th>Block</th><th>Fuel</th><th>Programs</th><th>Climb</th><th>Arrived (UTC)</th><th>Local time</th><th>Operator</th><th>Source</th><th>Link</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <!-- Pagination controls (bottom) -->
  {_pagination_html(page, total_pages, per_page, total_sightings)}

  {_chat_widget_html()}
</body>
</html>"""
    return html


@app.get("/eu")
def eu_dashboard():
    """
    EU_UK-scoped mirror of the NA homepage `/`. Same sticky header, same KPI
    stats layout, same 16-column flight table (via `_render_sighting_row_html`),
    same pagination. All counts, stream, and totals are filtered to
    `region = 'EU_UK'`.
    """
    try:
        per_page = int(request.args.get("per_page", 50))
    except ValueError:
        per_page = 50
    if per_page not in (25, 50, 100):
        per_page = 50
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    family = _normalize_family(request.args.get("family"))
    total_sightings = _sightings_total_eu(family=family)
    total_pages     = max(1, (total_sightings + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    rows       = _recent_eu_sightings(limit=per_page, offset=offset, family=family)
    status     = daemon_state["status"]
    last_poll  = daemon_state.get("last_poll_utc") or "—"
    last_error = daemon_state.get("last_error") or ""
    stats      = database.get_period_stats(region="EU_UK", family=family)
    opp_feed   = database.get_airline_opportunity_feed(region="EU_UK", family=family, limit=6)
    opportunity_html = _opportunity_feed_html(opp_feed)

    status_color = {"running": "#22c55e", "error": "#ef4444", "starting": "#f59e0b"}.get(status, "#888")
    status_dot   = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{status_color};margin-right:6px;"></span>'

    rows_html = "".join(_render_sighting_row_html(r) for r in rows)
    if not rows_html:
        rows_html = (
            '<tr><td colspan="15" style="text-align:center;color:#666;padding:24px;">'
            'No EU sightings yet — daemon is polling…'
            '</td></tr>'
        )

    error_banner = (
        f'<div style="background:#7f1d1d;color:#fca5a5;padding:10px 20px;font-size:13px;'
        f'margin-bottom:16px;border-radius:6px;">Last error: {last_error}</div>'
        if last_error else ""
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>A320/737 Sightings — EU/UK</title>
  <meta http-equiv="refresh" content="60">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0f172a; color: #e2e8f0; font-family: Arial, sans-serif; padding: 24px 24px 110px; }}
    h1 {{ font-size: 24px; font-weight: bold; margin-bottom: 4px; }}
    .sub {{ color: #94a3b8; font-size: 13px; margin-bottom: 12px; }}
    .sticky-top {{
      position: sticky;
      top: -24px;
      margin: -24px -24px 20px -24px;
      padding: 24px 24px 12px 24px;
      background: rgba(15, 23, 42, 0.96);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border-bottom: 1px solid #4338ca55;
      z-index: 10;
    }}
    .stats {{ display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
    .stat {{ background: #1e293b; border-radius: 8px; padding: 10px 14px; min-width: 100px; }}
    .stat .label {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
    .stat .value {{ font-size: 20px; font-weight: bold; margin-top: 2px; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; }}
    th {{ background: #0f172a; padding: 10px 14px; text-align: left; font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
    td {{ padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #0f172a; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #263148; }}
    a {{ color: #60a5fa; text-decoration: none; }}
    .local-time {{ color: #94a3b8; font-size: 11px; display: block; margin-top: 2px; }}
    /* EU accent — indigo left border on the sticky header + subtle badge in title */
    .eu-pill {{ display:inline-block; background:#4338ca; color:#fff; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700; letter-spacing:0.5px; vertical-align:middle; margin-left:6px; }}
  </style>
</head>
<body>
  {_identify_modal_html()}
  <div class="sticky-top">
  <h1>A320/737 Sightings <span class="eu-pill">EU / UK</span></h1>
  <div class="sub">{_family_label(family)} landings in Europe &amp; the UK &nbsp;·&nbsp; Auto-refreshes every 60s</div>
  {_family_filter_html(family, "/eu")}
  {error_banner}
  <div class="stats">
    <div class="stat">
      <div class="label">Daemon</div>
      <div class="value">{status_dot}{status.capitalize()}</div>
    </div>
    <div class="stat">
      <div class="label">Last Poll</div>
      <div class="value" style="font-size:13px;">{str(last_poll)[:16].replace('T',' ') if last_poll != '—' else '—'}</div>
    </div>
    <div class="stat">
      <div class="label">Last Hour</div>
      <div class="value">{stats['hour']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 24h</div>
      <div class="value">{stats['today']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 7 days</div>
      <div class="value">{stats['week']}</div>
    </div>
    <div class="stat">
      <div class="label">Last 30 days</div>
      <div class="value">{stats['month']}</div>
    </div>
    <div class="stat">
      <div class="label">YTD</div>
      <div class="value">{stats['ytd']}</div>
    </div>
    <div class="stat">
      <div class="label">All Time (EU)</div>
      <div class="value">{stats['total']}</div>
    </div>
  </div>
  <div style="margin-bottom:4px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
    <a href="/" style="display:inline-block;background:#1e293b;color:#e2e8f0;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;border:1px solid #334155;">
      ← NA Sightings
    </a>
    <a href="/insights{_family_query_suffix(family)}" style="display:inline-block;background:#1d4ed8;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      📊 NA Insights
    </a>
    <a href="/eu-insights{_family_query_suffix(family)}" style="display:inline-block;background:#6d28d9;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
      📊 EU Insights
    </a>
    {_watch_pill_html()}
  </div>
  </div> <!-- /sticky-top -->

  {opportunity_html}

  <!-- Pagination controls (top) -->
  {_pagination_html(page, total_pages, per_page, total_sightings)}

  <table>
    <thead>
      <tr>
        <th>Tail</th><th>Type</th><th>From</th><th>To</th>
        <th>Distance</th><th>Duration</th><th>Block</th><th>Fuel</th><th>Programs</th><th>Climb</th><th>Arrived (UTC)</th><th>Local time</th><th>Operator</th><th>Source</th><th>Link</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <!-- Pagination controls (bottom) -->
  {_pagination_html(page, total_pages, per_page, total_sightings)}

  {_chat_widget_html()}
</body>
</html>"""
    return html


def _plan_rule_badge(rule: str) -> str:
    meta = sales_plan.rule_meta(rule)
    return (f'<span style="background:{meta["color"]}22;color:{meta["color"]};'
            f'border:1px solid {meta["color"]}66;padding:2px 9px;border-radius:4px;'
            f'font-size:11px;font-weight:700;white-space:nowrap;">{meta["label"]}</span>')


def _plan_signal_badges(signals: list) -> str:
    colors = {"HOT RANGE": "#f97316", "WARM RANGE": "#eab308",
              "FUEL STOP": "#3b82f6", "FUEL STOPS": "#3b82f6",
              "HIGH-DA ARPT": "#ef4444", "WAT": "#22c55e", "OEI": "#22c55e"}
    out = []
    for s in signals:
        col = next((v for k, v in colors.items() if k in s), "#94a3b8")
        out.append(f'<span style="background:{col}22;color:{col};border:1px solid {col}55;'
                   f'padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;'
                   f'white-space:nowrap;">{s}</span>')
    return " ".join(out)


def _plan_card(p: dict, compact: bool = False) -> str:
    """Render one prospect as a preflight card for the /plan page."""
    from html import escape as _esc
    tail   = p["tail_number"]
    meta   = sales_plan.rule_meta(p["rule"])
    owner  = p.get("owner_name") or "—"
    contact = (p.get("contact_name") or "").strip()
    phone  = (p.get("phone") or "").strip()
    phone_clean = "".join(ch for ch in phone if ch.isdigit() or ch == "+")

    texts = teams_notifier._suggest_texts(p)
    t1 = texts[0] if texts else ""

    # Contact line — tap-to-call + tap-to-text (prefilled with the first script)
    contact_bits = []
    if contact:
        contact_bits.append(f'<strong style="color:#e2e8f0;">{_esc(contact)}</strong>')
    if phone_clean:
        contact_bits.append(f'📞 <a href="tel:{phone_clean}" style="color:#60a5fa;">{_esc(phone)}</a>')
        from urllib.parse import quote as _q
        contact_bits.append(
            f'<a href="sms:{phone_clean}?&body={_q(t1)}" '
            f'style="color:#22c55e;font-weight:600;">💬 Text</a>')
    contact_html = (' &nbsp;·&nbsp; '.join(contact_bits)
                    if contact_bits else
                    ('<span style="color:#94a3b8;">No cached contact — '
                     f'<a href="#" onclick="penrich(\'{tail}\');return false;" '
                     'style="color:#f59e0b;">Enrich from JETNET</a></span>'
                     if config.JETNET_ACTIVE else
                     '<span style="color:#64748b;">No contact on file</span>'))

    # Talk-track
    points = "".join(f'<li style="margin-bottom:2px;">{_esc(pt)}</li>' for pt in meta["points"])
    talk = (f'<div style="margin-top:8px;font-size:12px;">'
            f'<span style="color:{meta["color"]};font-weight:700;">{_esc(meta["angle"])}</span>'
            f'<ul style="margin:4px 0 0 18px;color:#cbd5e1;line-height:1.5;">{points}</ul></div>')

    # Scripts with copy buttons
    script_html = ""
    for j, t in enumerate(texts, 1):
        script_html += (
            f'<div style="display:flex;gap:8px;align-items:flex-start;margin-top:6px;">'
            f'<button class="copybtn" data-text="{_esc(t)}" onclick="copyText(this)" '
            f'style="flex:0 0 auto;background:#0f172a;color:#94a3b8;border:1px solid #334155;'
            f'padding:3px 8px;border-radius:4px;font-size:10px;cursor:pointer;">Copy</button>'
            f'<span style="font-size:12px;color:#e2e8f0;line-height:1.5;">'
            f'<span style="color:#64748b;">Text {j}:</span> {_esc(t)}</span></div>')

    # Ask-AI nudge
    ai_q = f"who else does {p.get('operator') or owner} operate and how do they fly"
    ai_btn = (f'<button onclick="askAI(\'{_esc(ai_q)}\')" '
              f'style="margin-top:8px;background:#1d4ed8;color:#fff;border:none;'
              f'padding:4px 10px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer;">'
              f'💬 Ask AI about this operator</button>')

    # Checklist buttons
    btns = ""
    for oc in sales_plan.OUTCOMES:
        col = {"Meeting set": "#22c55e", "Not interested": "#ef4444"}.get(oc, "#334155")
        txtcol = "#fff" if oc in ("Meeting set", "Not interested") else "#cbd5e1"
        btns += (f'<button onclick="plog(\'{tail}\',\'{oc}\')" '
                 f'style="background:{col if oc in ("Meeting set","Not interested") else "#0f172a"};'
                 f'color:{txtcol};border:1px solid {col};padding:4px 9px;border-radius:5px;'
                 f'font-size:11px;font-weight:600;cursor:pointer;">{oc}</button> ')

    # Last touch line
    lt = p.get("last_touch")
    lt_html = ""
    if lt:
        when = (lt.get("created_at") or "")[:16].replace("T", " ")
        lt_html = (f'<div style="margin-top:6px;font-size:11px;color:#fbbf24;">'
                   f'Last touch: <strong>{_esc(lt.get("outcome",""))}</strong> by '
                   f'{_esc(lt.get("rep_name",""))} · {when}</div>')

    score = p.get("composite_score") or 0
    header = (
        f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">'
        f'{_plan_rule_badge(p["rule"])}'
        f'<a href="/tail/{tail}" style="color:#60a5fa;font-size:17px;font-weight:700;">{tail}</a>'
        f'<span style="color:#94a3b8;font-size:12px;">{_esc(p.get("label",""))}</span>'
        f'<span style="color:#cbd5e1;font-size:12px;">{_esc(owner)}</span>'
        f'<span style="margin-left:auto;font-size:11px;color:#64748b;">score '
        f'<strong style="color:#e2e8f0;">{score}</strong></span></div>')

    sigs = f'<div style="margin-top:6px;">{_plan_signal_badges(p.get("signals",[]))}</div>' if p.get("signals") else ""

    return (
        '<div style="background:#1e293b;border:1px solid #334155;border-left:3px solid '
        f'{meta["color"]};border-radius:8px;padding:14px 16px;margin-bottom:12px;">'
        + header + sigs
        + f'<div style="margin-top:8px;font-size:12px;">{contact_html}</div>'
        + talk + script_html + ai_btn
        + f'<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">{btns}</div>'
        + lt_html
        + '</div>'
    )


@app.get("/plan")
def plan():
    """Daily Flight Plan — the morning sales ritual (shared pool + contact log)."""
    from html import escape as _esc
    rule_raw = (request.args.get("rule", "") or "").strip().lower()
    rule = rule_raw if rule_raw in ("135", "91", "unknown") else None
    region_raw = (request.args.get("region", "") or "").strip().upper()
    region = region_raw if region_raw in ("NA", "EU_UK", "OTHER") else None

    p = sales_plan.build_plan(days=30, limit=6, rule=rule, region=region)
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    greet = f"Good morning, {user.split()[0]}" if user else "Good morning"

    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%A, %b %d")
    except Exception:
        today = datetime.now(timezone.utc).strftime("%A, %b %d")

    g = p["goal"]
    goal_bar = (
        f'<div style="background:#1e293b;border-radius:8px;padding:14px 18px;margin-bottom:16px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
        f'<span style="font-size:13px;color:#cbd5e1;font-weight:600;">Team goal — {g["count"]} / {g["goal"]} touches today</span>'
        f'<span style="font-size:11px;color:#94a3b8;">{"🔥 goal hit!" if g["count"]>=g["goal"] else "keep going"}</span></div>'
        f'<div style="background:#0f172a;border-radius:6px;height:10px;overflow:hidden;">'
        f'<div style="background:linear-gradient(90deg,#f97316,#22c55e);height:100%;width:{g["pct"]}%;"></div></div>'
        + (f'<div style="margin-top:8px;font-size:11px;color:#94a3b8;">'
           + " · ".join(f'{_esc(k)}: <strong style="color:#e2e8f0;">{v}</strong>' for k, v in g["by_rep"].items())
           + '</div>' if g["by_rep"] else "")
        + '</div>'
    )

    # Rule filter tabs
    rc = p["rule_counts"]
    def _tab(key, label, count):
        active = (rule or "all") == key
        href = "/plan" if key == "all" else f"/plan?rule={key}"
        style = ("background:#3b82f6;color:#fff;" if active
                 else "background:#1e293b;color:#94a3b8;border:1px solid #334155;")
        return (f'<a href="{href}" style="padding:6px 12px;border-radius:6px;font-size:13px;'
                f'font-weight:600;text-decoration:none;{style}">{label}'
                + (f' <span style="opacity:0.7;">({count})</span>' if count is not None else '')
                + '</a>')
    tabs = (
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 14px;'
        'background:#0a1626;border:1px solid #3b82f655;border-left:4px solid #3b82f6;'
        'border-radius:8px;margin-bottom:16px;">'
        '<span style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px;'
        'font-weight:700;">Operating rule</span>'
        + _tab("all", "All", rc["135"] + rc["91"] + rc["unknown"])
        + _tab("135", "Part 135", rc["135"])
        + _tab("91", "Part 91", rc["91"])
        + _tab("unknown", "Unknown", rc["unknown"])
        + '<span style="font-size:11px;color:#64748b;margin-left:auto;max-width:360px;line-height:1.4;">'
        '135 = charter/managed (revenue story) · 91 = owner-flown (mission story). '
        'Unknown tails haven\'t been enriched from JETNET yet.</span></div>'
    )

    # Trip clusters
    cluster_cards = ""
    for c in p["clusters"]:
        reps = ", ".join(r.split()[0] for r in c["reps"])
        cluster_cards += (
            f'<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
            f'padding:12px 14px;min-width:180px;flex:1;">'
            f'<div style="font-size:16px;font-weight:700;color:#e2e8f0;">{c["icao"]}</div>'
            f'<div style="font-size:12px;color:#f97316;font-weight:600;margin:2px 0;">'
            f'{c["n_tails"]} CJs · {c["n_flights"]} flights</div>'
            f'<div style="font-size:11px;color:#94a3b8;">Good swing for <strong style="color:#cbd5e1;">{_esc(reps)}</strong>'
            f'<br>{c["base"]} · ~{c["dist_nm"]:,} nm</div></div>')
    clusters_html = (
        '<h2>🗺 Trip suggestions — where the aircraft are</h2>'
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;">{cluster_cards}</div>'
    ) if cluster_cards else ""

    # Prospect sections
    call_html = "".join(_plan_card(x) for x in p["call_list"]) or \
        '<div style="color:#94a3b8;padding:16px;">No fresh prospects for this filter. Try All, or check follow-ups below.</div>'
    follow_html = "".join(_plan_card(x, compact=True) for x in p["followups"])
    won_html = "".join(_plan_card(x, compact=True) for x in p["won"])

    # Recent team activity
    recent_rows = ""
    for t in p["recent"]:
        when = (t.get("created_at") or "")[:16].replace("T", " ")
        oc = t.get("outcome", "")
        oc_col = {"Meeting set": "#22c55e", "Not interested": "#ef4444"}.get(oc, "#cbd5e1")
        recent_rows += (
            f'<tr><td style="color:#94a3b8;font-size:11px;">{when}</td>'
            f'<td><a href="/tail/{t.get("tail_number","")}" style="color:#60a5fa;">{t.get("tail_number","")}</a></td>'
            f'<td style="color:{oc_col};font-weight:600;">{_esc(oc)}</td>'
            f'<td style="color:#cbd5e1;">{_esc(t.get("rep_name",""))}</td></tr>')
    recent_html = (
        '<h2>📇 Recent team activity</h2>'
        '<table><thead><tr><th>When (UTC)</th><th>Tail</th><th>Outcome</th><th>Rep</th></tr></thead>'
        f'<tbody>{recent_rows}</tbody></table>'
    ) if recent_rows else ""

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Daily Flight Plan — A320/737 Sightings</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif;padding:24px;max-width:1100px;margin:0 auto}}
    h1{{font-size:24px;font-weight:bold;margin-bottom:2px}}
    h2{{font-size:15px;font-weight:600;color:#cbd5e1;margin:26px 0 12px;border-bottom:1px solid #1e3a5f;padding-bottom:6px}}
    .sub{{color:#94a3b8;font-size:13px;margin-bottom:18px}}
    .nav{{margin-bottom:20px}}
    a{{color:#60a5fa;text-decoration:none}}
    table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden}}
    th{{background:#0f172a;padding:8px 10px;text-align:left;font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}}
    td{{padding:8px 10px;font-size:12px;border-bottom:1px solid #0f172a}}
    .callout{{background:#1e293b;border-left:3px solid #6366f1;border-radius:6px;padding:12px 16px;margin-bottom:12px;font-size:13px;color:#e2e8f0;line-height:1.5}}
  </style>
</head>
<body>
  <div class="nav"><a href="/">← Sightings</a> &nbsp;·&nbsp; <a href="/prospects">🎯 Prospects</a> &nbsp;·&nbsp; <a href="/insights">📊 Insights</a></div>
  <h1>🛫 Daily Flight Plan</h1>
  <div class="sub">{greet} · {today} · shared call/text list · everyone sees every touch</div>

  {goal_bar}

  <div class="callout">💡 <strong>Observation of the day:</strong> {_esc(p["observation"])}</div>
  <div class="callout" style="border-left-color:#f59e0b;">🎯 <strong>Coaching:</strong> {_esc(p["coaching"])}</div>

  {clusters_html}

  {tabs}

  <h2>📞 Today's call / text list <span style="font-size:12px;color:#64748b;font-weight:400;">— untouched, work these first</span></h2>
  {call_html}

  {("<h2>🔁 Follow-ups <span style='font-size:12px;color:#64748b;font-weight:400;'>— you already reached out, circle back</span></h2>" + follow_html) if follow_html else ""}

  {("<h2>✅ Meetings set</h2>" + won_html) if won_html else ""}

  {recent_html}

  <script>
    async function plog(tail, outcome){{
      try{{
        const r = await fetch('/plan/touch', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{tail, outcome}})}});
        if(r.status===401){{ alert('Pick who you are first (bottom-left corner), then try again.'); return; }}
        location.reload();
      }}catch(e){{ alert('Could not log touch — try again.'); }}
    }}
    async function penrich(tail){{
      try{{
        const r = await fetch('/plan/enrich', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{tail}})}});
        if(r.status===401){{ alert('Pick who you are first (bottom-left corner).'); return; }}
        location.reload();
      }}catch(e){{ alert('Enrich failed — try again.'); }}
    }}
    function copyText(btn){{ navigator.clipboard.writeText(btn.getAttribute('data-text')); const o=btn.textContent; btn.textContent='Copied'; setTimeout(()=>btn.textContent=o,1200); }}
    function askAI(q){{ if(window.chatToggle) window.chatToggle(); const i=document.getElementById('chat-input'); if(i){{ i.value=q; i.focus(); }} }}
  </script>

  {_identify_modal_html()}
  {_chat_widget_html()}
</body>
</html>"""


@app.post("/plan/touch")
def plan_touch():
    """Log one outreach touch. Requires the identify cookie (rep attribution)."""
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if not user:
        return jsonify({"error": "identify yourself first"}), 401
    data = request.get_json(silent=True) or {}
    tail = (data.get("tail") or "").strip().upper()
    outcome = (data.get("outcome") or "").strip()
    note = (data.get("note") or "").strip()
    rid = sales_plan.log_touch(tail, user, outcome, note)
    if not rid:
        return jsonify({"error": "invalid tail or outcome"}), 400
    return jsonify({"ok": True, "id": rid})


@app.post("/plan/enrich")
def plan_enrich():
    """Enrich a single tail's owner/operating-rule from JETNET on demand."""
    user = request.cookies.get(_IDENTIFY_COOKIE, "").strip()
    if not user:
        return jsonify({"error": "identify yourself first"}), 401
    if not config.JETNET_ACTIVE:
        return jsonify({"error": "JETNET not active"}), 503
    data = request.get_json(silent=True) or {}
    tail = (data.get("tail") or "").strip().upper()
    if not tail:
        return jsonify({"error": "no tail"}), 400
    try:
        import jetnet_enrichment as _je
        _je.fetch_and_store_owner(tail)
        return jsonify({"ok": True})
    except Exception as e:                                # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/prospects")
def prospects():
    return redirect("/", code=302)
    # Region toggle — same pattern as /insights.
    region_raw = (request.args.get("region", "") or "").strip().upper()
    region = region_raw if region_raw in ("NA", "EU_UK", "OTHER") else None
    region_label = {"NA": "North America", "EU_UK": "Europe / UK",
                    "OTHER": "Other"}.get(region or "", "All regions")

    rows  = database.get_prospects(days=30, region=region)
    fleets = database.get_operator_fleets(rows)

    hot_count   = sum(1 for r in rows if r["trips_hot"] > 0)
    chain_count = sum(1 for r in rows if r["n_chains"] > 0)
    da_count    = sum(1 for r in rows if r["high_da_count"] > 0)

    def _signal_badges(signals):
        colors = {"HOT RANGE":"#f97316","WARM RANGE":"#eab308",
                  "FUEL STOP":"#3b82f6","FUEL STOPS":"#3b82f6",
                  "HIGH-DA ARPT":"#ef4444","WAT":"#22c55e"}
        out = []
        for s in signals:
            col = next((v for k,v in colors.items() if k in s), "#94a3b8")
            out.append(f'<span style="background:{col}22;color:{col};border:1px solid {col}55;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;white-space:nowrap;">{s}</span>')
        return " ".join(out)

    rows_html = ""
    for i, r in enumerate(rows, 1):
        score_color = "#f97316" if r["trips_hot"] > 0 else "#eab308" if r["trips_warm"] > 0 else "#3b82f6" if r["n_chains"] > 0 else "#ef4444"
        detail_url  = f"/tail/{r['tail_number']}"
        track = f'<a href="{r["last_tracking_url"]}" target="_blank" style="color:#60a5fa;">Track</a>' if r.get("last_tracking_url") else "—"
        max_dist = r["max_distance_nm"]
        baseline = r.get("baseline_nm")
        atlas_nm = r.get("atlas_nm")
        if baseline and atlas_nm and max_dist:
            over = max_dist - baseline
            if over > 0:
                range_cell = f'<span style="color:#f97316;font-weight:600;">{max_dist:,} nm</span><br><span style="font-size:10px;color:#94a3b8;">+{over:,} over baseline</span>'
            else:
                range_cell = f'<span style="color:#eab308;">{max_dist:,} nm</span><br><span style="font-size:10px;color:#94a3b8;">{baseline-max_dist:,} nm under</span>'
        else:
            range_cell = f"{max_dist:,} nm" if max_dist else "—"

        wat_cell = ""
        if r.get("wat_gain_lb") and r["wat_gain_lb"] > 0:
            wat_cell = f'<span style="color:#22c55e;font-weight:600;">+{r["wat_gain_lb"]:,} lb</span><br><span style="font-size:10px;color:#94a3b8;">{r.get("wat_worst_icao","")}</span>'
        elif r.get("high_da_count"):
            wat_cell = f'<span style="color:#94a3b8;font-size:11px;">{r["high_da_count"]} DA arpt</span>'

        rows_html += (
            f'<tr>'
            f'<td style="color:#94a3b8;">{i}</td>'
            f'<td><span style="font-size:20px;font-weight:700;color:{score_color};">{r["composite_score"]}</span><br>'
            f'<span style="font-size:9px;color:#94a3b8;">R{r["range_score"]}+C{r["chain_score"]}+D{r["da_score"]}</span></td>'
            f'<td style="font-size:11px;">{_signal_badges(r["signals"])}</td>'
            f'<td style="font-weight:600;"><a href="{detail_url}" style="color:#60a5fa;">{r["tail_number"]}</a></td>'
            f'<td>{r["label"]}'
            + (f'<br><span style="font-size:9px;color:#64748b;">{r.get("engine","")}'
               + (' &nbsp;<span style="color:#22c55e;">FADEC</span>' if r.get("fadec") else "")
               + '</span>' if r.get("engine") else "")
            + '</td>'
            f'<td style="font-size:12px;">{r["operator"]}</td>'
            f'<td>{r["trips_hot"]}</td><td>{r["trips_warm"]}</td><td>{r["n_chains"]}</td>'
            f'<td>{range_cell}</td>'
            f'<td style="font-size:11px;">{r["best_origin"]} → {r["best_dest"]}</td>'
            f'<td>{wat_cell}</td>'
            f'<td style="font-size:11px;color:#94a3b8;">{r["last_seen"]}</td>'
            f'<td>{track}</td>'
            f'</tr>'
        )
    if not rows_html:
        rows_html = '<tr><td colspan="14" style="text-align:center;color:#666;padding:32px;">No scored prospects yet — keep the daemon running.</td></tr>'

    # Operator fleet rows
    fleet_rows = ""
    for f in fleets[:10]:
        tail_links = " ".join(f'<a href="/tail/{t}" style="color:#60a5fa;font-size:11px;">{t}</a>' for t in f["tails"][:6])
        fleet_rows += (
            f'<tr>'
            f'<td style="font-weight:600;">{f["operator"]}</td>'
            f'<td style="color:#60a5fa;font-weight:700;">{f["tail_count"]}</td>'
            f'<td style="font-size:20px;font-weight:700;color:#f97316;">{f["total_composite"]}</td>'
            f'<td>{f["ac_types"]}</td>'
            f'<td>{f["total_hot"]}</td><td>{f["total_chains"]}</td><td>{f["high_da_count"]}</td>'
            f'<td>{tail_links}</td>'
            f'</tr>'
        )
    show_fleets = bool(fleet_rows)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ATLAS Prospects — A320/737 Sightings</title>
  <meta http-equiv="refresh" content="120">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif;padding:24px}}
    h1{{font-size:24px;font-weight:bold;margin-bottom:4px}}
    h2{{font-size:15px;font-weight:600;color:#cbd5e1;margin:28px 0 12px;border-bottom:1px solid #1e3a5f;padding-bottom:6px}}
    .sub{{color:#94a3b8;font-size:13px;margin-bottom:20px}}
    .stats{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
    .stat{{background:#1e293b;border-radius:8px;padding:12px 18px;min-width:120px}}
    .stat .label{{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}}
    .stat .value{{font-size:20px;font-weight:bold;margin-top:2px}}
    table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden;margin-bottom:24px}}
    th{{background:#0f172a;padding:8px 10px;text-align:left;font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;white-space:nowrap}}
    td{{padding:8px 10px;font-size:12px;border-bottom:1px solid #0f172a;vertical-align:middle}}
    tr:hover td{{background:#253047}}
    a{{color:#60a5fa;text-decoration:none}}
    .nav{{margin-bottom:20px}}
    .legend{{background:#1e293b;border-radius:8px;padding:12px 18px;margin-bottom:20px;font-size:11px;color:#94a3b8;line-height:2}}
  </style>
</head>
<body>
  <div class="nav"><a href="/">← Sightings</a> &nbsp;·&nbsp; <a href="/insights">📊 Insights</a></div>
  <h1>🎯 ATLAS Prospect Intelligence</h1>
  <div class="sub">Composite-scored A320/737-family operators · Last 30 days · Auto-refreshes every 2 min · Filter: <strong>{region_label}</strong></div>

  <!-- Region toggle — same wiring as /insights -->
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 14px;background:#0a1626;border:1px solid #3b82f655;border-left:4px solid #3b82f6;border-radius:8px;margin:12px 0 16px;">
    <span style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;">🌐 Region</span>
    {"".join([f'<a href="/prospects{("" if k=="ALL" else "?region="+k)}" style="padding:6px 12px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;{"background:#3b82f6;color:#fff;" if (region or "ALL")==k else "background:#1e293b;color:#94a3b8;border:1px solid #334155;"}">{v}</a>' for k,v in [("ALL","All"),("NA","North America"),("EU_UK","Europe / UK"),("OTHER","Other")]])}
    <span style="font-size:11px;color:#64748b;margin-left:auto;max-width:340px;line-height:1.4;">Scores rebuild per region. EU_UK will populate as new landings resolve destinations from adsb.lol.</span>
  </div>

    <div class="legend" title="HOT = longer than baseline, WARM = near baseline, chains = likely fuel-stop pairs, high-DA = airports where takeoff performance matters." style="cursor:help;">
    <strong style="color:#e2e8f0;">Composite Score</strong> = Range (🔥 HOT +3/trip · ⚡ WARM +1/trip)
    + Chain (+5/fuel-stop chain ATLAS would eliminate)
    + High-DA (+2/unique high-altitude airport used)
    &nbsp;·&nbsp; Click any tail for full pre-call dossier.
  </div>

  <div class="stats">
        <div class="stat"><div class="label" title="Operators / tails with at least one composite ATLAS signal in the current lookback window." style="cursor:help;">Prospects</div><div class="value">{len(rows)}</div></div>
        <div class="stat"><div class="label" title="Trips longer than the flat-wing baseline for their sub-model. These are the missions where ATLAS would have removed a fuel stop." style="color:#f97316;cursor:help;">🔥 Range HOT</div><div class="value" style="color:#f97316;">{hot_count}</div></div>
        <div class="stat"><div class="label" title="Consecutive legs with a short stop in between, where the second leg continues past the stop instead of turning back." style="color:#3b82f6;cursor:help;">⛽ Fuel Chains</div><div class="value" style="color:#3b82f6;">{chain_count}</div></div>
        <div class="stat"><div class="label" title="Tails with at least one high-density-altitude or short-runway airport in their recent history." style="color:#ef4444;cursor:help;">🏔 High-DA Tails</div><div class="value" style="color:#ef4444;">{da_count}</div></div>
  </div>

  <div style="margin-bottom:14px;">
    <a href="/export/prospects.csv" style="display:inline-block;background:#1e293b;color:#94a3b8;padding:6px 14px;border-radius:6px;font-size:12px;border:1px solid #334155;">↓ Export CRM CSV</a>
  </div>

  <table>
    <thead><tr>
      <th>#</th><th>Score</th><th>Signals</th><th>Tail</th><th>Type</th><th>Operator</th>
      <th>🔥</th><th>⚡</th><th>⛽</th><th>Max Trip</th><th>Best Route</th>
      <th>WAT Gain</th><th>Last Seen</th><th>Track</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>

  {"<h2>🏢 Operator Fleet Intelligence</h2><table><thead><tr><th>Operator</th><th>Tails</th><th>Composite</th><th>Types</th><th>HOT Trips</th><th>Chains</th><th>High-DA</th><th>Tails (click for detail)</th></tr></thead><tbody>" + fleet_rows + "</tbody></table>" if show_fleets else ""}

</body>
</html>"""


@app.get("/tail/<tail>")
def tail_detail(tail: str):
    tail = tail.upper().strip()
    d = database.get_tail_detail(tail)
    if not d:
        return f"<h2 style='color:#e2e8f0;font-family:Arial;padding:40px'>No data found for {tail}</h2>", 404

    # Signal badges
    signals = []
    if d["trips_hot"]:   signals.append(f'<span title="Trips longer than the flat-wing baseline for this sub-model. These are the missions where ATLAS would have eliminated a fuel stop." style="background:#f9731622;color:#f97316;border:1px solid #f9731655;padding:3px 10px;border-radius:4px;font-weight:700;cursor:help;">🔥 {d["trips_hot"]} HOT TRIPS</span>')
    if d["trips_warm"]:  signals.append(f'<span title="Trips that are close to the flat-wing baseline but still under it. They are a range-pressure signal, not a guaranteed fuel-stop case." style="background:#eab30822;color:#eab308;border:1px solid #eab30855;padding:3px 10px;border-radius:4px;font-weight:700;cursor:help;">⚡ {d["trips_warm"]} WARM TRIPS</span>')
    if d["n_chains"]:    signals.append(f'<span title="Consecutive legs with a short stop in between, where the second leg continues past the stop instead of turning back. These are the fuel-stop cases ATLAS would have removed." style="background:#3b82f622;color:#60a5fa;border:1px solid #3b82f655;padding:3px 10px;border-radius:4px;font-weight:700;cursor:help;">⛽ {d["n_chains"]} FUEL-STOP CHAIN{"S" if d["n_chains"]>1 else ""}</span>')
    if d["wat_airports"]:signals.append(f'<span title="Airports where high density altitude or runway limits show a takeoff-performance story. These are the places where ATLAS WAT relief matters." style="background:#ef444422;color:#f87171;border:1px solid #ef444455;padding:3px 10px;border-radius:4px;font-weight:700;cursor:help;">🏔 {len(d["wat_airports"])} HIGH-DA AIRPORTS</span>')
    badges = " ".join(signals) or '<span style="color:#94a3b8">No strong signals yet</span>'

    # JETNET owner panel — cached registered owner + operator + contact. Only
    # renders when we have a cached row for this tail. Silent when JETNET is
    # off or the tail hasn't been enriched yet.
    owner_panel = ""
    try:
        import jetnet_enrichment as _je
        _orow = _je.get_owner_cached(tail)
        if _orow and (_orow.get("owner") or _orow.get("operator")):
            _owner    = (_orow.get("owner")    or "").strip()
            _operator = (_orow.get("operator") or "").strip()
            _cname    = (_orow.get("contact_name")  or "").strip()
            _ctitle   = (_orow.get("contact_title") or "").strip()
            _phone    = (_orow.get("phone")    or "").strip()
            _email    = (_orow.get("email")    or "").strip()
            _city     = (_orow.get("city")     or "").strip()
            _state    = (_orow.get("state")    or "").strip()
            _country  = (_orow.get("country")  or "").strip()
            _fetched  = (_orow.get("fetched_at") or "")[:10]

            _rows = []
            if _owner:
                _rows.append(f'<div style="margin-bottom:6px;"><span style="color:#94a3b8;font-size:11px;letter-spacing:0.05em;text-transform:uppercase;">Registered owner</span><br><strong style="color:#e2e8f0;font-size:14px;">{_owner}</strong></div>')
            if _operator and _operator.lower() != _owner.lower():
                _rows.append(f'<div style="margin-bottom:6px;"><span style="color:#94a3b8;font-size:11px;letter-spacing:0.05em;text-transform:uppercase;">Operator</span><br><strong style="color:#e2e8f0;font-size:14px;">{_operator}</strong></div>')

            # Every JETNET relationship + all phone numbers ("slippery bunch").
            _all = _je.extract_all_contacts(_orow)
            if _all:
                _cards = []
                for _c in _all:
                    _bits = []
                    for _lbl, _num in _c["phones"]:
                        _bits.append(f'📞 <a href="tel:{_num}" style="color:#60a5fa;text-decoration:none;">{_num}</a> <span style="color:#64748b;font-size:10px;">{_lbl}</span>')
                    for _em in _c["emails"]:
                        _bits.append(f'✉ <a href="mailto:{_em}" style="color:#60a5fa;text-decoration:none;">{_em}</a>')
                    if _c["location"]:
                        _bits.append(f'📍 {_c["location"]}')
                    _who = ""
                    if _c["name"]:
                        _title_html = f' <span style="color:#94a3b8;font-weight:400;">— {_c["title"]}</span>' if _c["title"] else ""
                        _who = f'<div style="color:#e2e8f0;font-size:13px;font-weight:600;">{_c["name"]}{_title_html}</div>'
                    _co = f'<div style="color:#cbd5e1;font-size:12px;">{_c["company"]}</div>' if _c["company"] else ""
                    _rel = f'<span style="display:inline-block;background:#4338ca55;color:#c7d2fe;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;padding:1px 7px;border-radius:4px;">{_c["relation"]}</span>'
                    _cards.append(
                        '<div style="border-top:1px solid #334155;padding:8px 0;">'
                        + _rel + _co + _who
                        + (f'<div style="font-size:12px;color:#cbd5e1;line-height:1.9;margin-top:2px;">{" &nbsp;·&nbsp; ".join(_bits)}</div>' if _bits else "")
                        + '</div>'
                    )
                _rows.append(
                    f'<div style="margin-top:8px;"><span style="color:#94a3b8;font-size:11px;letter-spacing:0.05em;text-transform:uppercase;">All contacts ({len(_all)})</span>'
                    + "".join(_cards) + '</div>'
                )
            else:
                # Fallback to the promoted single contact (old cached rows).
                if _cname:
                    _ctitle_html = f' <span style="color:#94a3b8;font-weight:400;">— {_ctitle}</span>' if _ctitle else ""
                    _rows.append(f'<div style="margin-bottom:6px;"><span style="color:#94a3b8;font-size:11px;letter-spacing:0.05em;text-transform:uppercase;">Contact</span><br><strong style="color:#e2e8f0;font-size:14px;">{_cname}</strong>{_ctitle_html}</div>')
                _contact_bits = []
                if _phone: _contact_bits.append(f'📞 <a href="tel:{_phone}" style="color:#60a5fa;text-decoration:none;">{_phone}</a>')
                if _email: _contact_bits.append(f'✉ <a href="mailto:{_email}" style="color:#60a5fa;text-decoration:none;">{_email}</a>')
                _loc = ", ".join(x for x in [_city, _state, _country] if x)
                if _loc: _contact_bits.append(f'📍 {_loc}')
                if _contact_bits:
                    _rows.append(f'<div style="font-size:12px;color:#cbd5e1;line-height:1.7;">{" &nbsp;·&nbsp; ".join(_contact_bits)}</div>')

            _footer = f'<div style="font-size:10px;color:#64748b;margin-top:8px;">JETNET · fetched {_fetched}</div>' if _fetched else ""
            owner_panel = (
                '<div style="background:#1e293b;border-left:3px solid #6366f1;border-radius:6px;padding:14px 18px;margin-bottom:16px;">'
                + "".join(_rows)
                + _footer
                + '</div>'
            )
        elif config.JETNET_ACTIVE:
            owner_panel = (
                '<div style="background:#1e293b;border-left:3px solid #475569;border-radius:6px;padding:10px 16px;margin-bottom:16px;color:#94a3b8;font-size:12px;">'
                'JETNET owner enrichment pending — next landing will trigger the lookup.'
                '</div>'
            )
    except Exception:                                    # noqa: BLE001
        owner_panel = ""

    # Flight table rows
    flight_rows = ""
    for f in d["flights"][:60]:
        tier_col = {"hot":"#f97316","warm":"#eab308"}.get(f["tier"] or "", "#94a3b8")
        tier_lbl = {"hot":"🔥 HOT","warm":"⚡ WARM"}.get(f["tier"] or "", "—")
        dist_str = f'{round(f["distance_nm"]):,} nm' if f["distance_nm"] else "—"
        da_str   = f'{f["da"]:,} ft DA' if f["da"] else "—"
        track    = f'<a href="{f["tracking_url"]}" target="_blank" style="color:#60a5fa;font-size:11px;">track</a>' if f.get("tracking_url") else ""
        flight_rows += (
            f'<tr>'
            f'<td style="color:#94a3b8;font-size:11px;">{(f["arrived_utc"] or "")[:10]}</td>'
            f'<td>{f["origin_icao"] or "—"}</td>'
            f'<td>{f["dest_icao"] or "—"}</td>'
            f'<td style="color:{tier_col};font-weight:600;">{tier_lbl}</td>'
            f'<td>{dist_str}</td>'
            f'<td style="font-size:11px;color:#94a3b8;">{da_str}</td>'
            f'<td style="font-size:11px;color:#94a3b8;">{f["source"]}</td>'
            f'<td>{track}</td>'
            f'</tr>'
        )

    # WAT / OEI airport benefit rows — type-specific
    wat_rows = ""
    ac_type = d.get("ac_type", "C525")
    for a in d["wat_airports"]:
        # --- C525: payload weight + temperature envelope ---
        if a.get("wat_gain") is not None or a.get("wat_flatwing") is not None:
            gain     = a.get("wat_gain", 0) or 0
            fw_lb    = a.get("wat_flatwing")
            tam_lb   = a.get("wat_tamarack")
            temp_adv = a.get("wat_temp_adv_c")
            max_oat_fw  = a.get("wat_max_oat_fw")
            max_oat_tam = a.get("wat_max_oat_tam")
            b_pay    = a.get("benefit_payload", "")
            b_temp   = a.get("benefit_temp", "")

            if gain > 0 and fw_lb and tam_lb:
                weight_cell = (
                    f'<span style="color:#f87171;font-size:12px;">{fw_lb:,} lb flat-wing</span><br>'
                    f'<strong style="color:#22c55e;font-size:13px;">{tam_lb:,} lb with ATLAS</strong><br>'
                    f'<span style="color:#22c55e;font-weight:700;font-size:13px;">+{gain:,} lb you could have carried</span>'
                )
            elif fw_lb:
                weight_cell = f'<span style="color:#94a3b8;font-size:12px;">{fw_lb:,} lb (at MTOW)</span>'
            else:
                weight_cell = '—'

            if temp_adv and temp_adv > 0 and max_oat_fw and max_oat_tam:
                temp_cell = (
                    f'<span style="color:#f87171;font-size:11px;">Flat-wing ceiling: {max_oat_fw}°C</span><br>'
                    f'<span style="color:#22c55e;font-size:11px;">ATLAS ceiling: {max_oat_tam}°C</span><br>'
                    f'<span style="color:#22c55e;font-weight:700;">+{temp_adv}°C hotter</span>'
                )
            else:
                temp_cell = '<span style="color:#94a3b8;font-size:11px;">No restriction<br>at this temp</span>'

            benefit_html = ""
            if b_pay:
                benefit_html += f'<div style="color:#e2e8f0;font-size:11px;margin-bottom:4px;">📦 {b_pay}</div>'
            if b_temp:
                benefit_html += f'<div style="color:#e2e8f0;font-size:11px;">🌡 {b_temp}</div>'

            wat_rows += (
                f'<tr>'
                f'<td><strong>{a["icao"]}</strong></td>'
                f'<td>{a["elevation_ft"]:,} ft</td>'
                f'<td style="color:#f87171;font-weight:600;">{a["da_max"]:,} ft</td>'
                f'<td>{a["oat_max_c"]}°C max / {a["oat_avg_c"]}°C avg ({a["n_obs"]} obs)</td>'
                f'<td>{weight_cell}</td>'
                f'<td>{temp_cell}</td>'
                f'<td style="font-size:11px;line-height:1.5;">{benefit_html}</td>'
                f'</tr>'
            )
        # --- C25A / C25B: OEI gradient improvement ---
        elif a.get("oei_gradient_pct") is not None:
            sev   = a.get("oei_severity", "marginal")
            sev_col = {"critical": "#f97316", "important": "#eab308", "marginal": "#94a3b8"}.get(sev, "#94a3b8")
            grad_pct = a.get("oei_gradient_pct", 13)
            b_str = a.get("oei_benefit_str", "")
            wat_rows += (
                f'<tr>'
                f'<td><strong>{a["icao"]}</strong></td>'
                f'<td>{a["elevation_ft"]:,} ft</td>'
                f'<td style="color:#f87171;font-weight:600;">{a["da_max"]:,} ft</td>'
                f'<td>{a["oat_max_c"]}°C max / {a["oat_avg_c"]}°C avg ({a["n_obs"]} obs)</td>'
                f'<td colspan="2"><span style="color:{sev_col};font-weight:700;font-size:13px;">+{grad_pct:.0f}% OEI climb gradient</span>'
                f'<br><span style="font-size:10px;color:{sev_col};text-transform:uppercase;">{sev}</span></td>'
                f'<td style="font-size:11px;color:#e2e8f0;line-height:1.5;">🛫 {b_str}</td>'
                f'</tr>'
            )
        else:
            # M2 (C25M) and other airframes don't have a certified WAT or OEI
            # table wired in. Say the honest thing instead of blaming missing
            # OAT (the OAT column is populated — we're showing it in this row).
            if ac_type == "C25M":
                msg = "M2 (C25M) — ATLAS OEI climb-gradient benefit is certified (per AFMS); need an OAT sample at this airport to compute the number"
            elif ac_type in ("C525", "C25A", "C25B"):
                msg = "WAT / OEI numbers unavailable for this DA / OAT combination (outside table envelope)"
            else:
                msg = f"WAT / OEI analysis not wired for aircraft type {ac_type or '?'}"
            wat_rows += (
                f'<tr><td><strong>{a["icao"]}</strong></td>'
                f'<td>{a["elevation_ft"]:,} ft</td>'
                f'<td style="color:#f87171;font-weight:600;">{a["da_max"]:,} ft</td>'
                f'<td>{a["oat_max_c"]}°C max ({a["n_obs"]} obs)</td>'
                f'<td colspan="3" style="color:#94a3b8;font-size:11px;">{msg}</td>'
                f'</tr>'
            )

    # Chain rows
    chain_rows = ""
    _TAGS = {
        "range_win":   ('<strong style="color:#22c55e">ATLAS NON-STOP</strong>',
                         'Combined distance exceeds flat-wing baseline but is within ATLAS range — ATLAS would have eliminated the fuel stop. Final destination keeps moving past the stop.'),
        "operational": ('<span style="color:#eab308">PAYLOAD / WAT / OPERATIONAL</span>',
                         'Combined distance is within flat-wing baseline, so the fuel stop was likely for payload, WAT, fuel pricing, crew rest, or operator preference — not pure range. Pickup/drop-off turnbacks are excluded.'),
        "beyond":      ('<span style="color:#94a3b8">BEYOND ATLAS RANGE</span>',
                         'Combined distance exceeds even ATLAS range — a fuel stop would still be required.'),
    }
    for c in d["chains"]:
        tag_html, tag_tip = _TAGS.get(c.get("atlas_advantage", "range_win"), _TAGS["range_win"])
        chain_rows += (
            f'<tr>'
            f'<td style="font-size:11px;color:#94a3b8;">{c["arrived_utc"]}</td>'
            f'<td>{c["origin_icao"]} → <strong>{c["fuel_stop_icao"]}</strong> → {c["dest_icao"]}</td>'
            f'<td><strong>{c["combined_nm"]:,} nm</strong> ({c["leg_a_nm"]}+{c["leg_b_nm"]})</td>'
            f'<td>{c["baseline_nm"]:,} nm</td>'
            f'<td>{c["ground_h"]}h on ground</td>'
            f'<td title="{tag_tip}" style="cursor:help;">{tag_html}</td>'
            f'</tr>'
        )

    baseline_str = f'{d["baseline_nm"]:,} nm baseline / {d["atlas_nm"]:,} nm with ATLAS (at MCT)' if d["baseline_nm"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{tail} — Pre-Call Brief</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif;padding:24px}}
    h1{{font-size:26px;font-weight:bold;margin-bottom:2px}}
    h2{{font-size:14px;font-weight:600;color:#cbd5e1;margin:24px 0 10px;border-bottom:1px solid #1e3a5f;padding-bottom:5px}}
    .sub{{color:#94a3b8;font-size:13px;margin-bottom:16px}}
    .stats{{display:flex;gap:12px;margin:16px 0 24px;flex-wrap:wrap}}
    .stat{{background:#1e293b;border-radius:8px;padding:12px 18px;min-width:110px}}
    .stat .label{{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}}
    .stat .value{{font-size:20px;font-weight:bold;margin-top:2px}}
    table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;margin-bottom:20px}}
    th{{background:#0f172a;padding:8px 10px;text-align:left;font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}}
    td{{padding:7px 10px;font-size:12px;border-bottom:1px solid #0f172a;vertical-align:middle}}
    tr:hover td{{background:#253047}}
    a{{color:#60a5fa;text-decoration:none}}
    .nav{{margin-bottom:20px}}
    .empty{{color:#94a3b8;padding:16px;font-size:12px}}
  </style>
</head>
<body>
  <div class="nav"><a href="/prospects">← Prospects</a> &nbsp;·&nbsp; <a href="/insights">📊 Insights</a></div>
  <h1>✈️ {tail}</h1>
  <div class="sub">
    {d["label"]}
    {f'&nbsp;·&nbsp; <span style="color:#64748b;">{d["engine"]}</span>' if d.get("engine") else ""}
    {f'&nbsp;<span style="background:#16a34a22;color:#22c55e;border:1px solid #16a34a55;padding:0 5px;border-radius:3px;font-size:10px;font-weight:700;">FADEC</span>' if d.get("fadec") else ""}
    &nbsp;·&nbsp; {d["operator"]} &nbsp;·&nbsp; {baseline_str} &nbsp;·&nbsp; Last {d["days"]} days
  </div>
  {f'<div style="background:#2d1e0a;border-left:3px solid #f59e0b;border-radius:6px;padding:12px 16px;font-size:13px;color:#fcd34d;margin-bottom:16px;">🛩️ <strong>Adjacent-tier tail</strong> — Mustang (C510) is not in ATLAS scope. Tracked here as an up-purchase signal (operator likely to move up into a CJ) and market-sizing input for a potential 510-family winglet product. Baseline / hot-warm framing below uses a rough Mustang range placeholder (~1000 nm MCT).</div>' if d.get("scope_tier") == "up" else ""}
  <div style="margin-bottom:16px;">{badges}</div>
  {owner_panel}

  <div class="stats">
        <div class="stat"><div class="label" title="Total flights with usable history in the current lookback window." style="cursor:help;">Flights tracked</div><div class="value">{d["total_flights"]}</div></div>
        <div class="stat"><div class="label" title="Trips longer than the flat-wing baseline for this sub-model. These are the missions where ATLAS would have eliminated a fuel stop." style="color:#f97316;cursor:help;">🔥 HOT trips</div><div class="value" style="color:#f97316;">{d["trips_hot"]}</div></div>
        <div class="stat"><div class="label" title="Trips that are close to the flat-wing baseline but still under it. They are a range-pressure signal, not a guaranteed fuel-stop case." style="color:#eab308;cursor:help;">⚡ WARM trips</div><div class="value" style="color:#eab308;">{d["trips_warm"]}</div></div>
        <div class="stat"><div class="label" title="Consecutive legs with a short stop in between, where the second leg continues past the stop instead of turning back. These are the fuel-stop cases ATLAS would have removed." style="color:#3b82f6;cursor:help;">⛽ Fuel chains</div><div class="value" style="color:#3b82f6;">{d["n_chains"]}</div></div>
        <div class="stat"><div class="label" title="Average airport-to-airport flight distance in the last {d['days']} days." style="cursor:help;">Avg distance</div><div class="value">{d["avg_dist_nm"]:,} nm</div></div>
        <div class="stat"><div class="label" title="Longest airport-to-airport flight distance in the last {d['days']} days." style="cursor:help;">Max distance</div><div class="value">{d["max_dist_nm"]:,} nm</div></div>
  </div>

        <div class="empty" title="Consecutive legs only count if the second leg continues past the stop instead of turning back to the origin or just shuttling a passenger." style="cursor:help;">Fuel-stop chains only count when the second leg continues past the stop in the same general direction; return-to-origin or simple pickup/drop-off turns are excluded.</div>

  {"<h2>🏔 High-DA Airport Analysis — ATLAS Benefit</h2><table><thead><tr><th>ICAO</th><th>Elevation</th><th>Max DA</th><th>Observed OAT</th><th>Payload / Weight Limit</th><th>Temp Envelope</th><th>What ATLAS gives you</th></tr></thead><tbody>" + wat_rows + "</tbody></table>" if wat_rows else ""}

    {"<h2>⛽ Fuel-Stop Chains</h2><table><thead><tr><th>Date</th><th title='Origin → fuel stop → final destination. Only shown when the final leg continues past the stop, not when the aircraft turns back to the origin or just shuttles a passenger.' style='cursor:help;'>Route via Fuel Stop</th><th>Combined Distance</th><th title='Stock (no ATLAS) real-world range for this sub-model at Max Continuous Thrust. If combined distance is above this, ATLAS could have eliminated the fuel stop.' style='cursor:help;'>Flat-wing Range (at MCT)</th><th>Ground Time</th><th title='ATLAS labels: NON-STOP means ATLAS could have eliminated the fuel stop; OPERATIONAL means the stop was probably for payload/WAT/crew/operator reasons; BEYOND means even ATLAS still needs the stop.' style='cursor:help;'>ATLAS Signal</th></tr></thead><tbody>" + chain_rows + "</tbody></table>" if chain_rows else ""}

  <h2>📋 Flight History (last {d["days"]} days)</h2>
  <table>
    <thead><tr><th>Date</th><th>Origin</th><th>Dest</th><th>Signal</th><th>Distance</th><th>Density Alt</th><th>Source</th><th></th></tr></thead>
    <tbody>{"".join([flight_rows]) or '<tr><td colspan="8" class="empty">No flights with data yet.</td></tr>'}</tbody>
  </table>
</body>
</html>"""


@app.get("/export/prospects.csv")
def export_prospects_csv():
    rows = database.get_prospects(days=30)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "rank", "composite_score", "range_score", "chain_score", "da_score",
        "signals", "tail_number", "ac_type", "label", "operator",
        "trips_hot", "trips_warm", "n_chains", "high_da_airports", "high_da_count",
        "max_distance_nm", "baseline_nm", "atlas_nm",
        "wat_gain_lb", "wat_deficit_lb", "wat_worst_icao",
        "best_origin", "best_dest", "last_seen", "detail_url",
    ])
    writer.writeheader()
    for i, r in enumerate(rows, 1):
        writer.writerow({
            "rank":             i,
            "composite_score":  r["composite_score"],
            "range_score":      r["range_score"],
            "chain_score":      r["chain_score"],
            "da_score":         r["da_score"],
            "signals":          " | ".join(r["signals"]),
            "tail_number":      r["tail_number"],
            "ac_type":          r["ac_type"],
            "label":            r["label"],
            "operator":         r["operator"],
            "trips_hot":        r["trips_hot"],
            "trips_warm":       r["trips_warm"],
            "n_chains":         r["n_chains"],
            "high_da_airports": " ".join(r["high_da_airports"]),
            "high_da_count":    r["high_da_count"],
            "max_distance_nm":  r["max_distance_nm"],
            "baseline_nm":      r.get("baseline_nm", ""),
            "atlas_nm":         r.get("atlas_nm", ""),
            "wat_gain_lb":      r.get("wat_gain_lb", ""),
            "wat_deficit_lb":   r.get("wat_deficit_lb", ""),
            "wat_worst_icao":   r.get("wat_worst_icao", ""),
            "best_origin":      r["best_origin"],
            "best_dest":        r["best_dest"],
            "last_seen":        r["last_seen"],
            "detail_url":       f"https://a320737sightings.voloaltro.tech/tail/{r['tail_number']}",
        })
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=atlas_prospects.csv"},
    )


def _request_region(default: str | None = "NA") -> str | None:
    raw = (request.args.get("region") or default or "").strip().upper()
    return raw if raw in ("NA", "EU_UK", "OTHER") else default


@app.get("/api/mission-bins")
def api_mission_bins():
    family = _normalize_family(request.args.get("family")) or "A320"
    region = _request_region("NA")
    bins = database.get_airline_mission_bins(region=region, family=family)
    return jsonify({
        "source": "A320_737_Sightings",
        "region": region,
        "family": family,
        "status": "observed_bins_pending_simulator_calibration",
        "notes": "Observed stage-length/altitude bins. Feed simulator deltas into Leasing_Model for A320 split-savings economics; Tamarack_525_Financials is for project/company run-rate costs. No fuel/WAT claims included here yet.",
        "bins": bins,
    })


@app.get("/export/mission-bins.csv")
def export_mission_bins_csv():
    family = _normalize_family(request.args.get("family")) or "A320"
    region = _request_region("NA")
    rows = database.get_airline_mission_bins(region=region, family=family)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "region", "family", "distance_bin", "altitude_bin", "flight_count",
        "representative_distance_nm", "representative_altitude_ft",
        "avg_distance_nm", "avg_altitude_ft", "sim_status",
        "flatwing_fuel_lb", "tamarack_fuel_lb", "fuel_saved_lb",
        "fuel_saved_pct", "wat_gain_lb", "annualized_savings_usd",
        "airline_share_usd", "tamarack_share_usd", "deck_evidence_note",
    ])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "region": region,
            "family": family,
            "distance_bin": r.get("distance_bin", ""),
            "altitude_bin": r.get("altitude_bin", ""),
            "flight_count": r.get("count", 0),
            "representative_distance_nm": r.get("representative_distance_nm", ""),
            "representative_altitude_ft": r.get("representative_altitude_ft", ""),
            "avg_distance_nm": r.get("avg_distance_nm", ""),
            "avg_altitude_ft": r.get("avg_altitude_ft", ""),
            "sim_status": r.get("sim_status", ""),
            "flatwing_fuel_lb": "",
            "tamarack_fuel_lb": "",
            "fuel_saved_lb": "",
            "fuel_saved_pct": "",
            "wat_gain_lb": "",
            "annualized_savings_usd": "",
            "airline_share_usd": "",
            "tamarack_share_usd": "",
            "deck_evidence_note": "Reserved for simulator output feeding Leasing_Model split-savings economics; intentionally blank until calibrated.",
        })
    fname = f"mission_bins_{region or 'ALL'}_{family}.csv".lower()
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.get("/mustangs")
def mustangs_page():
    return redirect("/", code=302)

@app.get("/mustang-insights")
def mustang_insights_page():
    return redirect("/", code=302)

@app.get("/insights")
def insights():
    return _airline_insights_html("NA", "NA Insights", "/", "NA Sightings")
    # Region toggle (top-of-page filter): 'ALL' (default), 'NA', 'EU_UK', 'OTHER'.
    region_raw = (request.args.get("region", "") or "").strip().upper()
    region = region_raw if region_raw in ("NA", "EU_UK", "OTHER") else None
    region_label = {"NA": "North America", "EU_UK": "Europe / UK",
                    "OTHER": "Other"}.get(region or "", "All regions")

    data = database.get_insights(region=region)
    if not data:
        # Empty result: distinguish "no data at all yet" from "no data for this region"
        if region:
            back_html = (
                "<div style='padding:40px;color:#e2e8f0;font-family:Arial;max-width:720px;'>"
                f"<div class='nav'><a href='/' style='color:#60a5fa;'>← Sightings</a></div>"
                f"<h2>📊 {region_label}</h2>"
                "<p style='color:#94a3b8;margin-top:12px;line-height:1.6;'>"
                "No fully-resolved landings in this region yet. adsb.lol is picking up "
                "European tails now that the global airport index is loaded, but the "
                "destination airport needs to resolve before a flight can populate "
                "distance / duration / block-speed charts. Come back after a few polls."
                "</p>"
                f"<p style='margin-top:16px;'><a href='/insights' style='background:#3b82f6;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;'>← Back to All regions</a></p>"
                "</div>"
            )
            return back_html
        return "<h2 style='color:#e2e8f0;font-family:Arial;padding:40px'>No distance data yet — keep the daemon running.</h2>"

    # Kick off background OAT enrichment (non-blocking, best-effort)
    try:
        import threading
        threading.Thread(target=database.enrich_temperatures, kwargs={"limit": 30},
                         daemon=True).start()
        threading.Thread(target=database.enrich_arrival_temperatures,
                         kwargs={"limit": 30}, daemon=True).start()
        threading.Thread(target=database.enrich_distance_validations,
                         kwargs={"limit": 10}, daemon=True).start()
    except Exception:
        pass

    chains   = database.get_fuel_stop_chains(days=90)
    perf_apt = database.get_performance_airports(da_threshold=5000, short_runway_ft=5000)
    high_da  = perf_apt["high_da"]
    short_rwy = perf_apt["short_rwy"]
    da_dist  = database.get_da_distribution()
    block_scatter  = database.get_block_speed_scatter(region=region)
    insights_by_sv = database.get_insights_by_subvariant(region=region)
    climb_cmp     = database.get_climb_comparison(min_distance_nm=500)
    pursuit       = database.get_operator_pursuit(top_n=25, days=30)
    route_map     = database.get_route_map_data(top_n=80, region=region)

    # Per-sub-variant views for the four filter-driven Range-Signal charts
    # (distChart, atlasChart, durChart, hourChart). Also includes an 'ALL'
    # pooled view identical to what get_insights() would emit.
    insights_views_js = _json.dumps(insights_by_sv["views"])
    insights_svs_js   = _json.dumps(insights_by_sv["sub_variants"])
    hour_lbl          = _json.dumps([f"{h:02d}:00" for h in range(24)])

    # Block-speed scatter datasets — per-sub-variant views with pre-fit curves.
    # `bs_views_js` schema:
    #   { "ALL":  {atlas_n, flat_n,
    #             atlas_points_d, flat_points_d, atlas_trend_d, flat_trend_d,
    #             atlas_points_h, flat_points_h, atlas_trend_h, flat_trend_h},
    #     "CJ1+": {...}, "M2": {...}, ... }
    bs_views_js = _json.dumps(block_scatter["views"])
    bs_svs_js   = _json.dumps(block_scatter["sub_variants"])

    # Climb comparison (top altitude + time-to-10k per sub-variant)
    climb_labels    = _json.dumps(climb_cmp["labels"])
    climb_top_atlas = _json.dumps([(v / 1000) if v else None for v in climb_cmp["atlas_top_ft"]])
    climb_top_other = _json.dumps([(v / 1000) if v else None for v in climb_cmp["other_top_ft"]])
    climb_ceiling   = _json.dumps([(v / 1000) if v else None for v in climb_cmp["ceiling_ft"]])

    # Route map (Leaflet) — top routes + airport coordinates
    route_airports_js = _json.dumps(route_map["airports"])
    route_routes_js   = _json.dumps(route_map["routes"])

    # Operator pursuit table HTML rows
    pursuit_rows = ""
    if pursuit:
        for i, op in enumerate(pursuit, 1):
            tail_links = " ".join(
                f'<a href="/tail/{t}" style="color:#60a5fa;font-size:11px;text-decoration:none;'
                f'background:#0f172a;padding:1px 6px;border-radius:3px;margin:1px;display:inline-block;">{t}</a>'
                for t in op["tails"][:8]
            )
            if len(op["tails"]) > 8:
                tail_links += f' <span style="color:#94a3b8;font-size:11px;">+{len(op["tails"]) - 8}</span>'
            subs = ", ".join(op["subvariants"]) or "—"
            pursuit_rows += (
                f'<tr>'
                f'<td style="text-align:center;color:#94a3b8;font-weight:600;">{i}</td>'
                f'<td><strong style="color:#e2e8f0;">{op["operator"]}</strong></td>'
                f'<td style="text-align:right;"><span style="font-size:16px;font-weight:700;color:#f97316;">{op["total_composite"]}</span></td>'
                f'<td style="text-align:center;">{op["tail_count"]}</td>'
                f'<td style="text-align:center;color:#f97316;font-weight:600;">{op["total_hot"]}</td>'
                f'<td style="text-align:center;color:#eab308;">{op["total_warm"]}</td>'
                f'<td style="text-align:center;color:#3b82f6;">{op["total_chains"]}</td>'
                f'<td style="font-size:11px;color:#94a3b8;">{subs}</td>'
                f'<td>{tail_links}</td>'
                f'</tr>'
            )
    if not pursuit_rows:
        pursuit_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:16px;">No operator signal yet — awaiting more flights.</td></tr>'

    # Template-side scalars
    climb_min_dist = climb_cmp["min_distance_nm"]
    route_top_n    = len(route_map["routes"])
    pursuit_n      = len(pursuit)
    from atlas_config import WEIGHT_HOT, WEIGHT_WARM
    weight_hot, weight_warm = WEIGHT_HOT, WEIGHT_WARM

    orig_lbl  = _json.dumps([x["icao"] for x in data["top_origins"]])
    orig_data = _json.dumps([x["count"] for x in data["top_origins"]])
    dest_lbl  = _json.dumps([x["icao"] for x in data["top_dests"]])
    dest_data = _json.dumps([x["count"] for x in data["top_dests"]])
    route_lbl = _json.dumps([x["route"] for x in data["top_routes"]])
    route_data= _json.dumps([x["count"] for x in data["top_routes"]])

    # DA distribution (high/hot takeoffs + landings)
    da_labels    = _json.dumps(da_dist["labels"])
    da_takeoffs  = _json.dumps(da_dist["takeoffs"])
    da_landings  = _json.dumps(da_dist["landings"])
    da_totals    = da_dist["totals"]
    da_pct_to    = round(100 * da_totals["takeoffs_high"] / da_totals["takeoffs"]) if da_totals["takeoffs"] else 0
    da_pct_la    = round(100 * da_totals["landings_high"] / da_totals["landings"]) if da_totals["landings"] else 0

    # Summary cards by type
    type_cards = ""
    for ac, t in data["by_type"].items():
        pct_opp = round(100 * (t["hot"] + t["warm"]) / t["count"]) if t["count"] else 0
        dur_str = f"{t['avg_dur_h']}h avg" if t["avg_dur_h"] else "—"
        block_str = f" &nbsp;·&nbsp; {t['block_kts']} kts block" if t.get("block_kts") else ""
        type_cards += f"""
        <div class="stat">
          <div class="label">{t["label"]}</div>
          <div class="value">{t["count"]} flights</div>
          <div style="font-size:12px;color:#94a3b8;margin-top:4px;">{t["avg_dist"]} nm avg &nbsp;·&nbsp; {dur_str}{block_str}</div>
          <div style="font-size:12px;color:#f59e0b;margin-top:2px;">{pct_opp}% range-limited</div>
        </div>"""

    # Chained fuel-stop table rows
    chain_rows = ""
    unique_chain_tails = len({c["tail_number"] for c in chains})
    range_win_chains = [c for c in chains if c.get("atlas_advantage") == "range_win"]
    _TAGS = {
        "range_win":   ('<span style="color:#22c55e;font-size:11px;font-weight:600;">ATLAS NON-STOP</span>',
                         'Combined distance exceeds flat-wing baseline but is within ATLAS range — ATLAS would have eliminated the fuel stop.'),
        "operational": ('<span style="color:#eab308;font-size:11px;">PAYLOAD / WAT / OPERATIONAL</span>',
                         'Combined distance is within flat-wing baseline, so the fuel stop was likely for payload, WAT, fuel pricing, crew rest, or operator preference — not pure range.'),
        "beyond":      ('<span style="color:#94a3b8;font-size:11px;">BEYOND ATLAS RANGE</span>',
                         'Combined distance exceeds even ATLAS range — a fuel stop would still be required.'),
    }
    for c in chains[:20]:
        tag_html, tag_tip = _TAGS.get(c.get("atlas_advantage", "range_win"), _TAGS["range_win"])
        chain_rows += f"""
        <tr>
          <td>{c["tail_number"]}</td>
          <td>{c["label"]}</td>
          <td>{c["origin_icao"]} → <strong>{c["fuel_stop_icao"]}</strong> → {c["dest_icao"]}</td>
          <td>{c["leg_a_nm"]} + {c["leg_b_nm"]} = <strong>{c["combined_nm"]} nm</strong></td>
          <td>{c["baseline_nm"]} nm</td>
          <td>{c["ground_h"]}h</td>
          <td title="{tag_tip}" style="cursor:help;">{tag_html}</td>
          <td style="color:#94a3b8;font-size:11px;">{c["arrived_utc"]}</td>
        </tr>"""
    if not chain_rows:
        chain_rows = '<tr><td colspan="8" style="color:#94a3b8;padding:16px;">No chained fuel stops detected yet — need more flight history.</td></tr>'

    # High-DA airport table rows
    da_rows = ""
    for a in high_da[:15]:
        if a["oat_samples"]:
            oat_str = f"{a['oat_max_c']}°C max / {a['oat_avg_c']}°C avg ({a['oat_samples']} obs)"
        else:
            oat_str = '<span style="color:#94a3b8">(ISA std — awaiting OAT data)</span>'

        if a["wat_limited"] and a["wat_gain_lb"] and a["wat_gain_lb"] > 0:
            wat_str = (f'<span style="color:#f87171">{a["wat_flatwing_lb"]:,} lb</span>'
                       f' → <span style="color:#22c55e">{a["wat_tamarack_lb"]:,} lb</span>'
                       f' <strong style="color:#22c55e">+{a["wat_gain_lb"]:,} lb ATLAS</strong>')
        elif a["wat_limited"]:
            wat_str = f'<span style="color:#f87171">{a["wat_flatwing_lb"]:,} lb (limited)</span>'
        elif a.get("wat_flatwing_lb"):
            wat_str = f'<span style="color:#94a3b8">At MTOW ({a["wat_flatwing_lb"]:,} lb)</span>'
        else:
            wat_str = '<span style="color:#94a3b8">—</span>'

        da_rows += f"""
        <tr>
          <td><strong>{a["icao"]}</strong></td>
          <td>{a["elevation_ft"]:,} ft</td>
          <td>{a["da_avg"]:,} ft</td>
          <td style="color:#f87171;font-weight:600;">{a["da_max"]:,} ft</td>
          <td style="font-size:11px;">{oat_str}</td>
          <td style="font-size:11px;">{wat_str}</td>
          <td>{a["tail_count"]} tails · {a["flight_count"]} flt</td>
        </tr>"""
    if not da_rows:
        da_rows = '<tr><td colspan="7" style="color:#94a3b8;padding:16px;">No high-DA airports detected yet.</td></tr>'

    # Short-runway airport table rows
    rwy_rows = ""
    for a in short_rwy[:15]:
        elev_str = f"{a['elevation_ft']:,} ft" if a["elevation_ft"] else "—"
        also_da = ' <span style="color:#f87171;font-size:10px;">+HIGH DA</span>' if a["also_high_da"] else ""
        rwy_rows += f"""
        <tr>
          <td><strong>{a["icao"]}</strong>{also_da}</td>
          <td style="color:#fb923c;font-weight:600;">{a["longest_runway_ft"]:,} ft</td>
          <td>{elev_str}</td>
          <td>{a["tail_count"]} tails · {a["flight_count"]} flights</td>
        </tr>"""
    if not rwy_rows:
        rwy_rows = '<tr><td colspan="4" style="color:#94a3b8;padding:16px;">No short-runway airports detected yet.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Fleet Insights — A320/737 Sightings</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ background:#0f172a; color:#e2e8f0; font-family:Arial,sans-serif; padding:24px; }}
    h1 {{ font-size:24px; font-weight:bold; margin-bottom:4px; }}
    h2 {{ font-size:16px; font-weight:600; color:#cbd5e1; margin:28px 0 12px; border-bottom:1px solid #1e3a5f; padding-bottom:6px; }}
    .sub {{ color:#94a3b8; font-size:13px; margin-bottom:20px; }}
    .nav {{ margin-bottom:20px; }}
    .stats {{ display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap; }}
    .stat {{ background:#1e293b; border-radius:8px; padding:14px 18px; min-width:140px; }}
    .stat .label {{ font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; }}
    .stat .value {{ font-size:22px; font-weight:bold; margin-top:2px; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }}
    .grid-3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:20px; margin-bottom:20px; }}
    .chart-box {{ background:#1e293b; border-radius:8px; padding:20px; }}
    .chart-box h3 {{ font-size:13px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; margin-bottom:14px; }}
    .insight {{ background:#1e2d1e; border-left:3px solid #22c55e; border-radius:6px; padding:12px 16px; font-size:13px; color:#86efac; margin-bottom:8px; }}
    .insight.amber {{ background:#2d1e0a; border-left-color:#f59e0b; color:#fcd34d; }}
    .insight.blue  {{ background:#0a1a2d; border-left-color:#3b82f6; color:#93c5fd; }}
    .insight.red   {{ background:#2d0a0a; border-left-color:#ef4444; color:#fca5a5; }}
    a {{ color:#60a5fa; text-decoration:none; }}
    table.data {{ width:100%; border-collapse:collapse; font-size:12px; }}
    table.data th {{ color:#94a3b8; text-transform:uppercase; font-size:10px; letter-spacing:1px; padding:8px 10px; text-align:left; border-bottom:1px solid #1e3a5f; }}
    table.data td {{ padding:7px 10px; border-bottom:1px solid #0f1f35; vertical-align:middle; }}
    table.data tr:hover td {{ background:#1e293b; }}
    @media(max-width:900px) {{ .grid,.grid-3 {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <h1>📊 Fleet Mission Intelligence</h1>
  <div class="sub">A320/737-family analytics · {data["total_flights"]} flights · ATLAS winglet opportunity signals · Filter: <strong>{region_label}</strong></div>

  <!-- Region toggle — top-level filter, drives block-speed + Range-Signal charts -->
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 14px;background:#0a1626;border:1px solid #3b82f655;border-left:4px solid #3b82f6;border-radius:8px;margin:12px 0 16px;">
    <span style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;">🌐 Region</span>
    {"".join([f'<a href="/insights{("" if k=="ALL" else "?region="+k)}" style="padding:6px 12px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;{"background:#3b82f6;color:#fff;" if (region or "ALL")==k else "background:#1e293b;color:#94a3b8;border:1px solid #334155;"}">{v}</a>' for k,v in [("ALL","All"),("NA","North America"),("EU_UK","Europe / UK"),("OTHER","Other")]])}
    <span style="font-size:11px;color:#64748b;margin-left:auto;max-width:340px;line-height:1.4;">Regions are classified from origin/dest ICAO (US/CA/MX = NA · EU-27 + UK + EFTA = EU_UK). Transatlantic legs bucket to Other.</span>
  </div>

  <!-- ATLAS value proposition callouts -->
  <div style="margin-bottom:20px;">
    <div class="insight">
      ✈️ Average A320/737-family mission: <strong>{data["avg_distance"]} nm</strong>
      {f"· <strong>{data['avg_duration_h']}h</strong> avg flight time" if data.get("avg_duration_h") else ""}
      {f"· <strong>{data['avg_ground_kts']} kts</strong> avg ground speed" if data.get("avg_ground_kts") else ""}
      · Longest recorded: <strong>{data["max_distance"]} nm</strong>
    </div>
    <div class="insight amber">🔥 <strong>Range:</strong> Operators flying over baseline range need a fuel stop today — ATLAS eliminates it. Range extension is certified across the in-scope CJ family: CJ / CJ1, CJ1+ / M2, CJ2, CJ2+, and CJ3 / CJ3+.</div>
    <div class="insight blue">⚖️ <strong>Payload (MZFW):</strong> ATLAS increases Max Zero Fuel Weight on the C525 — carry more on every flight, regardless of distance.</div>
    <div class="insight red">🏔️ <strong>Performance (WAT / Takeoff Gradient):</strong> ATLAS delivers certified takeoff gradient improvements and WAT table relief for the C525 (CJ) — critical at high-altitude or short-runway airports. WAT certification is C525-only; CJ1/CJ1+/M2 see similar performance gains but WAT tables are not yet certified on those models. Short flight? High airport? That tail needs winglets.</div>
  </div>

  <!-- Global sub-variant filter — controls the 4 Range Signal charts + 2 block-speed charts -->
  <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:12px 16px;background:#0a1626;border:1px solid #22c55e55;border-left:4px solid #22c55e;border-radius:8px;margin-bottom:20px;">
    <div style="display:flex;flex-direction:column;">
      <label for="svFilter" style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;">🔍 Sub-variant filter</label>
      <span style="font-size:11px;color:#22c55e;margin-top:2px;">applies to the 4 Range Signal charts + 2 Block-Speed charts below</span>
    </div>
    <select id="svFilter" style="background:#0f172a;color:#e2e8f0;border:1px solid #22c55e;border-radius:6px;padding:8px 14px;font-size:14px;font-weight:600;min-width:300px;cursor:pointer;"></select>
    <span id="svCounts" style="font-size:12px;color:#cbd5e1;font-weight:600;"></span>
    <span style="font-size:11px;color:#64748b;margin-left:auto;max-width:340px;line-height:1.4;">Why filter? CJ3/CJ3+ (~415 kt cruise, 1700 nm baseline) look nothing like CJ/CJ1 (~340 kt, 900 nm). Pool them and every average lies. Pick one model for like-for-like.</span>
  </div>

  <!-- Row 1: Distance distribution + ATLAS range opportunity -->
  <h2>📏 Range Signal</h2>
  <div class="grid">
    <div class="chart-box">
      <h3>Distance Distribution — all flights (nm)</h3>
      <canvas id="distChart" height="180"></canvas>
    </div>
    <div class="chart-box">
      <h3>Range-Limited Flights by Aircraft Type</h3>
      <canvas id="atlasChart" height="180"></canvas>
    </div>
  </div>

  <!-- Row 2: Duration + Hour of day -->
  <div class="grid">
    <div class="chart-box">
      <h3>Flight Duration Distribution (hours)</h3>
      <canvas id="durChart" height="180"></canvas>
    </div>
    <div class="chart-box">
      <h3>Arrival Hour (local time at destination) — when do CJs land?</h3>
      <canvas id="hourChart" height="180"></canvas>
    </div>
  </div>

  <!-- Row 3: Block-speed scatter (vs distance, vs duration) -->
  <h2>⚖️ Block-Speed Comparison — ATLAS vs Flat-wing</h2>
  <div class="grid">
    <div class="chart-box">
      <h3>Block Speed vs Distance — each dot is one flight (ATLAS vs Flat-wing)</h3>
      <canvas id="blockDistChart" height="180"></canvas>
    </div>
    <div class="chart-box">
      <h3>Block Speed vs Flight Duration — each dot is one flight (ATLAS vs Flat-wing)</h3>
      <canvas id="blockDurChart" height="180"></canvas>
    </div>
  </div>

  <!-- Row 4: Climb performance — top altitude + time-to-10k by sub-variant -->
  <h2>🚀 Climb Performance — ATLAS vs Flat-wing</h2>
  <div class="insight blue" style="margin-bottom:14px;">
    Long-leg flights only (≥{climb_min_dist} nm) — the slice where operators climb to true cruise altitude.
    ATLAS's climb-gradient improvement helps weak-climbing CJs reach their published ceiling and reduces time-to-10k.
  </div>
  <div class="grid">
    <div class="chart-box">
      <h3>Average Top Altitude Reached on Long Legs (kft) — vs published ceiling</h3>
      <canvas id="climbTopChart" height="180"></canvas>
    </div>
  </div>

  <!-- Row 5: Route map (Leaflet) -->
  <h2>🗺 Route Map — Top {route_top_n} A320/737-family Corridors</h2>
  <div class="insight blue" style="margin-bottom:14px;">
    Origin → destination arcs sized by traffic frequency. The heaviest corridors are the
    natural prospect targets — operators with the most fuel-stop pain along these routes
    are the first ATLAS sales calls.
  </div>
  <div id="routeMap" style="height:480px;border-radius:8px;border:1px solid #1e3a5f;margin-bottom:24px;"></div>

  <!-- Row 6: Operator pursuit list (TAM ranking) -->
  <h2>🏢 Operator Pursuit List — Top {pursuit_n} by ATLAS Signal (last 30 days)</h2>
  <div class="insight amber" style="margin-bottom:14px;">
    Operators ranked by aggregate composite score across all their tails. This is the order
    to call them in. Composite = HOT × {weight_hot} + WARM × {weight_warm} + fuel-stop chains × 5 + high-DA airports × 2.
  </div>
  <div class="chart-box" style="margin-bottom:24px;overflow-x:auto;">
    <table class="data">
      <thead><tr>
        <th>#</th><th>Operator</th><th style="text-align:right;">Composite</th>
        <th>Tails</th><th>HOT</th><th>WARM</th><th>Chains</th>
        <th>Sub-variants</th><th>Tail Links</th>
      </tr></thead>
      <tbody>{pursuit_rows}</tbody>
    </table>
  </div>

  <!-- Chained fuel-stop section -->
  <h2>⛽ Chained Fuel Stops</h2>
  <div class="insight amber" style="margin-bottom:14px;">
    {len(chains)} fuel-stop chains detected in the last 90 days across {unique_chain_tails} tails.
    <strong>{len(range_win_chains)}</strong> of those were pure range cases — combined distance exceeded flat-wing baseline, so ATLAS would have eliminated the fuel stop.
    The rest were within flat-wing range and likely stopped for payload, WAT, fuel pricing, crew rest, or operator preference.
  </div>
  <div class="chart-box" style="margin-bottom:20px;overflow-x:auto;">
    <table class="data">
      <thead><tr>
        <th>Tail</th><th>Type</th><th>Route (via fuel stop)</th>
        <th>Combined Distance</th>
        <th title="Stock (no ATLAS) real-world range for this sub-model at Max Continuous Thrust. If combined distance is above this, ATLAS could have eliminated the fuel stop." style="cursor:help;">Flat-wing Range (at MCT)</th>
        <th>Ground Time</th>
        <th>ATLAS Signal</th><th>Date</th>
      </tr></thead>
      <tbody>{chain_rows}</tbody>
    </table>
  </div>

  <!-- Performance-limited airports section -->
  <h2>🏔️ Performance-Limited Airport Activity</h2>
  <div class="insight red" style="margin-bottom:14px;">
    Tails operating from high-density-altitude or short-runway airports are <strong>weight-limited on takeoff today</strong>.
    ATLAS WAT table certification and takeoff gradient improvements directly unlock legal full-weight operations from these fields (certified on C525/CJ; performance benefit applies to all models).
    DA calculated at 120 ft/°C above ISA using recorded departure temperatures (Open-Meteo); ISA standard used where OAT not yet available.
  </div>

  <!-- High/Hot distribution: takeoffs vs landings by DA bucket -->
  <div class="chart-box" style="margin-bottom:20px;">
    <h3>High / Hot Distribution — Takeoffs vs Landings by Density Altitude</h3>
    <div style="display:flex;gap:18px;margin-bottom:10px;flex-wrap:wrap;">
      <div style="font-size:11px;color:#94a3b8;">
        Takeoffs at DA ≥ 4k ft: <strong style="color:#f97316;">{da_totals["takeoffs_high"]:,} / {da_totals["takeoffs"]:,}</strong> ({da_pct_to}%)
      </div>
      <div style="font-size:11px;color:#94a3b8;">
        Landings at DA ≥ 4k ft: <strong style="color:#f87171;">{da_totals["landings_high"]:,} / {da_totals["landings"]:,}</strong> ({da_pct_la}%)
      </div>
      <div style="font-size:11px;color:#94a3b8;">
        Observed OAT on <strong>{da_totals["takeoffs_with_oat"]:,}</strong> takeoffs and <strong>{da_totals["landings_with_oat"]:,}</strong> landings; remainder use ISA standard at field elevation.
      </div>
    </div>
    <canvas id="daDistChart" height="110"></canvas>
  </div>

  <div class="grid">
    <div class="chart-box" style="overflow-x:auto;">
      <h3>High Density-Altitude Airports (DA ≥ 5,000 ft)</h3>
      <table class="data">
        <thead><tr>
          <th>ICAO</th><th>Elevation</th><th>DA avg</th><th>DA max</th>
          <th>Observed OAT</th><th>WAT Limit — Flatwing → ATLAS</th><th>Fleet Activity</th>
        </tr></thead>
        <tbody>{da_rows}</tbody>
      </table>
    </div>
    <div class="chart-box" style="overflow-x:auto;">
      <h3>Short-Runway Airports (longest runway &lt; 5,000 ft)</h3>
      <table class="data">
        <thead><tr>
          <th>ICAO</th><th>Longest Runway</th><th>Elevation</th><th>Fleet Activity</th>
        </tr></thead>
        <tbody>{rwy_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- Row 3: Top origins + destinations + routes -->
  <h2>🗺️ Route Intelligence</h2>
  <div class="grid-3">
    <div class="chart-box">
      <h3>Top Origin Airports</h3>
      <canvas id="origChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <h3>Top Destination Airports</h3>
      <canvas id="destChart" height="220"></canvas>
    </div>
    <div class="chart-box">
      <h3>Top Routes</h3>
      <canvas id="routeChart" height="220"></canvas>
    </div>
  </div>

<script>
const C = Chart.defaults;
C.color = '#94a3b8';
C.borderColor = '#1e293b';

// Standard axis title style (same for every chart).
const axisTitle = (text) => ({{display: !!text, text, color:'#cbd5e1',
                              font:{{size:11, weight:'600'}}}});

function bar(id, labels, datasets, opts={{}}, xLabel='', yLabel='Flights') {{
  return new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{ labels, datasets }},
    options: {{
      responsive:true,
      plugins:{{ legend:{{ display: datasets.length>1 }}}},
      scales:{{
        x:{{ grid:{{color:'#1e3a5f'}}, title: axisTitle(xLabel) }},
        y:{{ grid:{{color:'#1e3a5f'}}, beginAtZero:true,
             ticks:{{ precision: 0 }}, title: axisTitle(yLabel) }}
      }},
      ...opts
    }}
  }});
}}

// Distribution histogram: Y-axis shows percentage, tooltip shows "X.X% (N flights)"
function barPct(id, labels, pctData, countData, color, xLabel='', yLabel='% of flights') {{
  const ds = [{{data: pctData, backgroundColor: color, borderRadius:3}}];
  return new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{ labels, datasets: ds }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{
          label: (ctx) => `${{ctx.parsed.y.toFixed(1)}}% (${{countData[ctx.dataIndex].toLocaleString()}} flights)`
        }}}}
      }},
      scales: {{
        x: {{ grid: {{color:'#1e3a5f'}}, title: axisTitle(xLabel) }},
        y: {{ grid: {{color:'#1e3a5f'}}, beginAtZero: true,
              ticks: {{ callback: (v) => v + '%' }},
              title: axisTitle(yLabel) }}
      }}
    }}
  }});
}}

function hbar(id, labels, data, color, xLabel='Flights', yLabel='') {{
  new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{ labels, datasets:[{{ data, backgroundColor:color }}] }},
    options: {{
      indexAxis:'y',
      responsive:true,
      plugins:{{ legend:{{display:false}}}},
      scales:{{
        x:{{ grid:{{color:'#1e3a5f'}}, beginAtZero:true,
             ticks:{{ precision: 0 }}, title: axisTitle(xLabel) }},
        y:{{ grid:{{color:'#1e3a5f'}}, title: axisTitle(yLabel) }}
      }}
    }}
  }});
}}

// Distance-histogram bucket colors (orange >= 1200 nm, amber >= 1000 nm)
function distColorsFor(labels) {{
  return labels.map(l => {{
    const start = parseInt(l);
    return start >= 1200 ? 'rgba(249,115,22,0.7)' : start >= 1000 ? 'rgba(234,179,8,0.7)' : 'rgba(96,165,250,0.5)';
  }});
}}

// Global sub-variant filter — drives the 4 Range-Signal charts + 2 Block-Speed
// scatters. All 6 charts are (re)built from precomputed per-sub-variant views
// on every filter change; simpler than mutating closure-captured tooltip
// callbacks in the barPct helper.
(function() {{
  const iv       = {insights_views_js};
  const iSvs     = {insights_svs_js};
  const bsv      = {bs_views_js};
  const bsSvs    = {bs_svs_js};
  const hourLbl  = {hour_lbl};

  const sel     = document.getElementById('svFilter');
  const countEl = document.getElementById('svCounts');

  // Union of the two source sub-variant lists, preserving ALL-first order.
  const svsOrdered = ['ALL'].concat(
    iSvs.filter(s => s !== 'ALL'),
    bsSvs.filter(s => s !== 'ALL' && !iSvs.includes(s))
  );
  svsOrdered.forEach(sv => {{
    const opt = document.createElement('option');
    opt.value = sv;
    const n = (iv[sv] && iv[sv].total_flights) || 0;
    opt.textContent = sv === 'ALL'
      ? `All sub-variants (mixed)  \u00b7  ${{n.toLocaleString()}} flights`
      : `${{sv}}  \u00b7  ${{n.toLocaleString()}} flights`;
    sel.appendChild(opt);
  }});

  let distCh=null, atlasCh=null, durCh=null, hourCh=null, bsDistCh=null, bsDurCh=null;

  function bsBuild(view, axis) {{
    if (!view) return [];
    const flatPts  = view[`flat_points_${{axis}}`]  || [];
    const atlasPts = view[`atlas_points_${{axis}}`] || [];
    const flatTr   = view[`flat_trend_${{axis}}`];
    const atlasTr  = view[`atlas_trend_${{axis}}`];
    const datasets = [
      {{label:'Flat-wing',   data:flatPts,  type:'scatter', backgroundColor:'rgba(96,165,250,0.35)', pointRadius:2.5, pointHoverRadius:4, order:3}},
      {{label:'ATLAS fleet', data:atlasPts, type:'scatter', backgroundColor:'rgba(34,197,94,0.55)',  pointRadius:3,   pointHoverRadius:5, order:2}},
    ];
    if (flatTr)  datasets.push({{label:'Flat-wing trend', data:flatTr,  type:'line', borderColor:'rgba(96,165,250,0.95)', borderWidth:2.5, pointRadius:0, fill:false, borderDash:[6,4], tension:0.3, order:1}});
    if (atlasTr) datasets.push({{label:'ATLAS trend',     data:atlasTr, type:'line', borderColor:'rgba(34,197,94,1)',     borderWidth:3,   pointRadius:0, fill:false,                   tension:0.3, order:0}});
    return datasets;
  }}

  function makeBSChart(id, axis, xLabel, view) {{
    return new Chart(document.getElementById(id), {{
      type: 'scatter',
      data: {{ datasets: bsBuild(view, axis) }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{position:'top', labels:{{boxWidth:12, font:{{size:11}}, filter:(item)=>!item.text.endsWith('trend')}}}},
          tooltip: {{callbacks:{{label:(ctx)=>`${{ctx.dataset.label}}: ${{ctx.parsed.y}} kts @ ${{ctx.parsed.x}} ${{xLabel.includes('hour')?'hr':'nm'}}`}}}}
        }},
        scales: {{
          x: {{type:'linear', grid:{{color:'#1e3a5f'}}, title: axisTitle(xLabel)}},
          y: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Block speed (kts)')}},
        }},
      }},
    }});
  }}

  function render(sv) {{
    const view   = iv[sv]  || iv.ALL;
    const bsView = bsv[sv] || bsv.ALL;
    [distCh, atlasCh, durCh, hourCh, bsDistCh, bsDurCh].forEach(c => c && c.destroy());

    distCh  = barPct('distChart',  view.dist_hist_labels, view.dist_hist_pct, view.dist_hist_data,
                     distColorsFor(view.dist_hist_labels), 'Distance (nm)', '% of flights');
    atlasCh = bar('atlasChart', view.type_labels, [
      {{label:'🔥 HOT (over baseline)', data:view.hot_data,   backgroundColor:'rgba(249,115,22,0.8)'}},
      {{label:'⚡ WARM (80-100%)',      data:view.warm_data,  backgroundColor:'rgba(234,179,8,0.7)'}},
      {{label:'Below 80%',              data:view.other_data, backgroundColor:'rgba(96,165,250,0.3)'}},
    ], {{scales:{{
         x:{{stacked:true, title: axisTitle('Sub-variant')}},
         y:{{stacked:true, grid:{{color:'#1e3a5f'}}, beginAtZero:true, title: axisTitle('Flights')}}
       }}}});
    durCh   = barPct('durChart',  view.dur_hist_labels, view.dur_hist_pct, view.dur_hist_data,
                     'rgba(34,197,94,0.6)', 'Flight duration (hours)', '% of flights');
    hourCh  = barPct('hourChart', hourLbl, view.hour_dist_pct, view.hour_dist,
                     'rgba(168,85,247,0.6)', 'Hour (local time at destination)', '% of flights');
    bsDistCh = makeBSChart('blockDistChart', 'd', 'Distance (nm)',           bsView);
    bsDurCh  = makeBSChart('blockDurChart',  'h', 'Flight duration (hours)', bsView);

    const bsn = bsView || {{atlas_n:0, flat_n:0}};
    const nAll = (view.total_flights || 0).toLocaleString();
    countEl.textContent = `\u00b7 ${{nAll}} flights  \u00b7  Speed compare: ATLAS n=${{(bsn.atlas_n||0).toLocaleString()}} \u00b7 Flat-wing n=${{(bsn.flat_n||0).toLocaleString()}}`;
  }}

  sel.addEventListener('change', e => render(e.target.value));
  render('ALL');
}})();

// Horizontal bar charts
hbar('origChart',  {orig_lbl},  {orig_data},  'rgba(96,165,250,0.7)', 'Departures', 'Origin ICAO');
hbar('destChart',  {dest_lbl},  {dest_data},  'rgba(34,197,94,0.7)',  'Arrivals',   'Destination ICAO');
hbar('routeChart', {route_lbl}, {route_data}, 'rgba(249,115,22,0.7)', 'Flights',    'Route');

// High/Hot distribution — takeoffs vs landings, grouped bars per DA bucket
bar('daDistChart', {da_labels}, [
  {{label:'Takeoffs (origin)', data:{da_takeoffs}, backgroundColor:'rgba(249,115,22,0.75)', borderRadius:3}},
  {{label:'Landings (dest)',   data:{da_landings}, backgroundColor:'rgba(248,113,113,0.75)', borderRadius:3}},
], {{}}, 'Density altitude (ft)', 'Flights');

// Climb performance — top altitude reached on long legs, ATLAS vs Flat-wing
// Published ceiling overlaid as a thin red line.
new Chart(document.getElementById('climbTopChart'), {{
  data: {{
    labels: {climb_labels},
    datasets: [
      {{type:'bar', label:'Flat-wing',   data:{climb_top_other}, backgroundColor:'rgba(96,165,250,0.75)', borderRadius:3, order:2}},
      {{type:'bar', label:'ATLAS fleet', data:{climb_top_atlas}, backgroundColor:'rgba(34,197,94,0.85)',  borderRadius:3, order:1}},
      {{type:'line', label:'Published ceiling', data:{climb_ceiling}, borderColor:'rgba(248,113,113,0.9)', backgroundColor:'rgba(248,113,113,0.9)', borderWidth:2, borderDash:[6,4], pointRadius:4, fill:false, tension:0}},
    ],
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{position:'top', labels:{{boxWidth:12, font:{{size:11}}}}}} }},
    scales: {{
      x: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Sub-variant')}},
      y: {{grid:{{color:'#1e3a5f'}}, beginAtZero:true, title: axisTitle('Top altitude (× 1,000 ft)')}},
    }},
  }},
}});

// Route map — Leaflet with origin/destination markers and weighted lines
(function() {{
  const airports = {route_airports_js};
  const routes   = {route_routes_js};
  if (!airports.length) return;
  const map = L.map('routeMap', {{ zoomControl: true, scrollWheelZoom: false }})
                .setView([39, -98], 4);
  // Esri Dark Gray Canvas — free, no API key, no watermark. Carto's
  // basemaps.cartocdn.com now waterstamps anonymous tiles (Aug 2026 policy change).
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
    attribution: 'Tiles &copy; Esri',
    maxZoom: 16,
  }}).addTo(map);

  // Index airports by ICAO for quick lookup
  const idx = {{}};
  airports.forEach(a => {{ idx[a.icao] = a; }});

  // Compute max count for line-weight scaling
  const maxN = routes.reduce((m, r) => Math.max(m, r.count), 1);

  // Draw routes as straight polylines (great-circle distortion is small on continental scale)
  routes.forEach(r => {{
    const o = idx[r.o], d = idx[r.d];
    if (!o || !d) return;
    const w = 1 + 4 * (r.count / maxN);
    const opacity = 0.35 + 0.55 * (r.count / maxN);
    L.polyline([[o.lat, o.lon], [d.lat, d.lon]], {{
      color: '#f97316', weight: w, opacity: opacity,
    }}).bindTooltip(`${{r.o}} → ${{r.d}} · ${{r.count}} flights`).addTo(map);
  }});

  // Airport markers — small dots
  airports.forEach(a => {{
    L.circleMarker([a.lat, a.lon], {{
      radius: 3, color: '#22c55e', fillColor: '#22c55e',
      fillOpacity: 0.9, weight: 1,
    }}).bindTooltip(a.icao).addTo(map);
  }});

  // Auto-fit to the loaded points so region filters (EU_UK / OTHER) don't
  // leave the viewport parked over the US.
  try {{
    const bounds = L.latLngBounds(airports.map(a => [a.lat, a.lon]));
    if (bounds.isValid()) {{
      map.fitBounds(bounds, {{ padding: [30, 30], maxZoom: 6 }});
    }}
  }} catch (e) {{ /* fall through to default US view */ }}
}})();
</script>
</body>
</html>"""



@app.get("/eu-insights")
def eu_insights():
    return _airline_insights_html("EU_UK", "EU Insights", "/eu", "EU Sightings")
    """
    EU-only analytics: flight-level distribution, ICAO semicircular
    rule compliance, country pairs, EU operator leaderboard, EU-scaled
    distance histogram, EU fuel-stop chains, and a NA-vs-EU comparison
    strip. Focused on ops patterns — not a filter of /insights.
    """
    data = database.get_eu_insights()
    if not data:
        return (
            "<div style='padding:40px;color:#e2e8f0;font-family:Arial;max-width:720px;'>"
            "<div class='nav'><a href='/' style='color:#60a5fa;'>← Sightings</a></div>"
            "<h2>EU Insights</h2>"
            "<p style='color:#94a3b8;margin-top:12px;line-height:1.6;'>"
            "No EU landings with a resolved destination yet. adsb.lol is picking "
            "up European tails now that the global airport index is loaded, but "
            "each flight needs a destination airport before it can populate the "
            "flight-level / country / distance charts. Come back after a few polls."
            "</p>"
            "<p style='margin-top:16px;'>"
            "<a href='/insights' style='background:#3b82f6;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;'>← Fleet Insights (all regions)</a>"
            "</p></div>"
        )

    # ── Serialize chart payloads ────────────────────────────────────────
    fl_labels_js = _json.dumps(data["fl_labels"])
    fl_east_js   = _json.dumps(data["fl_eastbound"])
    fl_west_js   = _json.dumps(data["fl_westbound"])
    dist_lbl_js  = _json.dumps(data["dist_labels"])
    dist_dat_js  = _json.dumps(data["dist_data"])
    pair_lbl_js  = _json.dumps([p["pair"] for p in data["top_pairs"]])
    pair_dat_js  = _json.dumps([p["n"]    for p in data["top_pairs"]])
    dest_lbl_js  = _json.dumps([d["iso"]  for d in data["top_dests"]])
    dest_dat_js  = _json.dumps([d["n"]    for d in data["top_dests"]])

    # ── NA vs EU comparison strip ───────────────────────────────────────
    def _delta(eu, na, unit=""):
        if eu is None or na is None:
            return ""
        d = eu - na
        sign = "+" if d > 0 else ""
        color = "#22c55e" if d > 0 else ("#f87171" if d < 0 else "#94a3b8")
        return f'<span style="color:{color};font-size:11px;margin-left:6px;">({sign}{d:g}{unit})</span>'

    cmp_html = f"""
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;background:#0a1626;border:1px solid #22c55e55;border-left:4px solid #22c55e;border-radius:8px;padding:14px 18px;margin-bottom:20px;">
      <div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;">Avg leg length</div>
        <div style="font-size:22px;font-weight:700;color:#e2e8f0;">{data['avg_distance']} <span style="font-size:12px;color:#94a3b8;">nm</span></div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px;">NA: {data.get('na_avg_distance') or '—'} nm{_delta(data['avg_distance'], data.get('na_avg_distance'), ' nm')}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;">Avg flight time</div>
        <div style="font-size:22px;font-weight:700;color:#e2e8f0;">{data.get('avg_duration_h') or '—'} <span style="font-size:12px;color:#94a3b8;">h</span></div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px;">NA: {data.get('na_avg_duration_h') or '—'} h{_delta(data.get('avg_duration_h'), data.get('na_avg_duration_h'), ' h')}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;">Avg cruise FL</div>
        <div style="font-size:22px;font-weight:700;color:#e2e8f0;">FL{data.get('avg_cruise_fl') or '—'}</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px;">NA: FL{data.get('na_avg_cruise_fl') or '—'}{_delta(data.get('avg_cruise_fl'), data.get('na_avg_cruise_fl'), ' FL')}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;">Fuel-stop chain rate</div>
        <div style="font-size:22px;font-weight:700;color:#e2e8f0;">{data['eu_chain_rate']}%</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px;">NA: {data['na_chain_rate']}%{_delta(data['eu_chain_rate'], data['na_chain_rate'], ' pp')}</div>
      </div>
    </div>
    """

    # ── ATLAS per-type cards ────────────────────────────────────────────
    type_cards = ""
    for gk, t in data["by_type"].items():
        pct_opp = round(100 * (t["hot"] + t["warm"]) / t["count"]) if t["count"] else 0
        type_cards += f"""
        <div class="stat">
          <div class="label">{t['label']}</div>
          <div class="value">{t['count']} flights</div>
          <div style="font-size:12px;color:#f59e0b;margin-top:4px;">{pct_opp}% range-limited</div>
        </div>"""
    if not type_cards:
        type_cards = '<div class="stat"><div class="label">No A320/737-family EU flights yet</div></div>'

    # ── Recent EU flights CTA (full stream lives on /eu) ────────────────
    eu_stream_total = _sightings_total_eu()

    # ── Country dest table (top 20) ─────────────────────────────────────
    dest_rows = ""
    for d in data["top_dests"]:
        dest_rows += f'<tr><td><strong>{d["iso"]}</strong></td><td style="text-align:right;">{d["n"]}</td></tr>'
    if not dest_rows:
        dest_rows = '<tr><td colspan="2" style="color:#94a3b8;padding:16px;text-align:center;">No EU landings yet.</td></tr>'

    # ── Country-pair table (top 20) ─────────────────────────────────────
    pair_rows = ""
    for p in data["top_pairs"]:
        pair_rows += f'<tr><td>{p["pair"]}</td><td style="text-align:right;">{p["n"]}</td></tr>'
    if not pair_rows:
        pair_rows = '<tr><td colspan="2" style="color:#94a3b8;padding:16px;text-align:center;">No cross-border EU pairs yet.</td></tr>'

    # ── Operator leaderboard (top 15) ───────────────────────────────────
    op_rows = ""
    for i, op in enumerate(data["top_operators"], 1):
        op_rows += f"""
        <tr>
          <td style="text-align:center;color:#94a3b8;">{i}</td>
          <td><strong>{op['name']}</strong></td>
          <td style="text-align:center;">{op['flights']}</td>
          <td style="text-align:center;">{op['tails']}</td>
          <td style="text-align:center;color:#94a3b8;">{op['countries']}</td>
        </tr>"""
    if not op_rows:
        op_rows = '<tr><td colspan="5" style="color:#94a3b8;padding:16px;text-align:center;">No EU operators identified yet.</td></tr>'

    # ── EU chain table (last 180 d) ─────────────────────────────────────
    chain_rows = ""
    for c in data["chains"][:15]:
        chain_rows += f"""
        <tr>
          <td>{c['tail_number']}</td>
          <td>{c.get('label') or c.get('ac_type', '')}</td>
          <td>{c['origin_icao']} → <strong>{c['fuel_stop_icao']}</strong> → {c['dest_icao']}</td>
          <td>{c['leg_a_nm']} + {c['leg_b_nm']} = <strong>{c['combined_nm']} nm</strong></td>
          <td>{c['baseline_nm']} nm</td>
          <td>{c['ground_h']}h</td>
          <td style="color:#94a3b8;font-size:11px;">{c.get('arrived_utc', '')}</td>
        </tr>"""
    if not chain_rows:
        chain_rows = '<tr><td colspan="7" style="color:#94a3b8;padding:16px;text-align:center;">No EU fuel-stop chains in the last 180 days.</td></tr>'

    # ── Semicircular breakdown numbers ──────────────────────────────────
    s_eo, s_ee = data["semi_east_odd"], data["semi_east_even"]
    s_wo, s_we = data["semi_west_odd"], data["semi_west_even"]
    s_tot = data["semi_total"]
    s_pct = data["semi_compliant_pct"]
    east_tot = s_eo + s_ee
    west_tot = s_wo + s_we
    east_pct = round(100.0 * s_eo / east_tot, 1) if east_tot else 0.0
    west_pct = round(100.0 * s_we / west_tot, 1) if west_tot else 0.0

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>EU Insights — A320/737 Sightings</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ background:#0f172a; color:#e2e8f0; font-family:Arial,sans-serif; padding:24px; }}
    h1 {{ font-size:24px; font-weight:bold; margin-bottom:4px; }}
    h2 {{ font-size:16px; font-weight:600; color:#cbd5e1; margin:28px 0 12px; border-bottom:1px solid #1e3a5f; padding-bottom:6px; }}
    .sub {{ color:#94a3b8; font-size:13px; margin-bottom:20px; }}
    .nav {{ margin-bottom:20px; }}
    .stats {{ display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap; }}
    .stat {{ background:#1e293b; border-radius:8px; padding:14px 18px; min-width:140px; }}
    .stat .label {{ font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; }}
    .stat .value {{ font-size:22px; font-weight:bold; margin-top:2px; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }}
    .grid-4 {{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:12px; margin-bottom:20px; }}
    .chart-box {{ background:#1e293b; border-radius:8px; padding:20px; }}
    .chart-box h3 {{ font-size:13px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; margin-bottom:14px; }}
    .insight {{ background:#0a1a2d; border-left:3px solid #3b82f6; border-radius:6px; padding:12px 16px; font-size:13px; color:#93c5fd; margin-bottom:8px; }}
    .insight.amber {{ background:#2d1e0a; border-left-color:#f59e0b; color:#fcd34d; }}
    .insight.green {{ background:#0a1f0a; border-left-color:#22c55e; color:#86efac; }}
    a {{ color:#60a5fa; text-decoration:none; }}
    table.data {{ width:100%; border-collapse:collapse; font-size:12px; }}
    table.data th {{ color:#94a3b8; text-transform:uppercase; font-size:10px; letter-spacing:1px; padding:8px 10px; text-align:left; border-bottom:1px solid #1e3a5f; }}
    table.data td {{ padding:7px 10px; border-bottom:1px solid #0f1f35; vertical-align:middle; }}
    table.data tr:hover td {{ background:#1e293b; }}
    #euFlightsTable tbody tr td {{ padding:10px 14px; border-bottom:1px solid #0f172a; font-size:13px; }}
    #euFlightsTable tbody tr:last-child td {{ border-bottom:none; }}
    #euFlightsTable tbody tr:hover td {{ background:#263148; }}
    .semi-box {{ background:#1e293b; border-radius:6px; padding:14px; }}
    .semi-box .n {{ font-size:22px; font-weight:700; }}
    .semi-box .lbl {{ font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; }}
    .semi-good {{ border-left:4px solid #22c55e; }}
    .semi-bad {{ border-left:4px solid #f87171; }}
    @media(max-width:900px) {{ .grid,.grid-4 {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">← Sightings</a> &nbsp;·&nbsp;
    <a href="/eu">EU Sightings</a> &nbsp;·&nbsp;
    <a href="/insights">📊 Fleet Insights</a> &nbsp;·&nbsp;
    <a href="/prospects">🎯 ATLAS Prospects</a> &nbsp;·&nbsp;
  </div>
  <h1>EU Insights — Flight Levels, Semicircular Rule, Country Patterns</h1>
  <div class="sub">
    {data['total_flights']:,} EU_UK flights ·
    {data['unique_tails']} tails ·
    {data['unique_operators']} operators ·
    {data['unique_countries']} countries ·
    {data['unique_pairs']} cross-border pairs
  </div>

  <!-- NA vs EU comparison strip -->
  {cmp_html}

  <div class="insight">
    <strong>Why EU flies differently:</strong> shorter legs, denser airspace, ICAO semicircular
    cruise rules everywhere above the transition altitude, RVSM FL290–FL410 with 1000 ft
    separation (2000 ft above FL410). Semicircular check below is applied to flights
    with sustained cruise ≥ FL100; below that ATC frequently vectors non-standard levels.
  </div>

  <!-- Row 0: EU stream CTA — the full paginated flight table lives on /eu -->
  <h2>✈️ Recent EU Flights</h2>
  <div class="chart-box" style="margin-bottom:20px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;">
    <div style="flex:1;min-width:280px;">
      <div style="font-size:14px;color:#e2e8f0;margin-bottom:6px;">
        The full EU/UK flight stream — same 16-column layout as the NA homepage,
        with pagination and daemon KPIs — is now a dedicated page.
      </div>
      <div style="font-size:12px;color:#94a3b8;">
        {eu_stream_total:,} EU_UK sightings in the database (all-time).
      </div>
    </div>
    <a href="/eu" style="display:inline-block;background:#4338ca;color:#fff;padding:12px 22px;border-radius:6px;font-size:14px;font-weight:700;text-decoration:none;white-space:nowrap;">
      Open EU Sightings →
    </a>
  </div>

  <!-- Row 1: Flight level histogram (split by direction) -->
  <h2>🎚 Flight-Level Distribution — Eastbound vs Westbound</h2>
  <div class="insight amber" style="margin-bottom:14px;">
    ICAO semicircular rule (Rules of the Air, Annex 2 App. 3): tracks 000°–179° → <strong>odd</strong> thousands (FL290, FL310, …).
    Tracks 180°–359° → <strong>even</strong> thousands (FL280, FL300, …). If pilots comply, you'll see eastbound bars only on odd FLs and westbound bars only on even FLs.
  </div>
  <div class="chart-box" style="margin-bottom:20px;">
    <h3>Cruise altitude (sustained_top_alt_ft) — FL200 through FL450 in 1000 ft bins</h3>
    <canvas id="flChart" height="140"></canvas>
  </div>

  <!-- Row 2: Semicircular compliance -->
  <h2>↔️ Semicircular Rule Compliance</h2>
  <div class="grid-4">
    <div class="semi-box semi-good">
      <div class="lbl">Eastbound + odd FL ✓</div>
      <div class="n" style="color:#22c55e;">{s_eo:,}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;">compliant</div>
    </div>
    <div class="semi-box semi-bad">
      <div class="lbl">Eastbound + even FL ✗</div>
      <div class="n" style="color:#f87171;">{s_ee:,}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;">non-compliant / ATC-assigned</div>
    </div>
    <div class="semi-box semi-good">
      <div class="lbl">Westbound + even FL ✓</div>
      <div class="n" style="color:#22c55e;">{s_we:,}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;">compliant</div>
    </div>
    <div class="semi-box semi-bad">
      <div class="lbl">Westbound + odd FL ✗</div>
      <div class="n" style="color:#f87171;">{s_wo:,}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;">non-compliant / ATC-assigned</div>
    </div>
  </div>
  <div class="insight green">
    Overall compliance: <strong>{s_pct}%</strong> of {s_tot:,} EU cruise legs are on the correct semicircular side.
    Eastbound compliance rate: <strong>{east_pct}%</strong> ({s_eo:,} of {east_tot:,}).
    Westbound compliance rate: <strong>{west_pct}%</strong> ({s_we:,} of {west_tot:,}).
    Non-compliant slices are almost always ATC-assigned non-standard levels for traffic conflict / TMA / free-route sequencing — not pilot error.
  </div>

  <!-- Row 3: Country activity (side by side) -->
  <h2>🗺 Country Activity</h2>
  <div class="grid">
    <div class="chart-box">
      <h3>Top 20 Country Pairs — where CJs cross EU borders</h3>
      <canvas id="pairChart" height="240"></canvas>
    </div>
    <div class="chart-box">
      <h3>Top 20 Destination Countries — where CJs land in EU</h3>
      <canvas id="destChart" height="240"></canvas>
    </div>
  </div>

  <!-- Row 4: Distance histogram (EU scaled) -->
  <h2>📏 Trip-Length Distribution — EU scale (0–1200 nm)</h2>
  <div class="insight" style="margin-bottom:14px;">
    EU legs cluster short. The NA-scaled histogram on <a href="/insights">/insights</a> stretches to 2400 nm and buries the EU signal in the first two bins.
  </div>
  <div class="chart-box" style="margin-bottom:20px;">
    <h3>Distance histogram — 50 nm bins</h3>
    <canvas id="distChart" height="140"></canvas>
  </div>

  <!-- Row 5: ATLAS math (EU-scoped) -->
  <h2>🔥 ATLAS Range Signal — EU only</h2>
  <div class="insight amber" style="margin-bottom:14px;">
    Same hot/warm/other buckets as /insights, but scored on EU flights only. EU legs are short, so most tails will land in "below 80%"; the meaningful signal here is any tail showing HOT — a European operator flying at or over the flat-wing baseline is a rare and strong ATLAS target.
  </div>
  <div class="stats">{type_cards}</div>

  <!-- Row 6: EU operator leaderboard -->
  <h2>🏢 EU Operator Leaderboard (top 15 by flight count)</h2>
  <div class="chart-box" style="margin-bottom:20px;overflow-x:auto;">
    <table class="data">
      <thead>
        <tr>
          <th style="width:40px;">#</th>
          <th>Operator</th>
          <th style="text-align:center;">Flights</th>
          <th style="text-align:center;">Tails</th>
          <th style="text-align:center;">Countries</th>
        </tr>
      </thead>
      <tbody>{op_rows}</tbody>
    </table>
  </div>

  <!-- Row 7: EU country tables (raw) -->
  <div class="grid">
    <div class="chart-box" style="overflow-x:auto;">
      <h3>Destination countries — full list</h3>
      <table class="data">
        <thead><tr><th>ISO</th><th style="text-align:right;">Landings</th></tr></thead>
        <tbody>{dest_rows}</tbody>
      </table>
    </div>
    <div class="chart-box" style="overflow-x:auto;">
      <h3>Country pairs — full list</h3>
      <table class="data">
        <thead><tr><th>Pair</th><th style="text-align:right;">Flights</th></tr></thead>
        <tbody>{pair_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- Row 8: EU fuel-stop chains -->
  <h2>⛽ EU Fuel-Stop Chains ({data['chain_count']} in last 180 days)</h2>
  <div class="insight amber" style="margin-bottom:14px;">
    Same-tail consecutive legs connected by a short ground stop, where the combined distance
    ≥ 80% of the type's flat-wing baseline. EU chains are rare given short legs — every one
    that shows up is a high-value ATLAS conversation.
  </div>
  <div class="chart-box" style="margin-bottom:20px;overflow-x:auto;">
    <table class="data">
      <thead>
        <tr>
          <th>Tail</th><th>Type</th><th>Route (via fuel stop)</th>
          <th>Combined Distance</th><th>Flat-wing Range</th>
          <th>Ground Time</th><th>Date</th>
        </tr>
      </thead>
      <tbody>{chain_rows}</tbody>
    </table>
  </div>

<script>
const C = Chart.defaults;
C.color = '#94a3b8';
C.borderColor = '#1e293b';

const axisTitle = (t) => ({{display: !!t, text: t, color:'#cbd5e1', font:{{size:11, weight:'600'}}}});

// Flight-level histogram (grouped bars) — eastbound (blue) vs westbound (orange)
new Chart(document.getElementById('flChart'), {{
  type: 'bar',
  data: {{
    labels: {fl_labels_js},
    datasets: [
      {{label:'Eastbound (000°–179°) → expect odd', data:{fl_east_js}, backgroundColor:'rgba(96,165,250,0.75)', borderRadius:2}},
      {{label:'Westbound (180°–359°) → expect even', data:{fl_west_js}, backgroundColor:'rgba(249,115,22,0.75)', borderRadius:2}},
    ],
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{position:'top', labels:{{boxWidth:12, font:{{size:11}}}}}} }},
    scales: {{
      x: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Flight level')}},
      y: {{grid:{{color:'#1e3a5f'}}, beginAtZero:true, ticks:{{precision:0}}, title: axisTitle('Flights')}},
    }},
  }},
}});

// Country-pair leaderboard (horizontal bar)
new Chart(document.getElementById('pairChart'), {{
  type: 'bar',
  data: {{
    labels: {pair_lbl_js},
    datasets: [{{data: {pair_dat_js}, backgroundColor:'rgba(34,197,94,0.7)'}}],
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{legend: {{display: false}}}},
    scales: {{
      x: {{grid:{{color:'#1e3a5f'}}, beginAtZero:true, ticks:{{precision:0}}, title: axisTitle('Flights')}},
      y: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Country pair')}},
    }},
  }},
}});

// Destination-country leaderboard (horizontal bar)
new Chart(document.getElementById('destChart'), {{
  type: 'bar',
  data: {{
    labels: {dest_lbl_js},
    datasets: [{{data: {dest_dat_js}, backgroundColor:'rgba(96,165,250,0.7)'}}],
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{legend: {{display: false}}}},
    scales: {{
      x: {{grid:{{color:'#1e3a5f'}}, beginAtZero:true, ticks:{{precision:0}}, title: axisTitle('Landings')}},
      y: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Country (ISO)')}},
    }},
  }},
}});

// EU-scaled distance histogram
new Chart(document.getElementById('distChart'), {{
  type: 'bar',
  data: {{
    labels: {dist_lbl_js},
    datasets: [{{data: {dist_dat_js}, backgroundColor:'rgba(168,85,247,0.6)', borderRadius:2}}],
  }},
  options: {{
    responsive: true,
    plugins: {{legend: {{display: false}}}},
    scales: {{
      x: {{grid:{{color:'#1e3a5f'}}, title: axisTitle('Distance (nm)')}},
      y: {{grid:{{color:'#1e3a5f'}}, beginAtZero:true, ticks:{{precision:0}}, title: axisTitle('Flights')}},
    }},
  }},
}});
</script>
</body>
</html>"""
