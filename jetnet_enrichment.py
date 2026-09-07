"""
jetnet_enrichment.py — owner/operator enrichment + ownership-change detection
via the JETNET Connect API.

Two tables:
  - tail_owners            (current owner/operator snapshot per tail)
  - tail_ownership_history (one row per detected change)

Two use cases:
  1. New-tail enrichment — every landing kicks off `enrich_new_tail_async()`.
     If we've never fetched this tail before, hit `jetnet.lookup_owner()`
     and cache. Cheap: one call per tail, ever (until an explicit refresh).
  2. Nightly ownership sweep — `sweep_active_tails()` iterates every tail
     seen in the last N days, re-fetches, and if owner OR operator changed
     since the last snapshot, appends to `tail_ownership_history` and
     returns the diff so the sweep thread can fire a Teams card.

INERT until `config.JETNET_ACTIVE` is True (i.e. Tiara's Evolution creds are
in the env). All functions are safe no-ops when disabled.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import config
from sources import jetnet

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "sightings.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    """Create JETNET enrichment tables if they don't exist. Idempotent.

    Schema note: `aircraftid` / `companyid` / `contactid` are JETNET's
    stable internal keys (per Core API IDs article). Keeping them here
    lets us key on them for future bulk delta queries via `getAircraftList`
    with `aircraftchanges=true` — one bulk call instead of a per-tail loop.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tail_owners (
                nnumber      TEXT PRIMARY KEY,
                aircraftid   INTEGER,
                companyid    INTEGER,
                contactid    INTEGER,
                owner        TEXT,
                operator     TEXT,
                phone        TEXT,
                email        TEXT,
                address      TEXT,
                city         TEXT,
                state        TEXT,
                country      TEXT,
                contact_name  TEXT,
                contact_title TEXT,
                raw_json     TEXT,
                fetched_at   TEXT,
                checked_at   TEXT,
                error        TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tail_ownership_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                nnumber      TEXT NOT NULL,
                snapshot_at  TEXT NOT NULL,
                aircraftid   INTEGER,
                companyid    INTEGER,
                contactid    INTEGER,
                owner        TEXT,
                operator     TEXT,
                phone        TEXT,
                email        TEXT,
                raw_json     TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ownership_hist_tail "
            "ON tail_ownership_history (nnumber, snapshot_at DESC)"
        )
        conn.commit()

        # Migrations for repos that already created the tables pre-2026-09-03
        # (before we knew about the stable IDs). Must run BEFORE any index
        # that references the new columns.
        existing = {r[1] for r in conn.execute(
            "PRAGMA table_info(tail_owners)").fetchall()}
        for col in ("aircraftid", "companyid", "contactid"):
            if col not in existing:
                conn.execute(f"ALTER TABLE tail_owners ADD COLUMN {col} INTEGER")
        for col in ("contact_name", "contact_title"):
            if col not in existing:
                conn.execute(f"ALTER TABLE tail_owners ADD COLUMN {col} TEXT")
        existing_hist = {r[1] for r in conn.execute(
            "PRAGMA table_info(tail_ownership_history)").fetchall()}
        for col in ("aircraftid", "companyid", "contactid"):
            if col not in existing_hist:
                conn.execute(
                    f"ALTER TABLE tail_ownership_history ADD COLUMN {col} INTEGER"
                )
        conn.commit()

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tail_owners_aircraftid "
            "ON tail_owners (aircraftid)"
        )
        conn.commit()


# ── Cache reads ──────────────────────────────────────────────────────────────

def get_owner_cached(nnumber: str) -> dict | None:
    """Return the cached owner record for `nnumber`, or None if we've never fetched it."""
    if not nnumber:
        return None
    n = nnumber.strip().upper()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tail_owners WHERE nnumber = ?", (n,)
        ).fetchone()
    return dict(row) if row else None


def has_been_fetched(nnumber: str) -> bool:
    """True when we have at least one non-error cache row for this tail."""
    row = get_owner_cached(nnumber)
    return bool(row and row.get("fetched_at"))


