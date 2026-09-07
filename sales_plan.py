"""
sales_plan.py — "Daily Flight Plan": the morning sales ritual for the team.

A shared prospect pool + a team-wide contact log + trip suggestions + light
habit mechanics, built on top of ``database.get_prospects()`` and the JETNET
owner/contact cache. Consumed by the ``/plan`` web page and the 8 AM Teams
nudge. The goal is to keep the team on track, force fresh thinking, and build
outreach habits — not to be a full CRM.

Design decisions (locked 2026-09-05 with Nick):
  - Shared pool: everyone sees the same list. No per-rep assignment.
  - Contact log is team-visible; each touch is stamped with the rep's name so
    nobody double-touches a contact.
  - Anti-repetition is emergent: once a tail is touched it leaves the primary
    call list and moves to Follow-ups, so new hot tails surface each day.
  - Part 135 vs 91 drives a different talk-track (see ``rule_meta``).
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import airports
import database
import jetnet_enrichment as je

DB_PATH = Path(__file__).parent / "sightings.db"

# ── Team + territory ─────────────────────────────────────────────────────────
# Jacob Klinginsmith is intentionally excluded (not in sales, per Nick).
SALES_TEAM: list[dict] = [
    {"name": "Danny Hiner",    "role": "Sales manager",  "base": "Sandpoint"},
    {"name": "Tiara Lark",     "role": "Sales",          "base": "Las Vegas"},
    {"name": "Nolan Johnson",  "role": "Sales",          "base": "Sandpoint"},
    {"name": "Marianne Wall",  "role": "Marketing",      "base": "Sandpoint"},
    {"name": "Erik Stasiowski", "role": "Contract sales", "base": "Boston"},
    {"name": "Nick Guida",     "role": "CEO",            "base": "Sandpoint"},
]
SALES_TEAM_NAMES: list[str] = [m["name"] for m in SALES_TEAM]

# Territory anchors used only to tag trip-suggestion clusters with the nearest
# rep(s). Distances are great-circle from these airports.
TERRITORIES: list[dict] = [
    {"base": "Las Vegas",     "reps": ["Tiara Lark"],
     "anchors": ["KLAS", "KVGT", "KHND"]},
    {"base": "Sandpoint / NW", "reps": ["Nolan Johnson", "Danny Hiner", "Marianne Wall"],
     "anchors": ["KSZT", "KGEG"]},
    {"base": "Boston / NE",   "reps": ["Erik Stasiowski"],
     "anchors": ["KBED", "KBOS", "KOWD"]},
]

# ── Contact-log outcomes ─────────────────────────────────────────────────────
OUTCOMES: list[str] = [
    "Texted", "Called", "Left VM", "No answer", "Meeting set", "Not interested",
]
_OPEN_OUTCOMES = {"Texted", "Called", "Left VM", "No answer"}
_WON_OUTCOMES  = {"Meeting set"}
_DEAD_OUTCOMES = {"Not interested"}

TEAM_DAILY_GOAL = 10   # team touches/day target for the habit bar

_PACIFIC = "America/Los_Angeles"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Contact log ──────────────────────────────────────────────────────────────

def init_table() -> None:
    """Create the shared contact-log table. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_touches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tail_number  TEXT NOT NULL,
                rep_name     TEXT NOT NULL,
                outcome      TEXT NOT NULL,
                note         TEXT,
                created_at   TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_touches_tail ON sales_touches(tail_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_touches_created ON sales_touches(created_at)")


def log_touch(tail: str, rep: str, outcome: str, note: str = "") -> int:
    """Record one outreach touch. Returns the new row id (0 on bad input)."""
    tail = (tail or "").strip().upper()
    rep = (rep or "").strip()
    outcome = (outcome or "").strip()
    if not tail or not rep or outcome not in OUTCOMES:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sales_touches (tail_number, rep_name, outcome, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tail, rep, outcome, (note or "").strip()[:500], now),
        )
        return int(cur.lastrowid or 0)


