"""
guest_access.py — time-boxed guest links for external visitors.

Two-phase lifecycle:
  1. LINK PHASE — from creation, the link is valid to be clicked for
     `link_valid_hours` (default 168 = 7 days). If the guest never clicks
     it, it silently expires.
  2. SESSION PHASE — the moment the guest first clicks the link, the
     session clock starts and runs for `duration_minutes` (default 30).

A guest link (e.g. /guest/<token>) sets a short-lived cookie that:
  - Bypasses the "Who are you?" identify modal.
  - Suppresses page-view logging to the team-visible `page_views` table
    (so the visitor does NOT appear in the 4pm daily digest).
  - Records access to a PRIVATE `guest_access_log` table, viewable only
    via the X-Deploy-Secret-protected admin endpoint.

Usage:
  - Nick creates a token via POST /admin/create-guest-link (X-Deploy-Secret)
    with body {"minutes": 30, "label": "Bob Smith / XYZ Aerospace"}.
  - Nick sends the returned URL to the guest.
  - Guest clicks the link → session clock starts → cookie set → sees the
    site normally with a small "Guest access — expires in Xm" pill
    (no identify picker).
  - No team member sees any trace of the visit in the digest or /activity.
  - Nick can review guest activity via GET /admin/guest-log (X-Deploy-Secret).
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "sightings.db"

# Guardrails on session lifetime
MIN_MINUTES = 1
MAX_MINUTES = 1440  # 24h

# How long an unclicked link stays valid before it silently expires.
DEFAULT_LINK_VALID_HOURS = 168  # 7 days


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    """Create + migrate guest tables."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_tokens (
                token               TEXT PRIMARY KEY,
                label               TEXT,
                created_at          TEXT NOT NULL,
                expires_at          TEXT NOT NULL,
                duration_minutes    INTEGER NOT NULL DEFAULT 30,
                activated_at        TEXT
            )
            """
        )
        # Migrate: add duration_minutes / activated_at to pre-existing tables.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(guest_tokens)")}
        if "duration_minutes" not in existing:
            conn.execute(
                "ALTER TABLE guest_tokens ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 30"
            )
        if "activated_at" not in existing:
            conn.execute("ALTER TABLE guest_tokens ADD COLUMN activated_at TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_access_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token       TEXT NOT NULL,
                label       TEXT,
                path        TEXT NOT NULL,
                remote_addr TEXT,
                user_agent  TEXT,
                viewed_at   TEXT NOT NULL
            )
            """
        )
        conn.commit()
    log.debug("guest_tokens + guest_access_log tables ready")


def create_token(minutes: int = 30, label: str = "",
                 link_valid_hours: int = DEFAULT_LINK_VALID_HOURS) -> dict:
    """
    Mint a new guest token.

    `minutes` = session length that starts the MOMENT the guest first clicks
                the link (bounded to 1..1440).
    `link_valid_hours` = window during which the unclicked link remains valid.
                        After this the link silently expires even if untouched.
    """
    minutes = max(MIN_MINUTES, min(int(minutes), MAX_MINUTES))
    link_valid_hours = max(1, min(int(link_valid_hours), 24 * 30))  # 1h..30d
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    link_expires = now + timedelta(hours=link_valid_hours)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guest_tokens "
            "(token, label, created_at, expires_at, duration_minutes, activated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (token, (label or "").strip(), now.isoformat(),
             link_expires.isoformat(), minutes),
        )
        conn.commit()
    return {
        "token":               token,
        "label":               (label or "").strip(),
        "created_at":          now.isoformat(),
        "link_expires_at":     link_expires.isoformat(),
        "duration_minutes":    minutes,
        "activated_at":        None,
    }


def _effective_expiry(row: sqlite3.Row) -> datetime | None:
    """
    Compute when the token effectively stops working:
      - If not yet activated: the link-valid deadline (row.expires_at).
      - If activated: activated_at + duration_minutes.
    Returns None if any field is unparseable.
    """
    try:
        if row["activated_at"]:
            start = datetime.fromisoformat(row["activated_at"])
            return start + timedelta(minutes=int(row["duration_minutes"] or 30))
        return datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError, KeyError):
        return None


def get_token(token: str) -> dict | None:
    """Return the token row (with `effective_expires_at`) if valid, else None."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT token, label, created_at, expires_at, "
            "duration_minutes, activated_at "
            "FROM guest_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    exp = _effective_expiry(row)
    if exp is None or datetime.now(timezone.utc) >= exp:
        return None
    return {
        "token":                row["token"],
        "label":                row["label"] or "",
        "created_at":           row["created_at"],
        "link_expires_at":      row["expires_at"],
        "duration_minutes":     int(row["duration_minutes"] or 30),
        "activated_at":         row["activated_at"],
        "effective_expires_at": exp.isoformat(),
    }


def activate_token(token: str) -> dict | None:
    """
    Mark a token as activated (session-clock start) if not already activated.
    Returns the refreshed token row, or None if the token is invalid/expired.
    """
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE guest_tokens SET activated_at = ? "
            "WHERE token = ? AND activated_at IS NULL",
            (now, token),
        )
        conn.commit()
    return get_token(token)


def revoke_token(token: str) -> bool:
    """Delete a token (expires it immediately). Returns True if a row was removed."""
    if not token:
        return False
    with _connect() as conn:
        cur = conn.execute("DELETE FROM guest_tokens WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0


def log_access(token: str, label: str, path: str,
               remote_addr: str = "", user_agent: str = "") -> None:
    """Record a guest page view to the private log."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guest_access_log "
            "(token, label, path, remote_addr, user_agent, viewed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, label, path, remote_addr, (user_agent or "")[:200],
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def list_active_tokens() -> list[dict]:
    """Return all tokens whose effective expiry is still in the future."""
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, label, created_at, expires_at, "
            "duration_minutes, activated_at "
            "FROM guest_tokens ORDER BY created_at DESC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        exp = _effective_expiry(r)
        if exp is None or now >= exp:
            continue
        out.append({
            "token":                r["token"],
            "label":                r["label"] or "",
            "created_at":           r["created_at"],
            "link_expires_at":      r["expires_at"],
            "duration_minutes":     int(r["duration_minutes"] or 30),
            "activated_at":         r["activated_at"],
            "effective_expires_at": exp.isoformat(),
        })
    return out


def list_recent_access(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, token, label, path, remote_addr, user_agent, viewed_at "
            "FROM guest_access_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]