def get_history(nnumber: str, limit: int = 20) -> list[dict]:
    """Return ownership-change history rows for a tail, newest first."""
    if not nnumber:
        return []
    n = nnumber.strip().upper()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tail_ownership_history WHERE nnumber = ? "
            "ORDER BY snapshot_at DESC LIMIT ?",
            (n, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Cache writes ─────────────────────────────────────────────────────────────

def _normalize(s: str | None) -> str:
    """Case/whitespace-insensitive comparator for owner/operator strings."""
    return " ".join((s or "").strip().lower().split())


def _upsert_owner_row(nnumber: str, payload: dict, now_iso: str) -> None:
    """
    Insert-or-replace the current-state row in tail_owners.
    `payload` is a `jetnet.lookup_owner()` return dict.
    """
    contact = payload.get("contact") or {}
    raw = payload.get("raw") or {}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tail_owners
                (nnumber, aircraftid, companyid, contactid,
                 owner, operator, phone, email, address,
                 city, state, country, contact_name, contact_title,
                 raw_json, fetched_at, checked_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(nnumber) DO UPDATE SET
                aircraftid  = excluded.aircraftid,
                companyid   = excluded.companyid,
                contactid   = excluded.contactid,
                owner       = excluded.owner,
                operator    = excluded.operator,
                phone       = excluded.phone,
                email       = excluded.email,
                address     = excluded.address,
                city        = excluded.city,
                state       = excluded.state,
                country     = excluded.country,
                contact_name  = excluded.contact_name,
                contact_title = excluded.contact_title,
                raw_json    = excluded.raw_json,
                fetched_at  = excluded.fetched_at,
                checked_at  = excluded.checked_at,
                error       = NULL
            """,
            (
                nnumber,
                payload.get("aircraftid"),
                payload.get("companyid"),
                payload.get("contactid"),
                payload.get("owner"),
                (raw.get("operatorname") or raw.get("operator")),
                contact.get("phone"),
                contact.get("email"),
                contact.get("address"),
                contact.get("city"),
                contact.get("state"),
                contact.get("country"),
                contact.get("name"),
                contact.get("title"),
                json.dumps(raw, default=str)[:200_000],
                now_iso,
                now_iso,
            ),
        )
        conn.commit()


def _mark_checked(nnumber: str, now_iso: str, error: str | None) -> None:
    """Update checked_at (and optionally error) without touching cached fields."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tail_owners (nnumber, checked_at, error) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(nnumber) DO UPDATE SET "
            "  checked_at = excluded.checked_at, "
            "  error      = excluded.error",
            (nnumber, now_iso, error),
        )
        conn.commit()


def _append_history(nnumber: str, payload: dict, now_iso: str) -> None:
    contact = payload.get("contact") or {}
    raw = payload.get("raw") or {}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tail_ownership_history
                (nnumber, snapshot_at, aircraftid, companyid, contactid,
                 owner, operator, phone, email, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                nnumber,
                now_iso,
                payload.get("aircraftid"),
                payload.get("companyid"),
                payload.get("contactid"),
                payload.get("owner"),
                (raw.get("operatorname") or raw.get("operator")),
                contact.get("phone"),
                contact.get("email"),
                json.dumps(raw, default=str)[:200_000],
            ),
        )
        conn.commit()


# ── Live API calls ───────────────────────────────────────────────────────────

def fetch_and_store_owner(nnumber: str) -> dict | None:
    """
    Force a live JETNET fetch and upsert the cache. Returns the fresh
    normalized dict, or None if JETNET is disabled / the tail is missing.
    Used by /admin/refresh-jetnet-owner and the on-landing enrichment thread.
    """
    if not (nnumber and config.JETNET_ACTIVE):
        return None
    n = nnumber.strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        payload = jetnet.lookup_owner(n)
    except Exception as e:                              # noqa: BLE001
        log.warning("JETNET lookup_owner(%s) raised: %s", n, e)
        _mark_checked(n, now_iso, error=str(e)[:200])
        return None
    if not payload:
        _mark_checked(n, now_iso, error="not_found_or_disabled")
        return None
    _upsert_owner_row(n, payload, now_iso)
    return get_owner_cached(n)


def enrich_new_tail_async(nnumber: str) -> None:
    """
    Fire-and-forget: if we've never fetched this tail, hit JETNET in a
    background thread and cache the result. Called from record_sighting()
    on every landing. Guaranteed cheap (one call per tail ever until an
    explicit refresh or nightly sweep).
    """
    if not (nnumber and config.JETNET_ACTIVE):
        return
    n = nnumber.strip().upper()
    if has_been_fetched(n):
        return

    def _worker():
        try:
            fetch_and_store_owner(n)
        except Exception as e:                          # noqa: BLE001
            log.warning("JETNET async enrich %s failed: %s", n, e)

    threading.Thread(target=_worker, daemon=True,
                     name=f"jetnet-enrich-{n}").start()


# ── Ownership-change detection ───────────────────────────────────────────────