def get_touches(tail: str | None = None, days: int = 30, limit: int = 500) -> list[dict]:
    """Return contact-log rows, newest first. Optionally filtered to one tail."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = "SELECT * FROM sales_touches WHERE created_at >= ?"
    params: list = [cutoff]
    if tail:
        q += " AND tail_number = ?"
        params.append(tail.strip().upper())
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, tuple(params)).fetchall()]


def latest_touch_by_tail(days: int = 60) -> dict[str, dict]:
    """Map each tail to its most-recent touch row within the window."""
    out: dict[str, dict] = {}
    for t in get_touches(days=days, limit=5000):
        out.setdefault(t["tail_number"], t)   # first seen = newest (DESC order)
    return out


def touches_today() -> list[dict]:
    """All touches since local (Pacific) midnight."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(_PACIFIC))
    except Exception:
        now_local = datetime.now(timezone.utc)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = midnight_local.astimezone(timezone.utc).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sales_touches WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def goal_status() -> dict:
    """Team habit bar: touches today vs goal, plus a per-rep breakdown."""
    today = touches_today()
    by_rep: dict[str, int] = {}
    for t in today:
        by_rep[t["rep_name"]] = by_rep.get(t["rep_name"], 0) + 1
    return {
        "count":    len(today),
        "goal":     TEAM_DAILY_GOAL,
        "pct":      min(100, round(100 * len(today) / TEAM_DAILY_GOAL)) if TEAM_DAILY_GOAL else 0,
        "by_rep":   dict(sorted(by_rep.items(), key=lambda kv: -kv[1])),
    }


# ── Owner / rule / phone helpers ─────────────────────────────────────────────

def rule_meta(rule: str) -> dict:
    """Talk-track + display metadata for a Part 135 / 91 / unknown prospect."""
    return {
        "135": {
            "label": "Part 135",
            "color": "#f59e0b",
            "angle": "Charter / managed — sell the REVENUE",
            "points": [
                "Payload = billable pounds out of hot/high fields",
                "Dispatch reliability + fewer tech stops = more trips/day",
                "Range wins add sellable city-pairs to the charter menu",
            ],
        },
        "91": {
            "label": "Part 91",
            "color": "#22c55e",
            "angle": "Owner-flown / corporate — sell the MISSION",
            "points": [
                "Nonstop to their real trips — skip the fuel stop",
                "Time + schedule certainty, not a spreadsheet",
                "Ramp presence + resale differentiation",
            ],
        },
    }.get(rule, {
        "label": "Rule ?",
        "color": "#64748b",
        "angle": "Operating rule unknown — enrich to tailor the pitch",
        "points": [
            "Tap Enrich to pull owner + operating rule from JETNET",
            "135 = revenue story · 91 = mission story",
        ],
    })


def best_phone(owner_row: dict | None) -> tuple[str, str]:
    """Return (phone, contact_name) for a cached owner row. Best-effort."""
    if not owner_row:
        return ("", "")
    phone = (owner_row.get("phone") or "").strip()
    name = (owner_row.get("contact_name") or "").strip()
    if not phone:
        for c in je.extract_all_contacts(owner_row):
            if c["phones"]:
                phone = c["phones"][0][1]
                name = name or (c.get("name") or "")
                break
    return (phone, name)


# ── Trip clusters ────────────────────────────────────────────────────────────

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065   # nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _nearest_territory(icao: str) -> tuple[dict | None, float]:
    """Closest territory (by any anchor) to an airport, plus distance in nm."""
    coords = airports.icao_coords(icao)
    if not coords:
        return (None, 1e9)
    best, best_d = None, 1e9
    for terr in TERRITORIES:
        for a in terr["anchors"]:
            ac = airports.icao_coords(a)
            if not ac:
                continue
            d = _haversine_nm(coords[0], coords[1], ac[0], ac[1])
            if d < best_d:
                best, best_d = terr, d
    return (best, best_d)


