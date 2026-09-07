"""
usage_tracker.py — Lightweight page-view logger for the A320/737 Sightings dashboard.

Since the site has no login, users self-identify once via a cookie-backed
name picker (team roster hard-coded below). Each page load is recorded to a
`page_views` SQLite table.

The daily digest (``get_daily_digest``) de-bounces the 60-second auto-refresh
into "sessions" — a session starts when a user loads a page after a gap of
>= SESSION_GAP_MINUTES from their previous page view.

Usage:
  - Call ``record_view(user_name, page)`` from the Flask before_request hook.
  - Call ``get_daily_digest()`` from the 4 PM scheduled thread.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "sightings.db"

# Hard-coded team roster — shown in the "Who are you?" picker.
# Maintain this list manually; add/remove names as team changes.
TEAM_ROSTER: list[str] = [
    "Danny Hiner",
    "Jacob Klinginsmith",
    "Marianne Wall",
    "Nick Guida",
    "Nolan Johnson",
    "Tiara Lark",
]

# A new "session" starts when a user hasn't loaded any page for this many minutes.
SESSION_GAP_MINUTES: int = 5


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    """Create the page_views table if it doesn't exist."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_views (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name   TEXT    NOT NULL,
                page        TEXT    NOT NULL,
                viewed_at   TEXT    NOT NULL
            )
        """)
        conn.commit()
    log.debug("page_views table ready")


def record_view(user_name: str, page: str) -> None:
    """Insert a single page-view row."""
    if not user_name:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO page_views (user_name, page, viewed_at) VALUES (?, ?, ?)",
            (user_name, page, now),
        )
        conn.commit()


def get_daily_digest(tz_name: str = "America/Los_Angeles") -> dict:
    """
    Build the daily usage digest for "today" in the given timezone.

    Returns::

        {
          "date":       "2026-07-02",
          "team_size":  6,
          "users": [
              {
                "name":             "Nick Guida",
                "sessions":         3,
                "total_minutes":    47,
                "pages_viewed":     28,
                "last_seen":        "3:42 PM PDT",
                "top_pages":        [("/", 18), ("/prospects", 6), ("/insights", 4)],
              },
              ...
          ],
          "no_shows":  ["Tiara Lark", "Nolan Johnson"],
        }
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc

    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    # Convert local day boundaries to UTC ISO strings for the SQL query
    start_utc = today_start.astimezone(timezone.utc).isoformat()
    end_utc = today_end.astimezone(timezone.utc).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_name, page, viewed_at
            FROM page_views
            WHERE viewed_at >= ? AND viewed_at < ?
            ORDER BY user_name, viewed_at
            """,
            (start_utc, end_utc),
        ).fetchall()

    # Group by user
    from collections import defaultdict, Counter
    user_views: dict[str, list[datetime]] = defaultdict(list)
    user_pages: dict[str, Counter] = defaultdict(Counter)

    for r in rows:
        name = r["user_name"]
        try:
            dt = datetime.fromisoformat(r["viewed_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        user_views[name].append(dt)
        user_pages[name][r["page"]] += 1

    users_out: list[dict] = []
    for name in sorted(user_views.keys()):
        views = sorted(user_views[name])
        pages_total = len(views)

        # De-bounce into sessions (gap >= SESSION_GAP_MINUTES -> new session)
        sessions: list[tuple[datetime, datetime]] = []
        sess_start = views[0]
        sess_end = views[0]
        for v in views[1:]:
            gap = (v - sess_end).total_seconds() / 60.0
            if gap >= SESSION_GAP_MINUTES:
                sessions.append((sess_start, sess_end))
                sess_start = v
            sess_end = v
        sessions.append((sess_start, sess_end))

        # Total active minutes: sum of session durations. A session with a
        # single page view counts as 1 minute (they at least glanced at it).
        total_min = 0
        for s_start, s_end in sessions:
            dur = (s_end - s_start).total_seconds() / 60.0
            total_min += max(dur, 1.0)
        total_min = round(total_min)

        # Last seen in local time
        last_utc = views[-1]
        last_local = last_utc.astimezone(tz)
        try:
            last_str = last_local.strftime("%-I:%M %p %Z")
        except ValueError:
            last_str = last_local.strftime("%I:%M %p %Z").lstrip("0")

        # Top pages
        top_pages = user_pages[name].most_common(5)

        users_out.append({
            "name": name,
            "sessions": len(sessions),
            "total_minutes": total_min,
            "pages_viewed": pages_total,
            "last_seen": last_str,
            "top_pages": top_pages,
        })

    # Sort by total_minutes descending (most engaged first)
    users_out.sort(key=lambda u: -u["total_minutes"])

    # Who didn't show up at all today
    active_names = set(user_views.keys())
    no_shows = [n for n in TEAM_ROSTER if n not in active_names]

    return {
        "date": now_local.strftime("%Y-%m-%d"),
        "team_size": len(TEAM_ROSTER),
        "users": users_out,
        "no_shows": no_shows,
    }