def snapshot_and_diff(nnumber: str) -> dict | None:
    """
    Live JETNET fetch + comparison against the most recent history snapshot.

    Returns a dict describing the state:
      {
        "nnumber":  "N525AB",
        "changed":  True | False,
        "before":   {"owner": ..., "operator": ...} | None,
        "after":    {"owner": ..., "operator": ...},
        "payload":  <full normalized lookup_owner dict>,
      }
    Or None when JETNET is disabled or the tail isn't found.

    Side effects: upserts tail_owners; appends tail_ownership_history when
    the normalized owner or operator string has changed since the last
    history row (or when there is no history yet).
    """
    if not (nnumber and config.JETNET_ACTIVE):
        return None
    n = nnumber.strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        payload = jetnet.lookup_owner(n)
    except Exception as e:                              # noqa: BLE001
        log.warning("JETNET snapshot_and_diff(%s) raised: %s", n, e)
        _mark_checked(n, now_iso, error=str(e)[:200])
        return None
    if not payload:
        _mark_checked(n, now_iso, error="not_found_or_disabled")
        return None

    raw = payload.get("raw") or {}
    new_owner    = payload.get("owner")
    new_operator = raw.get("operatorname") or raw.get("operator")

    with _connect() as conn:
        prev = conn.execute(
            "SELECT owner, operator FROM tail_ownership_history "
            "WHERE nnumber = ? ORDER BY snapshot_at DESC LIMIT 1",
            (n,),
        ).fetchone()

    prev_owner    = prev["owner"]    if prev else None
    prev_operator = prev["operator"] if prev else None

    changed = (
        _normalize(new_owner)    != _normalize(prev_owner) or
        _normalize(new_operator) != _normalize(prev_operator)
    )

    _upsert_owner_row(n, payload, now_iso)
    if changed:
        _append_history(n, payload, now_iso)

    return {
        "nnumber":  n,
        "changed":  bool(changed),
        "first":    prev is None,
        "before":   {"owner": prev_owner,    "operator": prev_operator} if prev else None,
        "after":    {"owner": new_owner,     "operator": new_operator},
        "payload":  payload,
    }


def get_active_tails(days: int = 90) -> list[str]:
    """
    N-numbers seen landing in the last `days` days (dedup view). Used as the
    scope for the nightly ownership sweep so we don't burn quota on tails
    that aren't in play.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT tail_number
            FROM v_sightings_dedup_all
            WHERE tail_number IS NOT NULL
              AND tail_number != ''
              AND arrived_utc >= ?
            ORDER BY tail_number
            """,
            (cutoff,),
        ).fetchall()
    return [r["tail_number"].strip().upper() for r in rows]


def sweep_active_tails(
    days: int = 90,
    limit: int = 500,
    call_delay_s: float = 0.5,
) -> dict:
    """
    Nightly ownership sweep. For each tail seen in the last `days`, call
    `snapshot_and_diff()` and collect every diff. `limit` caps how many
    tails we touch per run so we don't blow through daily API quota if the
    fleet grows. `call_delay_s` is a polite between-calls sleep.

    Returns a summary dict {checked, changed, diffs, elapsed_s} — the sweep
    thread uses `diffs` to fire per-tail Teams cards.

    PHASE-2 REFACTOR CANDIDATE: JETNET's help center recommends a "delta"
    strategy — one bulk `getAircraftList` call with `"aircraftchanges":true`
    returns only records that changed since our last call, instead of the
    per-tail loop below. That's ~1 call vs. ~500. Blocked on confirming
    Tiara's tier includes `getCondensedOwnerOperators` /
    `getBulkAircraftExport`. Current per-tail loop is the safe baseline
    that works with the confirmed `Aircraft/getRelationships` endpoint.
    """
    if not config.JETNET_ACTIVE:
        return {"checked": 0, "changed": 0, "diffs": [],
                "elapsed_s": 0.0, "reason": "JETNET disabled"}

    tails = get_active_tails(days=days)[:limit]
    started = time.monotonic()
    diffs:   list[dict] = []
    checked = 0

    for tail in tails:
        result = snapshot_and_diff(tail)
        checked += 1
        if result and result.get("changed") and not result.get("first"):
            diffs.append(result)
        if call_delay_s > 0 and checked < len(tails):
            time.sleep(call_delay_s)

    elapsed_s = round(time.monotonic() - started, 1)
    return {
        "checked":   checked,
        "changed":   len(diffs),
        "diffs":     diffs,
        "elapsed_s": elapsed_s,
    }


# ── One-shot bulk enrichment (all aircraft) ──────────────────────────────────

_bulk_lock = threading.Lock()
_bulk_status: dict = {
    "running":     False,
    "checked":     0,
    "total":       0,
    "changed":     0,
    "errors":      0,
    "started_at":  None,
    "finished_at": None,
    "elapsed_s":   0.0,
}