def trip_clusters(days: int = 30, top: int = 6, region: str | None = None) -> list[dict]:
    """
    Airports with the biggest concentration of distinct in-scope tails in the
    window — candidate in-person swings — each tagged with the nearest rep(s).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = """
        SELECT dest_icao,
               COUNT(DISTINCT tail_number) AS n_tails,
               COUNT(*) AS n_flights
        FROM v_sightings_dedup
        WHERE arrived_utc >= ?
          AND dest_icao IS NOT NULL AND dest_icao != ''
    """
    params: list = [cutoff]
    if region in ("NA", "EU_UK", "OTHER"):
        q += " AND region = ? "
        params.append(region)
    q += " GROUP BY dest_icao ORDER BY n_tails DESC, n_flights DESC LIMIT 60"
    with _connect() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    clusters: list[dict] = []
    for r in rows:
        icao = r["dest_icao"]
        terr, dist = _nearest_territory(icao)
        if terr is None:
            continue
        clusters.append({
            "icao":      icao,
            "n_tails":   r["n_tails"],
            "n_flights": r["n_flights"],
            "base":      terr["base"],
            "reps":      terr["reps"],
            "dist_nm":   round(dist),
        })
        if len(clusters) >= top:
            break
    return clusters


# ── Rotating coaching content ────────────────────────────────────────────────
_OBSERVATIONS = [
    "Owners hate fuel stops more than they admit. Lead with the schedule they lose, not the range number.",
    "A 135 operator hears \"payload\" as \"revenue.\" Translate every ATLAS number into billable pounds or extra legs.",
    "The tail you keep skipping is the one a competitor is calling. Work the untouched list first.",
    "Text before you call. A 2-line text with a specific observation earns the callback.",
    "\"I saw N123AB stop for fuel on a leg ATLAS flies nonstop\" beats any brochure line. Be specific.",
    "High-DA summer ops = takeoff-performance pain right now. Call the hot/high operators this week, not in October.",
    "Every operator with 2+ CJs is a fleet conversation, not a single sale. Ask who else they fly.",
    "Resale is a 91 owner's silent worry. ATLAS differentiates their airplane on the market — mention it.",
]
_COACHING = [
    "Goal: 3 touches before your first coffee. Momentum compounds.",
    "Pick one trip cluster below and block a half-day to go ramp it.",
    "Log every touch — the team can see it, so nobody double-dials the same owner.",
    "Ask the AI who else the operator flies before you dial. Walk in knowing the fleet.",
    "One new operator today. Not a follow-up — a brand-new name off the call list.",
    "End every call with a next step and a date. \"I'll send the 1-pager, call you Thursday.\"",
    "If they say no on range, pivot to payload or resale. Same airplane, different door.",
]


def _daily_index(n: int) -> int:
    return datetime.now(timezone.utc).date().toordinal() % max(n, 1)


def observation_of_the_day() -> str:
    return _OBSERVATIONS[_daily_index(len(_OBSERVATIONS))]


def coaching_of_the_day() -> str:
    return _COACHING[_daily_index(len(_COACHING))]


# ── Plan builder ─────────────────────────────────────────────────────────────

def _attach(p: dict, latest: dict[str, dict]) -> dict:
    """Attach owner cache, operating rule, phone, and last touch to a prospect."""
    tail = p["tail_number"]
    owner = je.get_owner_cached(tail)
    rl = je.operating_rule(owner) if owner else {"rule": "unknown", "basis": None}
    phone, contact = best_phone(owner)
    p["owner_row"]    = owner
    p["owner_name"]   = (owner or {}).get("owner") or p.get("operator") or "—"
    p["rule"]         = rl["rule"]
    p["rule_basis"]   = rl.get("basis")
    p["contact_name"] = contact or (owner or {}).get("contact_name") or ""
    p["phone"]        = phone
    p["last_touch"]   = latest.get(tail)
    return p


def build_plan(days: int = 30, limit: int = 6, rule: str | None = None,
               region: str | None = None) -> dict:
    """
    Assemble the daily flight plan. Returns:
      call_list  — top untouched prospects (the primary outreach list)
      followups  — prospects with an open touch (circle back)
      won        — prospects with a meeting set
      clusters   — trip suggestions
      goal       — habit bar status
      observation / coaching — rotating daily prompts
      recent     — last team touches (activity feed)
      rule_counts — {135, 91, unknown} counts across the full pool
    """
    prospects = database.get_prospects(days=days, region=region)
    latest = latest_touch_by_tail(days=90)

    for p in prospects:
        _attach(p, latest)

    rule_counts = {"135": 0, "91": 0, "unknown": 0}
    for p in prospects:
        rule_counts[p["rule"]] = rule_counts.get(p["rule"], 0) + 1

    if rule in ("135", "91", "unknown"):
        prospects = [p for p in prospects if p["rule"] == rule]

    call_list, followups, won = [], [], []
    for p in prospects:
        oc = (p.get("last_touch") or {}).get("outcome")
        if oc in _DEAD_OUTCOMES:
            continue
        if oc in _WON_OUTCOMES:
            won.append(p)
        elif oc in _OPEN_OUTCOMES:
            followups.append(p)
        else:
            call_list.append(p)

    return {
        "date":        datetime.now(timezone.utc),
        "call_list":   call_list[:limit],
        "followups":   followups[:12],
        "won":         won[:12],
        "clusters":    trip_clusters(days=days, top=6, region=region),
        "goal":        goal_status(),
        "observation": observation_of_the_day(),
        "coaching":    coaching_of_the_day(),
        "recent":      get_touches(days=7, limit=15),
        "rule_counts": rule_counts,
        "total_pool":  len(prospects),
    }