def bulk_enrich_status() -> dict:
    """Snapshot of the running/last bulk-enrich job."""
    with _bulk_lock:
        return dict(_bulk_status)


def start_bulk_enrich(
    days: int = 3650,
    limit: int = 100_000,
    call_delay_s: float = 0.4,
    skip_cached: bool = False,
) -> dict:
    """
    Populate the owner cache for EVERY tail seen in the last `days` days,
    in a background daemon thread. Returns immediately so the HTTP caller
    doesn't block for the ~minutes the sweep takes. Poll `bulk_enrich_status()`
    for progress. Only one job runs at a time.
    """
    if not config.JETNET_ACTIVE:
        return {"started": False, "reason": "JETNET disabled"}
    with _bulk_lock:
        if _bulk_status["running"]:
            return {"started": False, "reason": "already running",
                    "checked": _bulk_status["checked"],
                    "total":   _bulk_status["total"]}

    tails = get_active_tails(days=days)[:limit]
    if skip_cached:
        tails = [t for t in tails if not has_been_fetched(t)]

    def _worker():
        started = time.monotonic()
        with _bulk_lock:
            _bulk_status.update(
                running=True, checked=0, total=len(tails), changed=0, errors=0,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=None, elapsed_s=0.0,
            )
        changed = errors = 0
        for i, tail in enumerate(tails):
            try:
                r = snapshot_and_diff(tail)
                if r and r.get("changed") and not r.get("first"):
                    changed += 1
                elif r is None:
                    errors += 1
            except Exception as e:                       # noqa: BLE001
                errors += 1
                log.warning("bulk enrich %s failed: %s", tail, e)
            with _bulk_lock:
                _bulk_status["checked"]   = i + 1
                _bulk_status["changed"]   = changed
                _bulk_status["errors"]    = errors
                _bulk_status["elapsed_s"] = round(time.monotonic() - started, 1)
            if call_delay_s > 0 and i + 1 < len(tails):
                time.sleep(call_delay_s)
        with _bulk_lock:
            _bulk_status["running"]     = False
            _bulk_status["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_worker, daemon=True,
                     name="jetnet-bulk-enrich").start()
    return {"started": True, "total": len(tails)}


# ── Presentation helpers ─────────────────────────────────────────────────────

def format_owner_line(row: dict | None) -> str:
    """
    Compact "Owner · Operator" string for cards/tables. Blanks the operator
    when it's identical to the owner (common — many small ops).
    """
    if not row:
        return ""
    owner    = (row.get("owner")    or "").strip()
    operator = (row.get("operator") or "").strip()
    if owner and operator and _normalize(owner) != _normalize(operator):
        return f"{owner} · operated by {operator}"
    return owner or operator or ""


def contact_line(row: dict | None) -> str:
    """One-line contact summary: phone · email · city, state."""
    if not row:
        return ""
    bits: list[str] = []
    if row.get("phone"): bits.append(str(row["phone"]))
    if row.get("email"): bits.append(str(row["email"]))
    loc = ", ".join(x for x in [row.get("city"), row.get("state")] if x)
    if loc: bits.append(loc)
    return " · ".join(bits)


def extract_all_contacts(row_or_raw) -> list[dict]:
    """
    Every company/contact relationship JETNET returned for a tail — not just
    the promoted Owner. Accepts a cached `tail_owners` row (dict with a
    `raw_json` string) or a raw `getRegNumber` payload dict.

    Each entry: {relation, company, name, title, phones:[(label, number)],
    emails:[...], location}. Phone numbers are deduped by value across the
    contact's best/office/mobile and the company office line.
    """
    raw = None
    if isinstance(row_or_raw, dict):
        if row_or_raw.get("raw_json"):
            try:
                raw = json.loads(row_or_raw["raw_json"])
            except Exception:
                raw = None
        elif "aircraftresult" in row_or_raw:
            raw = row_or_raw
        elif isinstance(row_or_raw.get("raw"), dict):
            raw = row_or_raw["raw"]
    if not raw:
        return []

    ar = raw.get("aircraftresult") or {}
    rels = ar.get("companyrelationships") or []
    out: list[dict] = []
    seen: set = set()
    for r in rels:
        first = (r.get("contactfirstname") or "").strip()
        last  = (r.get("contactlastname")  or "").strip()
        name  = f"{first} {last}".strip()
        title = (r.get("contacttitle") or "").strip()

        phones: list[tuple[str, str]] = []
        pseen: set = set()
        for label, key in (
            ("mobile", "contactmobilephone"),
            ("direct", "contactofficephone"),
            ("best",   "contactbestphone"),
            ("office", "companyofficephone"),
        ):
            v = (r.get(key) or "").strip()
            if v and v not in pseen:
                pseen.add(v)
                phones.append((label, v))

        emails: list[str] = []
        for key in ("contactemail", "companyemail"):
            v = (r.get(key) or "").strip()
            if v and v not in emails:
                emails.append(v)

        loc = ", ".join(x for x in [
            (r.get("companycity") or "").strip(),
            (r.get("companystateabbr") or r.get("companystate") or "").strip(),
            (r.get("companycountry") or "").strip(),
        ] if x)

        entry = {
            "relation": (r.get("companyrelation") or "").strip() or "Contact",
            "company":  (r.get("companyname") or "").strip(),
            "name":     name or None,
            "title":    title or None,
            "phones":   phones,
            "emails":   emails,
            "location": loc,
        }
        key = (entry["relation"], entry["company"], entry["name"],
               tuple(v for _, v in phones))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def contact_facts(row_or_raw, max_contacts: int = 4) -> list[tuple[str, str]]:
    """
    Adaptive-Card FactSet rows for every JETNET contact on a tail. One row
    per relationship: (relation, "Name (Title) — 555-1212 (mobile) · email · city").
    Accepts a cached row (with `raw_json`) or a raw payload. Empty list when
    there's nothing cached.
    """
    facts: list[tuple[str, str]] = []
    for c in extract_all_contacts(row_or_raw)[:max_contacts]:
        who = c.get("name") or c.get("company") or ""
        if who and c.get("title"):
            who = f"{who} ({c['title']})"
        bits: list[str] = []
        for lbl, num in c["phones"]:
            bits.append(f"{num} ({lbl})")
        bits.extend(c["emails"])
        if c["location"]:
            bits.append(c["location"])
        value = (f"{who} \u2014 " if who and bits else who) + " \u00b7 ".join(bits)
        if value:
            facts.append((c["relation"], value))
    return facts


def _raw_payload(row_or_raw) -> dict | None:
    """Return the raw `getRegNumber` payload from a cached row or raw dict."""
    if not isinstance(row_or_raw, dict):
        return None
    if row_or_raw.get("raw_json"):
        try:
            return json.loads(row_or_raw["raw_json"])
        except Exception:
            return None
    if "aircraftresult" in row_or_raw:
        return row_or_raw
    if isinstance(row_or_raw.get("raw"), dict):
        return row_or_raw["raw"]
    return None


def operating_rule(row_or_raw) -> dict:
    """
    Best-effort Part 135 (charter/managed) vs Part 91 (owner-flown/corporate)
    classification from a JETNET aircraft record. The sales dialogue differs by
    operating rule, so /plan tailors the pitch off this.

    Signal priority:
      1. `maintained` — literally "FAR Part 135" / "FAR Part 91" (authoritative).
      2. A distinct "Certificate Holder" relationship or a "Charter" business
         type on any related company → 135.
      3. `usage` — Charter/Cargo → 135; Corporate/Private/Owner-flown → 91.

    Returns {rule: '135'|'91'|'unknown', maintained, usage, basis}.
    Accepts a cached `tail_owners` row (with `raw_json`) or a raw payload.
    """
    raw = _raw_payload(row_or_raw)
    ar = (raw or {}).get("aircraftresult") or {}
    maintained = (ar.get("maintained") or "").strip()
    usage = (ar.get("usage") or "").strip()
    rels = ar.get("companyrelationships") or []

    m = maintained.lower()
    u = usage.lower()
    biz = " ".join((r.get("companybusinesstype") or "") for r in rels).lower()
    has_cert_holder = any(
        (r.get("companyrelation") or "").strip().lower() == "certificate holder"
        for r in rels
    )

    rule = "unknown"
    basis = ""
    if "135" in m or "121" in m:
        rule, basis = "135", f"maintained = {maintained}"
    elif "91" in m:
        rule, basis = "91", f"maintained = {maintained}"
    elif has_cert_holder:
        rule, basis = "135", "has a separate Part 135 certificate holder"
    elif "charter" in biz:
        rule, basis = "135", "charter operator on the relationship graph"
    elif u in ("charter", "cargo"):
        rule, basis = "135", f"usage = {usage}"
    elif u in ("corporate", "private", "owner flown", "owner-flown", "personal"):
        rule, basis = "91", f"usage = {usage}"

    return {
        "rule":       rule,
        "maintained": maintained or None,
        "usage":      usage or None,
        "basis":      basis or None,
    }
