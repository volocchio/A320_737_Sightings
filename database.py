"""
database.py — SQLite store for deduplication.

A landing is uniquely identified by (source, flight_id).
We store enough detail to skip re-notification across polls
and to log every sighting for later analysis.
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

DB_PATH = Path(__file__).parent / "sightings.db"

log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _flightaware_tracking_url(flight_id: str | None, tail_number: str | None = None) -> str:
    """Build a stable FlightAware URL for a specific historical flight when possible."""
    fid = (flight_id or "").strip()
    if fid:
        return f"https://flightaware.com/live/flight/id/{quote(fid, safe='')}"
    tail = (tail_number or "").strip().upper()
    if tail:
        return f"https://flightaware.com/live/flight/{quote(tail, safe='')}"
    return ""


def resolve_tracking_url(
    source: str | None,
    flight_id: str | None,
    tracking_url: str | None,
    tail_number: str | None = None,
) -> str:
    """Return a best-effort per-flight tracking URL across ingest sources."""
    if (source or "").lower() == "flightaware":
        return _flightaware_tracking_url(flight_id, tail_number) or (tracking_url or "")
    return tracking_url or ""

A320_FAMILY_TYPES = {"A318", "A319", "A320", "A321", "A19N", "A20N", "A21N"}
B737_FAMILY_TYPES = {"B736", "B737", "B738", "B739", "B37M", "B38M", "B39M", "B3XM"}


def normalize_family(family: str | None) -> str | None:
    fam = (family or "").strip().upper().replace("-", "")
    if fam in {"A320", "AIRBUS", "AIRBUS320"}:
        return "A320"
    if fam in {"737", "B737", "BOEING", "BOEING737"}:
        return "B737"
    return None


def family_where_clause(family: str | None, alias: str = "") -> tuple[str, tuple]:
    fam = normalize_family(family)
    if not fam:
        return "", ()
    col = f"{alias}.ac_type" if alias else "ac_type"
    types = sorted(A320_FAMILY_TYPES if fam == "A320" else B737_FAMILY_TYPES)
    placeholders = ",".join("?" for _ in types)
    return f" AND UPPER(COALESCE({col}, '')) IN ({placeholders})", tuple(types)


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sightings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT    NOT NULL,
                flight_id   TEXT    NOT NULL,
                tail_number TEXT,
                ac_type     TEXT,
                origin_icao TEXT,
                origin_name TEXT,
                dest_icao   TEXT,
                dest_name   TEXT,
                departed_utc    TEXT,
                arrived_utc     TEXT,
                operator        TEXT,
                tracking_url    TEXT,
                distance_nm     REAL,
                notified_at     TEXT NOT NULL,
                UNIQUE(source, flight_id)
            )
            """
        )
        conn.commit()
    # Migrations: add columns that didn't exist in earlier schema versions
    with _connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sightings)").fetchall()]
        if "distance_nm" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN distance_nm REAL")
            conn.commit()
            log.info("Migrated sightings table: added distance_nm column")
        if "departure_oat_c" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN departure_oat_c REAL")
            conn.commit()
            log.info("Migrated sightings table: added departure_oat_c column")
        if "arrival_oat_c" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN arrival_oat_c REAL")
            conn.commit()
            log.info("Migrated sightings table: added arrival_oat_c column")
        if "serial_number" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN serial_number TEXT")
            conn.commit()
            log.info("Migrated sightings table: added serial_number column")
        if "is_tamarack_fleet" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN is_tamarack_fleet INTEGER")
            conn.commit()
            log.info("Migrated sightings table: added is_tamarack_fleet column")
        if "ac_subvariant" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN ac_subvariant TEXT")
            conn.commit()
            log.info("Migrated sightings table: added ac_subvariant column")
        # Climb-profile columns (populated by track_fetcher.backfill_pending)
        if "track_fetched_at" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN track_fetched_at TEXT")
            conn.execute("ALTER TABLE sightings ADD COLUMN top_altitude_ft INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN time_to_10k_sec INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN time_to_top_sec INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN avg_climb_rate_fpm INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN peak_climb_rate_fpm INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN climb_gradient_pct REAL")
            conn.commit()
            log.info("Migrated sightings table: added climb-profile columns")
        # Step-climb columns: initial level-off altitude + time to reach it.
        # Lets us show "FL280 18 min → FL410 28 min" for aircraft that step-climb
        # (typical for non-winglet CJs that can't make initial altitude directly).
        if "initial_cruise_alt_ft" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN initial_cruise_alt_ft INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN time_to_initial_cruise_sec INTEGER")
            conn.commit()
            log.info("Migrated sightings table: added step-climb columns")
        # Sustained-top (highest altitude held for ≥30 min within ±500 ft).
        # The "operationally honest" cruise altitude — filters brief overshoots
        # and ATC step assignments. Used by the climb-comparison chart.
        if "sustained_top_alt_ft" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN sustained_top_alt_ft INTEGER")
            conn.commit()
            log.info("Migrated sightings table: added sustained_top_alt_ft column")
        # Teams notification timestamp (parallel to email's notified_at)
        if "teams_notified_at" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN teams_notified_at TEXT")
            conn.commit()
            log.info("Migrated sightings table: added teams_notified_at column")
        # Distance validation via FA track — for long-haul flights we don't
        # want to trust origin/dest ICAOs blindly. `distance_validated`:
        # NULL=unchecked, 1=track is continuous & endpoints match, 0=phantom
        # (ground gap in track, or endpoints don't match recorded origin/dest).
        # `distance_nm_validated` = actual flown distance summed along the track.
        if "distance_validated" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN distance_validated INTEGER")
            conn.execute("ALTER TABLE sightings ADD COLUMN distance_nm_validated REAL")
            conn.commit()
            log.info("Migrated sightings table: added distance validation columns")
        # Scope tier — separates ATLAS-eligible aircraft from adjacent tracking
        # (Mustang up-purchase target, CJ4 down-purchase target). All existing
        # rows are ATLAS-eligible by definition (adjacent types were never
        # ingested before this column existed) so we backfill to 'atlas'.
        if "scope_tier" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN scope_tier TEXT")
            conn.execute("UPDATE sightings SET scope_tier='atlas' WHERE scope_tier IS NULL")
            conn.commit()
            log.info("Migrated sightings table: added scope_tier column (backfilled 'atlas')")
        # Up-purchase Teams-card notification timestamp — set once per Mustang
        # chain trip we alert on, so a subsequent poll doesn't re-fire the
        # same card.
        if "up_purchase_notified_at" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN up_purchase_notified_at TEXT")
            conn.commit()
            log.info("Migrated sightings table: added up_purchase_notified_at column")
        # Sales-region bucket — classifies each sighting into 'NA' (US/CA/MX),
        # 'EU_UK' (EU-27 + UK + EFTA), or 'OTHER' based on origin/dest ICAO.
        # Powers the region dropdown on /insights, /prospects, /. All pre-
        # migration rows are backfilled from the current airport index.
        if "region" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN region TEXT")
            conn.commit()
            log.info("Migrated sightings table: added region column")
        # FlightAware canonical flight id — populated during enrichment for
        # non-FA sources (adsb.lol, adsbexchange, opensky). Lets track_fetcher
        # pull `/flights/{fa_flight_id}/track` on rows whose source is not FA
        # itself, so EU sightings from adsb.lol can get climb metrics.
        if "fa_flight_id" not in cols:
            conn.execute("ALTER TABLE sightings ADD COLUMN fa_flight_id TEXT")
            # Rows whose source IS FlightAware already have their canonical
            # id in `flight_id`; copy it forward so downstream code can key
            # exclusively on `fa_flight_id`.
            conn.execute(
                "UPDATE sightings SET fa_flight_id = flight_id "
                "WHERE source = 'flightaware' AND fa_flight_id IS NULL"
            )
            conn.commit()
            log.info("Migrated sightings table: added fa_flight_id column "
                     "(seeded from flight_id for FA source)")

    # ── Deduplication view ─────────────────────────────────────────────────
    # Same flight reported by multiple sources (FlightAware + ADSB Exchange +
    # OpenSky) shouldn't show up as multiple rows on the dashboard. Group by
    # (tail, origin, dest, date) and keep the most-enriched source.
    # Scope filter: this view is the canonical dashboard source. For this
    # A320/737 clone it includes generic 'tracked' rows as well as legacy
    # 'atlas' rows, while still excluding adjacent/up-purchase rows. NULL
    # scope_tier is treated as 'atlas' for backwards compatibility.
    # Always DROP + CREATE so the definition can evolve.
    with _connect() as conn:
        conn.execute("DROP VIEW IF EXISTS v_sightings_dedup")
        conn.execute("""
            CREATE VIEW v_sightings_dedup AS
            SELECT * FROM (
              SELECT s.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY UPPER(COALESCE(NULLIF(tail_number, ''), 'id_' || id)),
                                    UPPER(IFNULL(origin_icao, '')),
                                    UPPER(IFNULL(dest_icao,   '')),
                                    DATE(arrived_utc)
                       ORDER BY
                         CASE source
                           WHEN 'flightaware'  THEN 1
                           WHEN 'adsbexchange' THEN 2
                           WHEN 'opensky'      THEN 3
                           ELSE 4
                         END,
                         id DESC
                     ) AS _rn
              FROM sightings s
              WHERE (s.scope_tier IS NULL OR s.scope_tier IN ('atlas', 'tracked'))
            )
            WHERE _rn = 1
        """)
        # Companion view: same dedup logic, no scope filter. Used by pages that
        # need to see adjacent-tier rows (Mustang activity, tail dossier for a
        # deep-linked Mustang tail). Do NOT use for ATLAS math.
        conn.execute("DROP VIEW IF EXISTS v_sightings_dedup_all")
        conn.execute("""
            CREATE VIEW v_sightings_dedup_all AS
            SELECT * FROM (
              SELECT s.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY UPPER(COALESCE(NULLIF(tail_number, ''), 'id_' || id)),
                                    UPPER(IFNULL(origin_icao, '')),
                                    UPPER(IFNULL(dest_icao,   '')),
                                    DATE(arrived_utc)
                       ORDER BY
                         CASE source
                           WHEN 'flightaware'  THEN 1
                           WHEN 'adsbexchange' THEN 2
                           WHEN 'opensky'      THEN 3
                           ELSE 4
                         END,
                         id DESC
                     ) AS _rn
              FROM sightings s
            )
            WHERE _rn = 1
        """)
        conn.commit()
    log.debug("Database ready at %s", DB_PATH)


def is_already_notified(source: str, flight_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sightings WHERE source=? AND flight_id=?",
            (source, flight_id),
        ).fetchone()
    return row is not None


def _tail_card_throttled(conn, tail_number: str | None, now_iso: str) -> bool:
    """True if a Teams sighting card for this tail was sent inside the
    user-configured cooldown window (notify_settings.min_interval_minutes).

    Keeps the A320/737 Sightings Thread from being spammed by a single busy tail.
    Returns False (never throttle) when the cooldown is 0 or on any error, so
    a settings/parse problem can never silently suppress alerts.
    """
    try:
        import notify_settings as _ns
        minutes = _ns.get_min_interval_minutes()
        if minutes <= 0:
            return False
        tail = (tail_number or "").strip().upper()
        if not tail:
            return False
        row = conn.execute(
            "SELECT MAX(teams_notified_at) FROM sightings "
            "WHERE UPPER(tail_number)=? AND teams_notified_at IS NOT NULL",
            (tail,),
        ).fetchone()
        last = row[0] if row else None
        if not last:
            return False
        last_dt = datetime.fromisoformat(last)
        now_dt = datetime.fromisoformat(now_iso)
        return (now_dt - last_dt).total_seconds() < minutes * 60
    except Exception as e:   # noqa: BLE001
        log.debug("tail card throttle check failed: %s", e)
        return False


def record_sighting(sighting: dict) -> None:
    """Insert a sighting. Silently ignores duplicates (UNIQUE constraint)."""
    import config as _cfg
    now = datetime.now(timezone.utc).isoformat()

    # Generic A320/737 tracker: accept configured ICAO types and avoid the
    # original CJ/ATLAS-only rejection path. JETNET enrichment remains active.
    raw_type = (sighting.get("ac_type") or "").strip().upper()
    if raw_type and raw_type not in set(_cfg.AIRCRAFT_TYPES):
        return

    tail = sighting.get("tail_number") or ""
    serial_number: str | None = None
    is_tamarack_fleet: int | None = None
    ac_subvariant: str | None = None
    scope_tier = "tracked"

    # Sales-region bucket (NA / EU_UK / OTHER) — used by the region dropdown.
    from airports import region_for_sighting as _region_for_sighting
    region = _region_for_sighting(sighting.get("origin_icao"),
                                  sighting.get("dest_icao"))

    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO sightings
                    (source, flight_id, tail_number, ac_type,
                     origin_icao, origin_name, dest_icao, dest_name,
                     departed_utc, arrived_utc, operator, tracking_url,
                     distance_nm, notified_at, serial_number, is_tamarack_fleet,
                     ac_subvariant, scope_tier, region, fa_flight_id)
                VALUES
                    (:source, :flight_id, :tail_number, :ac_type,
                     :origin_icao, :origin_name, :dest_icao, :dest_name,
                     :departed_utc, :arrived_utc, :operator, :tracking_url,
                     :distance_nm, :notified_at, :serial_number, :is_tamarack_fleet,
                     :ac_subvariant, :scope_tier, :region, :fa_flight_id)
                """,
                {
                    **sighting,
                    "notified_at": now,
                    "serial_number": serial_number,
                    "is_tamarack_fleet": is_tamarack_fleet,
                    "ac_subvariant": ac_subvariant,
                    "scope_tier": scope_tier,
                    "region": region,
                    "fa_flight_id": (
                        sighting.get("fa_flight_id")
                        or (sighting.get("flight_id")
                            if sighting.get("source") == "flightaware" else None)
                    ),
                },
            )
            conn.commit()

            # JETNET owner enrichment: fire-and-forget lookup on every new
            # landing. No-op when disabled or when we've already cached this
            # tail — safe to call unconditionally.
            try:
                import jetnet_enrichment as _je
                _je.enrich_new_tail_async(sighting.get("tail_number") or "")
            except Exception as e:                       # noqa: BLE001
                log.debug("JETNET enrichment kickoff failed: %s", e)

            # Teams notification: airport-or-tail match against the watch lists
            try:
                import watch_airports as _wa
                import watch_tails as _wt
                import teams_notifier as _tn
                apt_hits   = _wa.matches(sighting.get("origin_icao"),
                                         sighting.get("dest_icao"))
                tail_hit   = (sighting.get("tail_number") or "").upper() \
                             if _wt.matches(sighting.get("tail_number")) else None
                if (apt_hits or tail_hit) and not _tail_card_throttled(
                        conn, sighting.get("tail_number"), now):
                    enriched = dict(sighting)
                    enriched.setdefault("ac_subvariant", ac_subvariant)
                    enriched.setdefault("serial_number", serial_number)
                    if _tn.notify_sighting(enriched,
                                           matched_airports=apt_hits,
                                           matched_tail=tail_hit):
                        conn.execute(
                            "UPDATE sightings SET teams_notified_at=? "
                            "WHERE source=? AND flight_id=?",
                            (now, sighting["source"], sighting["flight_id"]),
                        )
                        conn.commit()
            except Exception as e:   # noqa: BLE001
                log.warning("Teams notify failed: %s", e)

            # Up-purchase signal: if this is a Mustang landing that completes a
            # fuel-stop chain, fire the dedicated Teams card. Silent no-op for
            # every non-Mustang landing.
            if scope_tier == "up":
                try:
                    _fire_up_purchase_chain_card_if_applicable(
                        conn, sighting, now,
                    )
                except Exception as e:   # noqa: BLE001
                    log.warning("Up-purchase notify failed: %s", e)
        except sqlite3.IntegrityError:
            pass  # already recorded


def _fire_up_purchase_chain_card_if_applicable(conn, sighting: dict, now_iso: str) -> None:
    """
    If the just-inserted Mustang landing closes a fuel-stop chain with the
    previous Mustang landing on the same tail, fire an "up-purchase signal"
    Teams card. Dedup by stamping up_purchase_notified_at on the second-leg
    row so we never re-send the same chain.
    """
    import teams_notifier as _tn
    tail = (sighting.get("tail_number") or "").strip()
    if not tail:
        return
    chains = get_fuel_stop_chains(days=7, scope_tier="up", tail_number=tail)
    if not chains:
        return
    # Find the chain (if any) whose final leg matches this just-inserted sighting.
    dest = (sighting.get("dest_icao") or "").upper()
    arrived_day = (sighting.get("arrived_utc") or "")[:10]
    chain = next(
        (c for c in chains
         if c.get("dest_icao") == dest
         and c.get("arrived_utc", "")[:10] == arrived_day),
        None,
    )
    if chain is None:
        return
    # Dedup: only fire if we haven't stamped this row yet.
    row = conn.execute(
        "SELECT up_purchase_notified_at FROM sightings "
        "WHERE source=? AND flight_id=?",
        (sighting["source"], sighting["flight_id"]),
    ).fetchone()
    if row and row["up_purchase_notified_at"]:
        return
    if _tn.notify_up_purchase_chain(chain):
        conn.execute(
            "UPDATE sightings SET up_purchase_notified_at=? "
            "WHERE source=? AND flight_id=?",
            (now_iso, sighting["source"], sighting["flight_id"]),
        )
        conn.commit()


def backfill_distances() -> int:
    """
    Recompute distance_nm from origin+dest ICAO pairs and reconcile stored values.

    Updates rows when:
      - distance_nm is NULL, or
      - stored distance differs from computed great-circle distance by > 1 nm.

    Returns count of rows updated.
    """
    from airports import icao_distance_nm
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, origin_icao, dest_icao, distance_nm FROM sightings "
            "WHERE origin_icao != '' AND dest_icao != ''"
        ).fetchall()
        updated = 0
        for row in rows:
            dist = icao_distance_nm(row["origin_icao"], row["dest_icao"])
            if dist is None:
                continue
            old = row["distance_nm"]
            if old is not None and abs(float(old) - float(dist)) <= 1.0:
                continue
            conn.execute(
                "UPDATE sightings SET distance_nm=? WHERE id=?",
                (dist, row["id"]),
            )
            updated += 1
        conn.commit()
    return updated


def reset_eu_track_backfill(days: int = 14, limit: int = 500) -> int:
    """
    Clear `track_fetched_at` on adsb.lol-sourced EU_UK rows that never got
    climb metrics — so the periodic `track_fetcher.backfill_pending` loop
    will re-process them and try to resolve `fa_flight_id` via FA enrichment.

    Only touches rows arrived within the last `days` (FA's tail-history
    endpoint typically covers ~14 days), and only rows that don't already
    have `fa_flight_id` or `top_altitude_ft`. Returns count of rows reset.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE sightings
               SET track_fetched_at = NULL
             WHERE id IN (
                 SELECT id FROM sightings
                  WHERE source = 'adsblol'
                    AND region = 'EU_UK'
                    AND arrived_utc >= ?
                    AND fa_flight_id IS NULL
                    AND top_altitude_ft IS NULL
                    AND track_fetched_at IS NOT NULL
                  ORDER BY id DESC
                  LIMIT ?
             )
            """,
            (cutoff, limit),
        )
        conn.commit()
        return cur.rowcount


def backfill_regions() -> int:
    """
    Populate the ``region`` column for existing sightings that don't have it
    yet. Called once at startup after the airport index has been loaded. Rows
    inserted after the region migration already carry a value, so this only
    fires against pre-migration history.
    """
    from airports import region_for_sighting
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, origin_icao, dest_icao FROM sightings "
            "WHERE region IS NULL OR region = ''"
        ).fetchall()
        updated = 0
        for row in rows:
            region = region_for_sighting(row["origin_icao"], row["dest_icao"])
            conn.execute(
                "UPDATE sightings SET region=? WHERE id=?",
                (region, row["id"]),
            )
            updated += 1
        conn.commit()
    return updated


def backfill_fleet_status() -> int:
    """
    Populate serial_number, is_tamarack_fleet, and ac_subvariant for existing
    sightings that have a tail_number but no fleet data yet.
    Requires FAA registry to be loaded.  Returns count of rows updated.
    """
    import tamarack_fleet as _tf
    _tf.load_faa_registry()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tail_number FROM sightings "
            "WHERE tail_number IS NOT NULL AND tail_number != '' "
            "AND (serial_number IS NULL OR ac_subvariant IS NULL)"
        ).fetchall()
        updated = 0
        for row in rows:
            tail = row["tail_number"]
            status = _tf.fleet_status(tail)
            sv     = _tf.nnum_to_subvariant(tail)
            if status is not None or sv:
                conn.execute(
                    "UPDATE sightings SET serial_number=?, is_tamarack_fleet=?, "
                    "ac_subvariant=? WHERE id=?",
                    (
                        status["sn"]                    if status else None,
                        (1 if status["active"] else 2)  if status else None,
                        sv or None,
                        row["id"],
                    ),
                )
                updated += 1
        conn.commit()
    return updated


def rescrub_fleet_status() -> dict:
    """
    Force re-resolution of serial_number, ac_type, ac_subvariant, and
    is_tamarack_fleet for EVERY sighting with a tail_number.

    Used to fix rows that were populated with a stale or buggy subvariant
    resolver. Authoritative sources (in order):
      1. tail_number → FAA registry → serial_number
      2. serial_number → sn_variant()       → ac_type        (overrides any
         API-provided ac_type because S/N + TCDS is more reliable)
      3. serial_number → subvariant_from_sn() → ac_subvariant (overrides
         any API-derived sub-variant for the same reason)
      4. tail_number → nnum_to_subvariant() fallback if no S/N available

    Returns a summary dict:
        scanned       — total rows considered
        updated       — rows where at least one field changed
        dashed        — rows where an un-dashed letter-suffix S/N was
                        canonicalised to the dashed form (525A0123 → 525A-0123)
        ac_type_changes        — dict {old → {new: count}}
        ac_subvariant_changes  — dict {old → {new: count}}
    """
    import tamarack_fleet as _tf
    from collections import defaultdict
    _tf.load_faa_registry()

    type_changes = defaultdict(lambda: defaultdict(int))
    sv_changes   = defaultdict(lambda: defaultdict(int))
    scanned = 0
    updated = 0
    dashed  = 0

    # First pass: canonicalise any un-dashed letter-suffix serial numbers to
    # the dashed TCDS form so fleet-dict lookups work uniformly.
    with _connect() as conn:
        bad_sn_rows = conn.execute(
            "SELECT id, serial_number FROM sightings "
            "WHERE serial_number IS NOT NULL AND serial_number != ''"
        ).fetchall()
        for r in bad_sn_rows:
            new = _tf._normalize_faa_sn(r["serial_number"])
            if new != r["serial_number"]:
                conn.execute(
                    "UPDATE sightings SET serial_number=? WHERE id=?",
                    (new, r["id"]),
                )
                dashed += 1
        if dashed:
            conn.commit()
            log.info("rescrub: canonicalised %d un-dashed serial_numbers", dashed)

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tail_number, ac_type, ac_subvariant, serial_number, "
            "is_tamarack_fleet FROM sightings "
            "WHERE tail_number IS NOT NULL AND tail_number != ''"
        ).fetchall()

        for row in rows:
            scanned += 1
            tail = row["tail_number"]

            # 1. Resolve fleet status + S/N from tail
            status = _tf.fleet_status(tail)
            new_sn      = status["sn"] if status else _tf.nnum_to_sn(tail)
            new_is_flt  = (1 if status["active"] else 2) if status else None

            # 2 & 3. Derive ac_type + ac_subvariant from S/N (authoritative)
            new_ac_type = None
            new_sv      = None
            if new_sn:
                new_ac_type = _tf.sn_variant(new_sn)
                new_sv      = _tf.subvariant_from_sn(new_sn)
            # 4. Fallback: ACFTREF model name
            if not new_sv:
                new_sv = _tf.nnum_to_subvariant(tail) or None
            if not new_ac_type:
                # Preserve existing ac_type if S/N unknown — API value is best we have
                new_ac_type = row["ac_type"]

            # Empty-string sub-variant → NULL for cleanliness
            new_sv = new_sv or None

            # Track changes for reporting
            if (row["ac_type"] or "") != (new_ac_type or ""):
                type_changes[row["ac_type"] or ""][new_ac_type or ""] += 1
            if (row["ac_subvariant"] or "") != (new_sv or ""):
                sv_changes[row["ac_subvariant"] or ""][new_sv or ""] += 1

            # Only UPDATE if something actually changed
            if (
                (row["ac_type"]          or "")    != (new_ac_type or "")
                or (row["ac_subvariant"] or "")    != (new_sv or "")
                or (row["serial_number"] or "")    != (new_sn or "")
                or (row["is_tamarack_fleet"] or 0) != (new_is_flt or 0)
            ):
                conn.execute(
                    "UPDATE sightings SET ac_type=?, ac_subvariant=?, "
                    "serial_number=?, is_tamarack_fleet=? WHERE id=?",
                    (new_ac_type, new_sv, new_sn, new_is_flt, row["id"]),
                )
                updated += 1
        conn.commit()

    return {
        "scanned": scanned,
        "updated": updated,
        "dashed":  dashed,
        "ac_type_changes":       {k: dict(v) for k, v in type_changes.items()},
        "ac_subvariant_changes": {k: dict(v) for k, v in sv_changes.items()},
    }


def get_fleet_comparison() -> dict:
    """
    Compare mission statistics between active Tamarack ATLAS fleet (is_tamarack_fleet=1),
    removed/decommissioned fleet (is_tamarack_fleet=2), and unmodified aircraft (NULL/0).
    Only includes sightings with a known distance.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT is_tamarack_fleet, distance_nm
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > 50
            """
        ).fetchall()

    buckets: dict[str, list[float]] = {"atlas": [], "removed": [], "other": []}
    for row in rows:
        flag = row["is_tamarack_fleet"]
        dist = row["distance_nm"]
        if flag == 1:
            buckets["atlas"].append(dist)
        elif flag == 2:
            buckets["removed"].append(dist)
        else:
            buckets["other"].append(dist)

    def _stats(dists: list[float]) -> dict:
        if not dists:
            return {"count": 0, "avg": None, "max": None, "pct_over_1000": None}
        avg = round(sum(dists) / len(dists))
        return {
            "count": len(dists),
            "avg": avg,
            "max": round(max(dists)),
            "pct_over_1000": round(100 * sum(1 for d in dists if d > 1000) / len(dists)),
        }

    atlas_stats = _stats(buckets["atlas"])
    other_stats = _stats(buckets["other"])

    # Compute the ATLAS advantage: how much farther fleet flies on average
    advantage_nm: int | None = None
    advantage_pct: int | None = None
    if atlas_stats["avg"] and other_stats["avg"] and other_stats["avg"] > 0:
        advantage_nm  = atlas_stats["avg"] - other_stats["avg"]
        advantage_pct = round(100 * advantage_nm / other_stats["avg"])

    return {
        "atlas":        atlas_stats,
        "removed":      _stats(buckets["removed"]),
        "other":        other_stats,
        "advantage_nm": advantage_nm,
        "advantage_pct": advantage_pct,
    }


def get_period_stats(region: str | None = None, family: str | None = None) -> dict:
    """Return sighting counts for common time periods. Optionally region/family-scoped."""
    now = datetime.now(timezone.utc)
    region_sql = " AND region = ?" if region else ""
    region_args: tuple = (region,) if region else ()
    family_sql, family_args = family_where_clause(family)

    def _count(hours: float) -> int:
        cutoff = (now - timedelta(hours=hours)).isoformat()
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM v_sightings_dedup WHERE arrived_utc >= ?{region_sql}{family_sql}",
                (cutoff, *region_args, *family_args),
            ).fetchone()
        return row[0] if row else 0

    ytd_cutoff = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with _connect() as conn:
        ytd = conn.execute(
            f"SELECT COUNT(*) FROM v_sightings_dedup WHERE arrived_utc >= ?{region_sql}{family_sql}",
            (ytd_cutoff, *region_args, *family_args),
        ).fetchone()[0]
        if region:
            total = conn.execute(
                f"SELECT COUNT(*) FROM v_sightings_dedup WHERE region = ?{family_sql}",
                (region, *family_args),
            ).fetchone()[0]
        else:
            total = conn.execute(f"SELECT COUNT(*) FROM v_sightings_dedup WHERE 1=1{family_sql}", family_args).fetchone()[0]

    return {
        "hour":  _count(1),
        "today": _count(24),
        "week":  _count(24 * 7),
        "month": _count(24 * 30),
        "ytd":   ytd,
        "total": total,
    }


def get_hourly_summary(hours: float = 1.0) -> dict:
    """
    Compact recap for the Teams hourly card. Returns:
        {
          "window_hours": 1.0,
          "count_window": int,         # sightings in the window
          "count_today":  int,         # last 24h
          "count_week":   int,         # last 7 days
          "sightings":    [ {tail, label, origin, dest, distance_nm,
                             arrived_utc, atlas_signal, watch_hit}, ... ],
          "mustangs":     [ {tail, origin, dest, distance_nm, operator,
                             arrived_utc, arrived_local, tracking_url,
                             chain_hit}, ... ],   # adjacent-tier up-purchase feed
          "mustangs_window": int,
          "mustangs_today":  int,
          "mustangs_week":   int,
        }
    `atlas_signal` is one of "range_win", "operational", "beyond", or "" if no
    distance / cfg available. `watch_hit` is True if the trip touched a watched
    airport or tail (for highlighting). Sightings sorted by arrived_utc DESC.
    """
    import atlas_config as _ac
    import watch_airports as _wa
    import watch_tails    as _wt
    now    = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source, flight_id, tail_number, ac_type, ac_subvariant, origin_icao, dest_icao,
                   distance_nm, arrived_utc, operator, tracking_url
            FROM v_sightings_dedup
            WHERE arrived_utc >= ?
            ORDER BY arrived_utc DESC
            LIMIT 25
            """,
            (cutoff,),
        ).fetchall()
        count_window = conn.execute(
            "SELECT COUNT(*) FROM v_sightings_dedup WHERE arrived_utc >= ?",
            (cutoff,),
        ).fetchone()[0]
        count_today = conn.execute(
            "SELECT COUNT(*) FROM v_sightings_dedup WHERE arrived_utc >= ?",
            ((now - timedelta(hours=24)).isoformat(),),
        ).fetchone()[0]
        count_week = conn.execute(
            "SELECT COUNT(*) FROM v_sightings_dedup WHERE arrived_utc >= ?",
            ((now - timedelta(days=7)).isoformat(),),
        ).fetchone()[0]

    watched_airports = set(_wa.get_watch_list())
    watched_tails    = set(_wt.get_watch_list())

    import airports as _ap
    sightings: list[dict] = []
    for r in rows:
        sub = (r["ac_subvariant"] or "").upper()
        cfg = _ac.atlas_cfg(r["ac_type"] or "", sub)
        baseline = cfg.get("baseline_nm") or 0
        gain     = cfg.get("atlas_gain_nm") or 0
        dist     = r["distance_nm"] or 0
        signal   = ""
        if dist and baseline:
            if dist > baseline + gain:
                signal = "beyond"
            elif dist > baseline:
                signal = "range_win"
            else:
                signal = "operational"
        tail_up = (r["tail_number"] or "").upper()
        origin  = (r["origin_icao"] or "").upper()
        dest    = (r["dest_icao"]   or "").upper()
        watch_hit = (tail_up in watched_tails) or \
                    (origin in watched_airports) or (dest in watched_airports)
        arrived_utc_str = r["arrived_utc"] or ""
        arrived_local   = _ap.local_time_at_icao(arrived_utc_str, dest) if arrived_utc_str else ""
        sightings.append({
            "tail":           tail_up or "—",
            "label":          cfg.get("label") or r["ac_type"] or "?",
            "origin":         origin or "?",
            "dest":           dest or "?",
            "distance_nm":    int(dist) if dist else None,
            "arrived_utc":    arrived_utc_str,
            "arrived_local":  arrived_local,
            "operator":       r["operator"] or "",
            "atlas_signal":   signal,
            "watch_hit":      watch_hit,
            "tracking_url":   resolve_tracking_url(
                r["source"], r["flight_id"], r["tracking_url"], r["tail_number"]
            ),
        })

    # ── Adjacent-tier feed: Mustang (up-purchase) movement ─────────────────
    # Raw sightings query since v_sightings_dedup filters to scope='atlas'.
    # We surface these separately in the recap card so the sales team sees the
    # full Mustang activity firehose (not just chain-worthy trips).
    with _connect() as conn:
        m_rows = conn.execute(
            """
            SELECT source, flight_id, tail_number, ac_type, origin_icao, dest_icao,
                   distance_nm, arrived_utc, operator, tracking_url
            FROM sightings
            WHERE scope_tier = 'up'
              AND arrived_utc >= ?
            ORDER BY arrived_utc DESC
            LIMIT 25
            """,
            (cutoff,),
        ).fetchall()
        m_window = conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE scope_tier='up' AND arrived_utc >= ?",
            (cutoff,),
        ).fetchone()[0]
        m_today = conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE scope_tier='up' AND arrived_utc >= ?",
            ((now - timedelta(hours=24)).isoformat(),),
        ).fetchone()[0]
        m_week = conn.execute(
            "SELECT COUNT(*) FROM sightings WHERE scope_tier='up' AND arrived_utc >= ?",
            ((now - timedelta(days=7)).isoformat(),),
        ).fetchone()[0]

    # Mark rows already flagged as chain hits (so the recap can badge them).
    # Chain rows carry arrived_utc as a YYYY-MM-DD date-only string.
    chain_arrivals: set[tuple[str, str]] = {
        ((c["tail_number"] or "").upper(), c.get("arrived_utc") or "")
        for c in get_fuel_stop_chains(days=7, scope_tier="up")
    }

    mustangs: list[dict] = []
    for r in m_rows:
        tail_up = (r["tail_number"] or "").upper()
        origin  = (r["origin_icao"] or "").upper()
        dest    = (r["dest_icao"]   or "").upper()
        arrived_utc_str = r["arrived_utc"] or ""
        arrived_local   = _ap.local_time_at_icao(arrived_utc_str, dest) if arrived_utc_str else ""
        chain_hit = (tail_up, arrived_utc_str[:10]) in chain_arrivals
        mustangs.append({
            "tail":          tail_up or "—",
            "origin":        origin or "?",
            "dest":          dest or "?",
            "distance_nm":   int(r["distance_nm"]) if r["distance_nm"] else None,
            "operator":      r["operator"] or "",
            "arrived_utc":   arrived_utc_str,
            "arrived_local": arrived_local,
            "chain_hit":     chain_hit,
            "tracking_url":  resolve_tracking_url(
                r["source"], r["flight_id"], r["tracking_url"], r["tail_number"]
            ),
        })

    return {
        "window_hours":    hours,
        "count_window":    count_window,
        "count_today":     count_today,
        "count_week":      count_week,
        "sightings":       sightings,
        "mustangs":        mustangs,
        "mustangs_window": m_window,
        "mustangs_today":  m_today,
        "mustangs_week":   m_week,
    }


def _duration_h(dep: str | None, arr: str | None) -> float | None:
    """Flight duration in hours from ISO UTC strings. Returns None on bad input
    or sanity-failing values (<6 min or >20 hr)."""
    if not dep or not arr:
        return None
    try:
        d = datetime.fromisoformat(dep.replace("Z", "+00:00"))
        a = datetime.fromisoformat(arr.replace("Z", "+00:00"))
        h = (a - d).total_seconds() / 3600
        return round(h, 2) if 0.1 < h < 20 else None
    except Exception:                                   # noqa: BLE001
        return None


def get_fleet_comparison_by_type() -> dict:
    """
    Per-type ATLAS fleet vs flat-wing mission distance comparison.

    Focuses on "long missions" (>= 80% of type baseline) where ATLAS range
    extension matters.  Returns a dict keyed by ac_type, e.g.:

      { "C525": { "label": "CJ", "atlas": {...}, "other": {...},
                  "advantage_nm": 210, "advantage_pct": 17, ...}, ... }

    advantage_nm / advantage_pct measure the gap on long missions only —
    so the message reads: "When a long flight is required, Tamarack CJ1
    operators are flying 15% farther than flat-wing CJ1 operators."
    """
    from atlas_config import ATLAS

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ac_type, is_tamarack_fleet, distance_nm,
                   departed_utc, arrived_utc
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > 50
              AND ac_type   IS NOT NULL AND ac_type   != ''
            """
        ).fetchall()

    # Accumulate (distance, block_kts) entries per (type, fleet_flag).
    # block_kts is None when departure/arrival timestamps can't yield a
    # sane flight duration.
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        key = ((row["ac_type"] or "").upper(), row["is_tamarack_fleet"])
        dur_h = _duration_h(row["departed_utc"], row["arrived_utc"])
        kts = (row["distance_nm"] / dur_h) if (dur_h and dur_h > 0) else None
        buckets.setdefault(key, []).append({"d": row["distance_nm"], "kts": kts})

    results: dict[str, dict] = {}
    for ac_type, cfg in ATLAS.items():
        baseline      = cfg["baseline_nm"]
        atlas_nm      = baseline + cfg["atlas_gain_nm"]
        long_threshold = baseline * 0.80

        atlas_all = buckets.get((ac_type, 1), [])
        # "other" = neither tagged (NULL) nor removed (2); removed shown separately
        other_all = (buckets.get((ac_type, None), [])
                     + buckets.get((ac_type, 0), []))
        removed_all = buckets.get((ac_type, 2), [])

        atlas_long   = [e for e in atlas_all   if e["d"] >= long_threshold]
        other_long   = [e for e in other_all   if e["d"] >= long_threshold]
        removed_long = [e for e in removed_all if e["d"] >= long_threshold]

        def _stats(all_e: list[dict], long_e: list[dict]) -> dict | None:
            if not all_e:
                return None
            all_d  = [e["d"] for e in all_e]
            long_d = [e["d"] for e in long_e]
            kts_vals = [e["kts"] for e in all_e if e["kts"] is not None]
            block_kts = round(sum(kts_vals) / len(kts_vals)) if kts_vals else None
            return {
                "count":      len(all_d),
                "avg":        round(sum(all_d)  / len(all_d)),
                "max":        round(max(all_d)),
                "long_count": len(long_d),
                "long_avg":   round(sum(long_d) / len(long_d)) if long_d else None,
                "long_max":   round(max(long_d)) if long_d else None,
                "hot_pct":    round(100 * sum(1 for d in all_d if d > baseline) / len(all_d)),
                "block_kts":  block_kts,
            }

        atlas_stats   = _stats(atlas_all,   atlas_long)
        other_stats   = _stats(other_all,   other_long)
        removed_stats = _stats(removed_all, removed_long)

        # Advantage computed on LONG missions only — that's the sales story
        adv_nm  = None
        adv_pct = None
        if (atlas_stats and other_stats
                and atlas_stats.get("long_avg") and other_stats.get("long_avg")
                and other_stats["long_avg"] > 0):
            adv_nm  = atlas_stats["long_avg"] - other_stats["long_avg"]
            adv_pct = round(100 * adv_nm / other_stats["long_avg"])

        if atlas_stats or other_stats:
            results[ac_type] = {
                "label":         cfg["label"],
                "baseline_nm":   baseline,
                "atlas_nm":      atlas_nm,
                "atlas":         atlas_stats,
                "other":         other_stats,
                "removed":       removed_stats,
                "advantage_nm":  adv_nm,
                "advantage_pct": adv_pct,
            }

    return results


def get_fleet_comparison_by_perfgroup() -> list[dict]:
    """
    Per-performance-group ATLAS fleet vs flat-wing mission distance comparison.

    Returns a list (one entry per group, in PERF_GROUPS display order). Each
    entry has the same shape as a single value from get_fleet_comparison_by_type()
    plus `key`, `label`, `subvariants`. Aircraft rows are bucketed by their
    `ac_subvariant`; rows whose subvariant is unknown fall back to the ICAO
    `ac_type`-based group inference (see atlas_config.perf_group_for).
    """
    from atlas_config import PERF_GROUPS, perf_group_for

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ac_type, ac_subvariant, is_tamarack_fleet, distance_nm,
                   departed_utc, arrived_utc
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > 50
              AND (ac_type IS NOT NULL AND ac_type != '')
            """
        ).fetchall()

    # Bucket (distance, block_kts) entries by (perf_group_key, fleet_flag).
    # block_kts is None when timestamps can't yield a sane flight duration.
    buckets: dict[tuple[str, object], list[dict]] = {}
    for row in rows:
        ac  = (row["ac_type"] or "").upper()
        sv  = (row["ac_subvariant"] or "").upper()
        if ac == "C25C" or sv == "CJ4":
            continue   # CJ4 not in scope
        gk = perf_group_for(ac, sv)
        if not gk:
            continue
        dur_h = _duration_h(row["departed_utc"], row["arrived_utc"])
        kts = (row["distance_nm"] / dur_h) if (dur_h and dur_h > 0) else None
        buckets.setdefault((gk, row["is_tamarack_fleet"]), []).append({"d": row["distance_nm"], "kts": kts})

    out: list[dict] = []
    for g in PERF_GROUPS:
        gk        = g["key"]
        baseline  = g["baseline_nm"]
        atlas_nm  = baseline + g["atlas_gain_nm"]
        long_threshold = baseline * 0.80

        atlas_all   = buckets.get((gk, 1), [])
        other_all   = (buckets.get((gk, None), [])
                       + buckets.get((gk, 0), []))
        removed_all = buckets.get((gk, 2), [])

        atlas_long   = [e for e in atlas_all   if e["d"] >= long_threshold]
        other_long   = [e for e in other_all   if e["d"] >= long_threshold]
        removed_long = [e for e in removed_all if e["d"] >= long_threshold]

        def _stats(all_e: list[dict], long_e: list[dict]) -> dict | None:
            if not all_e:
                return None
            all_d  = [e["d"] for e in all_e]
            long_d = [e["d"] for e in long_e]
            kts_vals = [e["kts"] for e in all_e if e["kts"] is not None]
            block_kts = round(sum(kts_vals) / len(kts_vals)) if kts_vals else None
            return {
                "count":      len(all_d),
                "avg":        round(sum(all_d) / len(all_d)),
                "max":        round(max(all_d)),
                "long_count": len(long_d),
                "long_avg":   round(sum(long_d) / len(long_d)) if long_d else None,
                "long_max":   round(max(long_d)) if long_d else None,
                "hot_pct":    round(100 * sum(1 for d in all_d if d > baseline) / len(all_d)),
                "block_kts":  block_kts,
            }

        atlas_stats   = _stats(atlas_all,   atlas_long)
        other_stats   = _stats(other_all,   other_long)
        removed_stats = _stats(removed_all, removed_long)

        adv_nm  = None
        adv_pct = None
        if (atlas_stats and other_stats
                and atlas_stats.get("long_avg") and other_stats.get("long_avg")
                and other_stats["long_avg"] > 0):
            adv_nm  = atlas_stats["long_avg"] - other_stats["long_avg"]
            adv_pct = round(100 * adv_nm / other_stats["long_avg"])

        out.append({
            "key":           gk,
            "label":         g["label"],
            "subvariants":   list(g["subvariants"]),
            "baseline_nm":   baseline,
            "atlas_nm":      atlas_nm,
            "atlas":         atlas_stats,
            "other":         other_stats,
            "removed":       removed_stats,
            "advantage_nm":  adv_nm,
            "advantage_pct": adv_pct,
        })

    return out


def get_insights(region: str | None = None) -> dict:
    """
    Compute fleet mission profile statistics for the insights dashboard.
    Uses all sightings with known distance_nm.

    Aggregates by the 5 canonical performance groups (CJ/CJ1, CJ1+/M2, CJ2,
    CJ2+, CJ3/CJ3+). CJ4 (C25C) is filtered out — not in ATLAS scope.

    Optional ``region`` filter (``'NA'`` | ``'EU_UK'`` | ``'OTHER'``) narrows
    the dataset to a single sales-region bucket for the top-of-page region
    toggle. ``None`` = all regions pooled (default).
    """
    from atlas_config import PERF_GROUPS, perf_group_for, perf_group_info
    from collections import Counter

    _HARD_MAX_NM = 2500

    params: list = [_HARD_MAX_NM]
    region_sql = ""
    if region in ("NA", "EU_UK", "OTHER"):
        region_sql = " AND region = ? "
        params.append(region)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ac_type, ac_subvariant, distance_nm, distance_validated,
                   distance_nm_validated, departed_utc, arrived_utc,
                   origin_icao, dest_icao, operator
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > 0
              AND distance_nm <= ?
              AND dest_icao IS NOT NULL AND dest_icao != ''
              {region_sql}
            """,
            tuple(params),
        ).fetchall()

    # Filter out CJ4/C25C up front
    rows = [r for r in rows
            if (r["ac_type"] or "").upper() != "C25C"
            and (r["ac_subvariant"] or "").upper() != "CJ4"]

    # Suspect-zone gate: for near-max flights, require track validation.
    # NULL = unchecked → drop from insights until the background enricher
    # gets to it. 0 = phantom → drop. 1 = validated → keep, and use the
    # track-derived distance if we have it.
    keep = []
    for r in rows:
        raw = r["distance_nm"]
        if raw >= _DISTANCE_SUSPECT_NM:
            if r["distance_validated"] != 1:
                continue
            if r["distance_nm_validated"]:
                # Overlay the track-derived actual distance
                r = dict(r)
                r["distance_nm"] = r["distance_nm_validated"]
        keep.append(r)
    rows = keep

    if not rows:
        return {}

    dur_by_row = [_duration_h(r["departed_utc"], r["arrived_utc"]) for r in rows]
    durations = [h for h in dur_by_row if h]
    distances = [r["distance_nm"] for r in rows]

    # Per-flight ground speed (kts), simple arithmetic mean across the fleet —
    # matches the per-group block_kts methodology (each flight counts equally).
    fleet_trip_kts = [
        r["distance_nm"] / h
        for r, h in zip(rows, dur_by_row)
        if r["distance_nm"] and h and h > 0
    ]
    avg_ground_kts = round(sum(fleet_trip_kts) / len(fleet_trip_kts)) if fleet_trip_kts else None

    # Per-performance-group stats (5 buckets)
    by_group: dict[str, dict] = {}
    for r in rows:
        ac  = (r["ac_type"] or "").upper()
        sv  = (r["ac_subvariant"] or "").upper()
        gk  = perf_group_for(ac, sv)
        if not gk:
            continue
        g   = perf_group_info(gk) or {}
        baseline = g.get("baseline_nm")
        atlas_nm = (baseline or 0) + (g.get("atlas_gain_nm") or 0) if baseline else None
        if gk not in by_group:
            by_group[gk] = {"label": g.get("label", gk), "distances": [], "durations": [],
                            "block_pairs": [],   # (dist_nm, dur_h) for trips with both
                            "hot": 0, "warm": 0, "other": 0,
                            "baseline": baseline, "atlas_nm": atlas_nm}
        by_group[gk]["distances"].append(r["distance_nm"])
        dur = _duration_h(r["departed_utc"], r["arrived_utc"])
        if dur:
            by_group[gk]["durations"].append(dur)
            if r["distance_nm"] and r["distance_nm"] > 0:
                by_group[gk]["block_pairs"].append((r["distance_nm"], dur))
        # Trip tier relative to this group's baseline
        if baseline:
            if r["distance_nm"] > baseline:
                by_group[gk]["hot"] += 1
            elif r["distance_nm"] > 0.80 * baseline:
                by_group[gk]["warm"] += 1
            else:
                by_group[gk]["other"] += 1
        else:
            by_group[gk]["other"] += 1

    # Build per-group stats in canonical PERF_GROUPS display order
    type_stats: dict[str, dict] = {}
    for g in PERF_GROUPS:
        gk = g["key"]
        d  = by_group.get(gk)
        if not d:
            continue
        dists, durs = d["distances"], d["durations"]
        pairs = d["block_pairs"]
        # Per-flight block speed (kts) = trip distance / trip duration, then
        # simple arithmetic mean across the fleet. Each flight counts equally,
        # regardless of length. NOT a duration-weighted average.
        per_trip_kts = [p[0] / p[1] for p in pairs if p[1] > 0]
        block_kts = round(sum(per_trip_kts) / len(per_trip_kts)) if per_trip_kts else None
        type_stats[gk] = {
            "label":       d["label"],
            "count":       len(dists),
            "avg_dist":    round(sum(dists) / len(dists)) if dists else 0,
            "max_dist":    round(max(dists)) if dists else 0,
            "avg_dur_h":   round(sum(durs) / len(durs), 1) if durs else None,
            "block_kts":   block_kts,
            "hot":         d["hot"],
            "warm":        d["warm"],
            "other":       d["other"],
            "baseline_nm": d["baseline"],
            "atlas_nm":    d["atlas_nm"] or None,
        }

    # Distance histogram (100 nm buckets) — trim trailing empty bins
    # (keep one empty bin past the last non-zero bucket for breathing room)
    b = list(range(0, 2401, 100))
    dist_hist_full = [sum(1 for d in distances if b[i] <= d < b[i+1]) for i in range(len(b)-1)]
    last_nonzero  = max((i for i, v in enumerate(dist_hist_full) if v > 0), default=-1)
    keep          = min(len(dist_hist_full), last_nonzero + 2)
    dist_hist     = dist_hist_full[:keep] if keep > 0 else dist_hist_full[:1]
    hist_labels   = [f"{b[i]}–{b[i]+100}" for i in range(keep)] if keep > 0 else [f"{b[0]}–{b[0]+100}"]

    # Duration histogram (0.25h buckets, 0–6h)
    db2 = [i * 0.25 for i in range(25)]
    dur_hist = [sum(1 for h in durations if db2[i] <= h < db2[i+1]) for i in range(len(db2)-1)]
    dur_labels = [f"{db2[i]:g}–{db2[i+1]:g}h" for i in range(len(db2)-1)]

    # Arrival hour at the destination's LOCAL time (falls back to UTC if the
    # airport's IANA timezone can't be resolved).
    import airports as _ap
    try:
        from zoneinfo import ZoneInfo
    except Exception:                                    # noqa: BLE001
        ZoneInfo = None                                  # type: ignore
    _tz_cache: dict[str, object] = {}
    hour_dist = [0] * 24
    for r in rows:
        au = (r["arrived_utc"] or "").replace("Z", "+00:00")
        if not au:
            continue
        try:
            dt_utc = datetime.fromisoformat(au)
        except ValueError:
            continue
        dest = (r["dest_icao"] or "").upper()
        local_hour = None
        if dest and ZoneInfo is not None:
            tz_obj = _tz_cache.get(dest)
            if tz_obj is None and dest not in _tz_cache:
                tz_name = _ap._icao_timezone(dest)
                tz_obj  = ZoneInfo(tz_name) if tz_name else None
                _tz_cache[dest] = tz_obj
            if tz_obj is not None:
                local_hour = dt_utc.astimezone(tz_obj).hour
        if local_hour is None:
            local_hour = dt_utc.hour                     # fall back to UTC
        hour_dist[local_hour] += 1

    # Top airports & routes
    orig_c  = Counter(r["origin_icao"] for r in rows if r["origin_icao"])
    dest_c  = Counter(r["dest_icao"]   for r in rows if r["dest_icao"])
    route_c = Counter(f"{r['origin_icao']}→{r['dest_icao']}" for r in rows if r["origin_icao"] and r["dest_icao"])

    return {
        "total_flights":    len(distances),
        "avg_distance":     round(sum(distances) / len(distances)) if distances else 0,
        "max_distance":     round(max(distances)) if distances else 0,
        "avg_duration_h":   round(sum(durations) / len(durations), 1) if durations else None,
        "avg_ground_kts":   avg_ground_kts,
        "by_type":          type_stats,
        "dist_hist_labels": hist_labels,
        "dist_hist_data":   dist_hist,
        "dist_hist_pct":    _as_pct(dist_hist),
        "dur_hist_labels":  dur_labels,
        "dur_hist_data":    dur_hist,
        "dur_hist_pct":     _as_pct(dur_hist),
        "hour_dist":        hour_dist,
        "hour_dist_pct":    _as_pct(hour_dist),
        "top_origins":  [{"icao": k, "count": v} for k, v in orig_c.most_common(10)],
        "top_dests":    [{"icao": k, "count": v} for k, v in dest_c.most_common(10)],
        "top_routes":   [{"route": k, "count": v} for k, v in route_c.most_common(10)],
    }


def _as_pct(counts: list[int]) -> list[float]:
    """Convert a list of bucket counts to percentage of total (1-decimal)."""
    total = sum(counts)
    if not total:
        return [0.0] * len(counts)
    return [round(c * 100.0 / total, 1) for c in counts]


def get_insights_by_subvariant(region: str | None = None) -> dict:
    """
    Return per-sub-variant + pooled ALL views of the four filter-driven
    insights charts: distance histogram, duration histogram, arrival-hour
    histogram, and the range-limited (hot/warm/other) stacked bar. Uses the
    same plausibility gates as ``get_insights()``. CJ4 (C25C) excluded.
    Sub-variants with fewer than 5 rows are skipped so the dropdown doesn't
    show near-empty views. ``atlasChart`` labels switch to sub-variant names
    (CJ, CJ1, CJ1+, M2, CJ2, CJ2+, CJ3, CJ3+) so a filter selection shows a
    single meaningful bar instead of collapsing the whole chart.

    Optional ``region`` filter (``'NA'`` | ``'EU_UK'`` | ``'OTHER'``) narrows
    the underlying dataset to a single sales-region bucket for the
    top-of-page region toggle. ``None`` = all regions pooled (default).
    """
    from atlas_config import ATLAS_SUBVARIANT
    import airports as _ap
    try:
        from zoneinfo import ZoneInfo
    except Exception:                                    # noqa: BLE001
        ZoneInfo = None                                  # type: ignore

    _HARD_MAX_NM = 2500
    params: list = [_HARD_MAX_NM]
    region_sql = ""
    if region in ("NA", "EU_UK", "OTHER"):
        region_sql = " AND region = ? "
        params.append(region)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ac_type, ac_subvariant, distance_nm, distance_validated,
                   distance_nm_validated, departed_utc, arrived_utc,
                   origin_icao, dest_icao
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > 0
              AND distance_nm <= ?
              AND dest_icao IS NOT NULL AND dest_icao != ''
              {region_sql}
            """,
            tuple(params),
        ).fetchall()

    _SV_ORDER = ["CJ", "CJ1", "CJ1+", "M2", "CJ2", "CJ2+", "CJ3", "CJ3+"]
    _SV_BY_LABEL = {v["label"]: k for k, v in ATLAS_SUBVARIANT.items()}

    def _sv_label(ac_type: str, sv_raw: str) -> str | None:
        cfg = ATLAS_SUBVARIANT.get(sv_raw)
        if cfg:
            return cfg["label"]
        return None

    def _baseline_for(sv_label: str) -> int | None:
        key = _SV_BY_LABEL.get(sv_label)
        if not key:
            return None
        return ATLAS_SUBVARIANT[key].get("baseline_nm")

    # Bucket rows per sub-variant, apply the same suspect-zone gate as get_insights.
    by_sv: dict[str, list[dict]] = {}
    for r in rows:
        ac = (r["ac_type"] or "").upper()
        sv_raw = (r["ac_subvariant"] or "").upper()
        if ac == "C25C" or sv_raw == "CJ4":
            continue
        raw = r["distance_nm"]
        if raw >= _DISTANCE_SUSPECT_NM:
            if r["distance_validated"] != 1:
                continue
            r = dict(r)
            if r.get("distance_nm_validated"):
                r["distance_nm"] = r["distance_nm_validated"]
        sv = _sv_label(ac, sv_raw)
        if not sv:
            continue
        by_sv.setdefault(sv, []).append(r)

    # Bucket definitions shared with get_insights so the two datasets align.
    dist_edges = list(range(0, 2401, 100))
    dur_edges  = [i * 0.25 for i in range(25)]
    dur_labels = [f"{dur_edges[i]:g}–{dur_edges[i+1]:g}h" for i in range(len(dur_edges)-1)]

    _tz_cache: dict[str, object] = {}

    def _local_hour(dt_utc, dest: str) -> int:
        if dest and ZoneInfo is not None:
            tz_obj = _tz_cache.get(dest)
            if tz_obj is None and dest not in _tz_cache:
                tz_name = _ap._icao_timezone(dest)
                tz_obj  = ZoneInfo(tz_name) if tz_name else None
                _tz_cache[dest] = tz_obj
            if tz_obj is not None:
                return dt_utc.astimezone(tz_obj).hour
        return dt_utc.hour

    def _build(view_rows: list[dict]) -> dict:
        distances = [r["distance_nm"] for r in view_rows if r["distance_nm"]]
        # Distance histogram — trim trailing empty tail
        dist_full = [sum(1 for d in distances if dist_edges[i] <= d < dist_edges[i+1])
                     for i in range(len(dist_edges)-1)]
        last_nz = max((i for i, v in enumerate(dist_full) if v > 0), default=-1)
        keep = min(len(dist_full), last_nz + 2)
        dist_hist   = dist_full[:keep] if keep > 0 else dist_full[:1]
        dist_labels = [f"{dist_edges[i]}–{dist_edges[i]+100}" for i in range(len(dist_hist))]

        # Duration histogram (0–6h in 0.25h bins)
        durs = [d for d in (_duration_h(r["departed_utc"], r["arrived_utc"])
                            for r in view_rows) if d]
        dur_hist = [sum(1 for h in durs if dur_edges[i] <= h < dur_edges[i+1])
                    for i in range(len(dur_edges)-1)]

        # Arrival hour at destination local time
        hour_dist = [0] * 24
        for r in view_rows:
            au = (r["arrived_utc"] or "").replace("Z", "+00:00")
            if not au:
                continue
            try:
                dt_utc = datetime.fromisoformat(au)
            except ValueError:
                continue
            hour_dist[_local_hour(dt_utc, (r["dest_icao"] or "").upper())] += 1

        return {
            "dist_hist_labels": dist_labels,
            "dist_hist_data":   dist_hist,
            "dist_hist_pct":    _as_pct(dist_hist),
            "dur_hist_labels":  dur_labels,
            "dur_hist_data":    dur_hist,
            "dur_hist_pct":     _as_pct(dur_hist),
            "hour_dist":        hour_dist,
            "hour_dist_pct":    _as_pct(hour_dist),
            "total_flights":    len(view_rows),
        }

    # Range-limited stacked bar: one column per sub-variant, per view.
    ordered_svs = [sv for sv in _SV_ORDER if sv in by_sv]

    def _range_bars(subset_by_sv: dict[str, list[dict]]) -> dict:
        labels, hot, warm, other = [], [], [], []
        for sv in ordered_svs:
            if sv not in subset_by_sv:
                continue
            base = _baseline_for(sv)
            h = w = o = 0
            for r in subset_by_sv[sv]:
                d = r["distance_nm"]
                if base:
                    if d > base:            h += 1
                    elif d > 0.80 * base:   w += 1
                    else:                   o += 1
                else:
                    o += 1
            labels.append(sv); hot.append(h); warm.append(w); other.append(o)
        return {"type_labels": labels, "hot_data": hot,
                "warm_data": warm, "other_data": other}

    # ALL view: pooled histograms, all sub-variants shown in the stacked bar.
    all_rows = [r for rlist in by_sv.values() for r in rlist]
    all_view = _build(all_rows)
    all_view.update(_range_bars(by_sv))

    views: dict[str, dict] = {"ALL": all_view}
    for sv in ordered_svs:
        subset = by_sv.get(sv, [])
        if len(subset) < 5:
            continue
        v = _build(subset)
        v.update(_range_bars({sv: subset}))
        views[sv] = v

    return {
        "views":        views,
        "sub_variants": ["ALL"] + [sv for sv in ordered_svs if sv in views],
    }


def get_eu_insights() -> dict:
    """
    EU-specific analytics: flight-level distribution, ICAO semicircular
    cruise-rule compliance (eastbound=odd / westbound=even), country-pair
    activity, EU operator leaderboard, EU-scaled distance histogram, and a
    NA-vs-EU comparison strip. CJ4 (C25C) filtered out — not in ATLAS scope.

    Uses `v_sightings_dedup` (same dedup rules as `/insights`). Semicircular
    analysis only counts flights with `sustained_top_alt_ft >= 10000` (below
    that is either short-leg cruise or ATC-vectored — the rule doesn't
    strictly apply until transition altitude).
    """
    from atlas_config import PERF_GROUPS, perf_group_for, perf_group_info
    from airports import icao_coords, airport_country
    from collections import Counter, defaultdict
    import math

    _HARD_MAX_NM = 2500

    def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        x = math.sin(dl) * math.cos(p2)
        y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

    with _connect() as conn:
        eu_rows = conn.execute(
            """
            SELECT tail_number, ac_type, ac_subvariant, operator,
                   origin_icao, dest_icao, distance_nm, distance_validated,
                   distance_nm_validated, departed_utc, arrived_utc,
                   sustained_top_alt_ft
            FROM v_sightings_dedup
            WHERE region = 'EU_UK'
              AND distance_nm IS NOT NULL AND distance_nm > 0
              AND distance_nm <= ?
              AND dest_icao IS NOT NULL AND dest_icao != ''
            """,
            (_HARD_MAX_NM,),
        ).fetchall()
        na_rows = conn.execute(
            """
            SELECT distance_nm, distance_validated, distance_nm_validated,
                   departed_utc, arrived_utc, sustained_top_alt_ft
            FROM v_sightings_dedup
            WHERE region = 'NA'
              AND distance_nm IS NOT NULL AND distance_nm > 0
              AND distance_nm <= ?
              AND dest_icao IS NOT NULL AND dest_icao != ''
            """,
            (_HARD_MAX_NM,),
        ).fetchall()

    def _drop_cj4(rows):
        return [r for r in rows
                if (r["ac_type"] or "").upper() != "C25C"
                and (r["ac_subvariant"] or "").upper() != "CJ4"] if rows and "ac_type" in rows[0].keys() else rows

    eu_rows = _drop_cj4(eu_rows)

    def _suspect_gate(rows):
        keep = []
        for r in rows:
            raw = r["distance_nm"]
            if raw >= _DISTANCE_SUSPECT_NM:
                if r["distance_validated"] != 1:
                    continue
                if r["distance_nm_validated"]:
                    r = dict(r)
                    r["distance_nm"] = r["distance_nm_validated"]
            keep.append(r)
        return keep

    eu_rows = _suspect_gate(eu_rows)
    na_rows = _suspect_gate(na_rows)

    if not eu_rows:
        return {}

    # ── Headline scalars ────────────────────────────────────────────────
    tails     = {r["tail_number"] for r in eu_rows if r["tail_number"]}
    operators = {(r["operator"] or "").strip() for r in eu_rows if (r["operator"] or "").strip()}
    distances = [r["distance_nm"] for r in eu_rows]
    durations = [_duration_h(r["departed_utc"], r["arrived_utc"]) for r in eu_rows]
    durations = [d for d in durations if d]

    def _avg_fl(rows) -> int | None:
        alts = [r["sustained_top_alt_ft"] for r in rows if r["sustained_top_alt_ft"]]
        if not alts:
            return None
        return round(sum(alts) / len(alts) / 100)             # FL = alt / 100

    eu_avg_fl = _avg_fl(eu_rows)
    na_avg_fl = _avg_fl(na_rows)
    na_dists  = [r["distance_nm"] for r in na_rows]
    na_durs   = [d for d in (_duration_h(r["departed_utc"], r["arrived_utc"]) for r in na_rows) if d]

    # ── Chain rates (chain hits ÷ flights) ──────────────────────────────
    eu_chains = get_fuel_stop_chains(days=180, region="EU_UK")
    na_chains = get_fuel_stop_chains(days=180, region="NA")
    eu_chain_rate = round(100.0 * len(eu_chains) / len(eu_rows), 2) if eu_rows else 0.0
    na_chain_rate = round(100.0 * len(na_chains) / len(na_rows), 2) if na_rows else 0.0

    # ── Flight-level histogram (1000 ft bins, FL200–FL450) split by dir ──
    # For every EU flight with sustained cruise ≥ FL100, bin the altitude
    # and tag by bearing direction. Overlaying east vs west on the same
    # 1000 ft grid makes semicircular compliance visible as an alternating
    # odd/even pattern.
    fl_min, fl_max = 200, 450                                 # in units of FL (1/100 ft)
    n_bins   = (fl_max - fl_min) // 10 + 1                    # 26 bins @ 1000 ft each
    fl_east  = [0] * n_bins
    fl_west  = [0] * n_bins
    semi = {"east_odd": 0, "east_even": 0, "west_odd": 0, "west_even": 0}
    country_pairs: Counter = Counter()
    country_dests: Counter = Counter()
    for r in eu_rows:
        # Country pair + landing (always tabulated, even without altitude)
        oc = airport_country(r["origin_icao"] or "")
        dc = airport_country(r["dest_icao"]   or "")
        if dc:
            country_dests[dc] += 1
        if oc and dc and oc != dc:
            country_pairs[f"{oc}→{dc}"] += 1
        # Flight-level + semicircular only when we know cruise altitude
        alt = r["sustained_top_alt_ft"]
        if not alt or alt < 10000:
            continue
        origin = icao_coords(r["origin_icao"] or "")
        dest   = icao_coords(r["dest_icao"]   or "")
        if not origin or not dest:
            continue
        brg = _bearing(origin[0], origin[1], dest[0], dest[1])
        eastbound = 0.0 <= brg < 180.0
        fl_1000s  = round(alt / 1000.0)                       # e.g. 35 for FL350
        is_odd    = bool(fl_1000s % 2)
        # Histogram bin
        fl_val    = fl_1000s * 10                             # e.g. FL350 → 350
        if fl_min <= fl_val <= fl_max:
            idx = (fl_val - fl_min) // 10
            (fl_east if eastbound else fl_west)[idx] += 1
        # Semicircular tally
        if eastbound and is_odd:
            semi["east_odd"] += 1
        elif eastbound:
            semi["east_even"] += 1
        elif is_odd:
            semi["west_odd"] += 1
        else:
            semi["west_even"] += 1

    fl_labels = [f"FL{fl_min + i*10:03d}" for i in range(n_bins)]
    semi_total     = sum(semi.values())
    semi_compliant = semi["east_odd"] + semi["west_even"]
    semi_pct = round(100.0 * semi_compliant / semi_total, 1) if semi_total else 0.0

    # ── Country pair + landing leaderboards ─────────────────────────────
    top_pairs = [{"pair": k, "n": v} for k, v in country_pairs.most_common(20)]
    top_dests = [{"iso": k, "n": v} for k, v in country_dests.most_common(20)]

    # ── EU-scaled distance histogram (0–1200 nm, 50 nm bins) ────────────
    dist_edges = list(range(0, 1201, 50))                     # 25 edges → 24 bins
    dist_data  = [sum(1 for d in distances if dist_edges[i] <= d < dist_edges[i+1])
                  for i in range(len(dist_edges) - 1)]
    dist_labels = [f"{dist_edges[i]}–{dist_edges[i+1]}" for i in range(len(dist_edges) - 1)]

    # ── Operator leaderboard ────────────────────────────────────────────
    op_bucket: dict[str, dict] = defaultdict(lambda: {"flights": 0, "tails": set(), "countries": set()})
    for r in eu_rows:
        op = (r["operator"] or "").strip()
        if not op:
            continue
        op_bucket[op]["flights"] += 1
        if r["tail_number"]:
            op_bucket[op]["tails"].add(r["tail_number"])
        dc = airport_country(r["dest_icao"] or "")
        if dc:
            op_bucket[op]["countries"].add(dc)
    top_operators = sorted(
        ({"name": op, "flights": b["flights"], "tails": len(b["tails"]),
          "countries": len(b["countries"])} for op, b in op_bucket.items()),
        key=lambda x: (-x["flights"], -x["tails"], x["name"]),
    )[:15]

    # ── ATLAS math per perf group (EU-scoped) ───────────────────────────
    by_group: dict[str, dict] = {}
    for r in eu_rows:
        ac  = (r["ac_type"] or "").upper()
        sv  = (r["ac_subvariant"] or "").upper()
        gk  = perf_group_for(ac, sv)
        if not gk:
            continue
        g   = perf_group_info(gk) or {}
        baseline = g.get("baseline_nm")
        atlas_nm = (baseline or 0) + (g.get("atlas_gain_nm") or 0) if baseline else None
        if gk not in by_group:
            by_group[gk] = {"label": g.get("label", gk), "count": 0,
                            "hot": 0, "warm": 0, "other": 0,
                            "baseline_nm": baseline, "atlas_nm": atlas_nm}
        by_group[gk]["count"] += 1
        if baseline:
            if r["distance_nm"] > baseline:
                by_group[gk]["hot"] += 1
            elif r["distance_nm"] > 0.80 * baseline:
                by_group[gk]["warm"] += 1
            else:
                by_group[gk]["other"] += 1
        else:
            by_group[gk]["other"] += 1
    by_type = {g["key"]: by_group[g["key"]] for g in PERF_GROUPS if g["key"] in by_group}

    return {
        # Headline
        "total_flights":     len(eu_rows),
        "unique_tails":      len(tails),
        "unique_operators":  len(operators),
        "unique_countries":  len(country_dests),
        "unique_pairs":      len(country_pairs),
        "avg_distance":      round(sum(distances) / len(distances)),
        "avg_duration_h":    round(sum(durations) / len(durations), 1) if durations else None,
        "avg_cruise_fl":     eu_avg_fl,
        "max_distance":      round(max(distances)),
        # NA vs EU comparison strip
        "na_flights":        len(na_rows),
        "na_avg_distance":   round(sum(na_dists) / len(na_dists)) if na_dists else None,
        "na_avg_duration_h": round(sum(na_durs) / len(na_durs), 1) if na_durs else None,
        "na_avg_cruise_fl":  na_avg_fl,
        "eu_chain_rate":     eu_chain_rate,
        "na_chain_rate":     na_chain_rate,
        # Flight-level charts
        "fl_labels":         fl_labels,
        "fl_eastbound":      fl_east,
        "fl_westbound":      fl_west,
        # Semicircular breakdown
        "semi_east_odd":     semi["east_odd"],
        "semi_east_even":    semi["east_even"],
        "semi_west_odd":     semi["west_odd"],
        "semi_west_even":    semi["west_even"],
        "semi_total":        semi_total,
        "semi_compliant_pct": semi_pct,
        # Country activity
        "top_pairs":         top_pairs,
        "top_dests":         top_dests,
        # Distance histogram (EU scale)
        "dist_labels":       dist_labels,
        "dist_data":         dist_data,
        # Operator leaderboard
        "top_operators":     top_operators,
        # ATLAS math
        "by_type":           by_type,
        # Chains (last 180 d, EU-only)
        "chains":            eu_chains,
        "chain_count":       len(eu_chains),
    }


def get_block_speed_scatter(min_distance_nm: int = 50,
                            region: str | None = None) -> dict:
    """
    Return per-flight (distance_nm, duration_h, block_kts) datapoints for
    the block-speed scatter charts, split into an "ALL" pooled view plus one
    view per CJ-family sub-variant (CJ, CJ1, CJ1+, M2, CJ2, CJ2+, CJ3, CJ3+).
    Each view contains ATLAS and Flat-wing point sets plus a 40-point sample
    of a non-linear saturating fit ``y = a·x/(b+x)`` of block_kts vs distance
    (``atlas_trend_d`` / ``flat_trend_d``) and duration (``atlas_trend_h`` /
    ``flat_trend_h``). Fits are linearized via ``1/y = 1/a + (b/a)·(1/x)``
    for a plain least-squares solve; falls back to logarithmic if the
    rational parameters come out unphysical. Sub-variant views make the
    dashboard's sub-variant filter dropdown a like-for-like comparison
    instead of the mixed-fleet pool. CJ4 (C25C) excluded. Caps each series
    at 4,000 points to keep the chart payload small.

    Optional ``region`` filter (``'NA'`` | ``'EU_UK'`` | ``'OTHER'``) narrows
    the underlying dataset to a single sales-region bucket for the
    top-of-page region toggle. ``None`` = all regions pooled (default).
    """
    params: list = [min_distance_nm]
    region_sql = ""
    if region in ("NA", "EU_UK", "OTHER"):
        region_sql = " AND region = ? "
        params.append(region)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT distance_nm, departed_utc, arrived_utc,
                   COALESCE(is_tamarack_fleet, 0) AS flag,
                   ac_type, ac_subvariant
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm > ?
              AND departed_utc IS NOT NULL AND departed_utc != ''
              AND arrived_utc  IS NOT NULL AND arrived_utc  != ''
              {region_sql}
            """,
            tuple(params),
        ).fetchall()

    from atlas_config import ATLAS_SUBVARIANT, ATLAS as _ATLAS_ICAO

    series: dict[str, list[dict]] = {"atlas": [], "flatwing": []}
    for r in rows:
        ac = (r["ac_type"] or "").upper()
        sv_raw = (r["ac_subvariant"] or "").upper()
        if ac == "C25C" or sv_raw == "CJ4":
            continue
        dur_h = _duration_h(r["departed_utc"], r["arrived_utc"])
        if not dur_h or dur_h <= 0:
            continue
        d = float(r["distance_nm"])
        kts = round(d / dur_h, 1)
        if kts < 60 or kts > 600:
            continue
        # Sub-variant label for the filter dropdown; fall back to the ICAO
        # type-level label so tails without a resolved sub-variant still get
        # grouped somewhere.
        sv_cfg = ATLAS_SUBVARIANT.get(sv_raw)
        sv_label = (sv_cfg["label"] if sv_cfg
                    else _ATLAS_ICAO.get(ac, {}).get("label") or ac or "?")
        # Skip is_tamarack_fleet=2 (ATLAS removed) — we no longer render that
        # series and it would otherwise pollute the flat-wing bucket.
        if r["flag"] == 2:
            continue
        key = "atlas" if r["flag"] == 1 else "flatwing"
        series[key].append({"d": d, "h": round(dur_h, 2),
                            "kts": kts, "sv": sv_label})

    def _cap(lst: list[dict], n: int = 4000) -> list[dict]:
        if len(lst) <= n:
            return lst
        step = len(lst) / n
        return [lst[int(i * step)] for i in range(n)]

    def _linear_fit(us: list[float], vs: list[float]) -> tuple[float, float] | None:
        n = len(us)
        if n < 2:
            return None
        mu = sum(us) / n
        mv = sum(vs) / n
        num = sum((us[i] - mu) * (vs[i] - mv) for i in range(n))
        den = sum((us[i] - mu) ** 2 for i in range(n))
        if den == 0:
            return None
        slope = num / den
        return slope, mv - slope * mu

    def _curve_fit(points: list[dict], x_key: str,
                   n_samples: int = 40) -> list[dict] | None:
        """Rational y = a·x/(b+x) via reciprocal linearization; log fallback."""
        import math
        pts = [p for p in points if p[x_key] > 0 and p["kts"] > 0]
        if len(pts) < 8:
            return None
        xs = [p[x_key] for p in pts]
        ys = [p["kts"] for p in pts]
        x_min, x_max = min(xs), max(xs)
        if x_max <= x_min:
            return None

        curve = None
        r = _linear_fit([1.0 / x for x in xs], [1.0 / y for y in ys])
        if r:
            slope_inv, intercept_inv = r
            if intercept_inv > 0:
                a = 1.0 / intercept_inv
                b = slope_inv * a
                if a > 0 and (b + x_min) > 0:
                    curve = lambda x, a=a, b=b: a * x / (b + x)

        if curve is None:
            r2 = _linear_fit([math.log(x) for x in xs], ys)
            if not r2:
                return None
            b_log, a_log = r2
            curve = lambda x, a=a_log, b=b_log: a + b * math.log(x)

        step = (x_max - x_min) / (n_samples - 1)
        return [
            {"x": round(x_min + i * step, 2),
             "y": round(curve(x_min + i * step), 1)}
            for i in range(n_samples)
        ]

    def _build_view(pts_atlas: list[dict],
                    pts_flat:  list[dict]) -> dict:
        cap_a = _cap(pts_atlas)
        cap_f = _cap(pts_flat)
        return {
            "atlas_n":       len(pts_atlas),
            "flat_n":        len(pts_flat),
            "atlas_points_d": [{"x": p["d"], "y": p["kts"]} for p in cap_a],
            "flat_points_d":  [{"x": p["d"], "y": p["kts"]} for p in cap_f],
            "atlas_points_h": [{"x": p["h"], "y": p["kts"]} for p in cap_a],
            "flat_points_h":  [{"x": p["h"], "y": p["kts"]} for p in cap_f],
            "atlas_trend_d":  _curve_fit(cap_a, "d"),
            "flat_trend_d":   _curve_fit(cap_f, "d"),
            "atlas_trend_h":  _curve_fit(cap_a, "h"),
            "flat_trend_h":   _curve_fit(cap_f, "h"),
        }

    views: dict[str, dict] = {"ALL": _build_view(series["atlas"], series["flatwing"])}
    # Order sub-variants along the CJ family production timeline so the
    # dropdown reads naturally. Skip anything not in _SV_ORDER (ICAO-level
    # fallback labels like "CJ/CJ1/CJ1+" leak in when a tail's sub-variant
    # can't be resolved — they'd show as empty dropdown entries).
    _SV_ORDER = ["CJ", "CJ1", "CJ1+", "M2", "CJ2", "CJ2+", "CJ3", "CJ3+"]
    seen_svs = {p["sv"] for pts in series.values() for p in pts}
    ordered = [sv for sv in _SV_ORDER if sv in seen_svs]
    for sv in ordered:
        a = [p for p in series["atlas"]    if p["sv"] == sv]
        f = [p for p in series["flatwing"] if p["sv"] == sv]
        if (len(a) + len(f)) < 5:
            continue
        views[sv] = _build_view(a, f)

    return {
        "views":        views,
        "sub_variants": ["ALL"] + [sv for sv in ordered if sv in views],
    }


# Published max operating altitudes (ft) per sub-variant — for the climb chart
_PUBLISHED_CEILING_FT: dict[str, int] = {
    "CJ":      41000,
    "CJ1":     41000,
    "CJ1PLUS": 41000,
    "M2":      41000,
    "CJ2":     45000,
    "CJ2PLUS": 45000,
    "CJ3":     45000,
    "CJ3PLUS": 45000,
}


def get_climb_comparison(min_distance_nm: int = 500) -> dict:
    """
    Per-sub-variant climb-performance comparison (ATLAS vs Flat-wing).

    For each sub-variant, returns mean top altitude (ft) and mean time-to-ICA
    (initial cruise altitude, in minutes) for flights longer than
    `min_distance_nm` with climb-profile data. Long legs are the right slice
    because operators climb to true cruise altitude on those.
    Time-to-ICA is the operationally relevant metric (climb-gradient story)
    rather than time-to-10k (which is mostly piloting / departure-procedure noise).
    """
    from atlas_config import PERF_GROUPS  # noqa: F401 — sub-variant ordering source

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT UPPER(COALESCE(ac_subvariant, '')) AS sv,
                   COALESCE(is_tamarack_fleet, 0)     AS flag,
                   sustained_top_alt_ft AS top_ft,
                   time_to_initial_cruise_sec, distance_nm
            FROM v_sightings_dedup
            WHERE distance_nm IS NOT NULL AND distance_nm >= ?
              AND ac_subvariant IS NOT NULL AND ac_subvariant != ''
              AND (sustained_top_alt_ft IS NOT NULL OR time_to_initial_cruise_sec IS NOT NULL)
            """,
            (min_distance_nm,),
        ).fetchall()

    sub_order = ("CJ", "CJ1", "CJ1PLUS", "M2", "CJ2", "CJ2PLUS", "CJ3", "CJ3PLUS")
    labels = {"CJ":"CJ", "CJ1":"CJ1", "CJ1PLUS":"CJ1+", "M2":"M2",
              "CJ2":"CJ2", "CJ2PLUS":"CJ2+", "CJ3":"CJ3", "CJ3PLUS":"CJ3+"}

    # Buckets: {sv: {atlas: {top: [], tica: []}, other: {top: [], tica: []}}}
    bucket: dict[str, dict] = {sv: {"atlas": {"top": [], "tica": []},
                                    "other": {"top": [], "tica": []}} for sv in sub_order}
    for r in rows:
        sv = r["sv"]
        if sv == "CJ4" or sv not in bucket:
            continue
        key = "atlas" if r["flag"] == 1 else "other"   # treat removed (2) and null as flat-wing baseline
        if r["top_ft"] is not None:
            bucket[sv][key]["top"].append(int(r["top_ft"]))
        if r["time_to_initial_cruise_sec"] is not None:
            bucket[sv][key]["tica"].append(int(r["time_to_initial_cruise_sec"]))

    def _mean_int(xs: list[int]) -> int | None:
        return int(sum(xs) / len(xs)) if xs else None

    out: dict = {
        "labels":          [labels[sv] for sv in sub_order],
        "subvariants":     list(sub_order),
        "ceiling_ft":      [_PUBLISHED_CEILING_FT.get(sv) for sv in sub_order],
        "atlas_top_ft":    [_mean_int(bucket[sv]["atlas"]["top"])  for sv in sub_order],
        "other_top_ft":    [_mean_int(bucket[sv]["other"]["top"])  for sv in sub_order],
        "atlas_tica_min":  [round(_mean_int(bucket[sv]["atlas"]["tica"]) / 60, 1)
                            if bucket[sv]["atlas"]["tica"] else None for sv in sub_order],
        "other_tica_min":  [round(_mean_int(bucket[sv]["other"]["tica"]) / 60, 1)
                            if bucket[sv]["other"]["tica"] else None for sv in sub_order],
        "atlas_n":         [len(bucket[sv]["atlas"]["top"]) for sv in sub_order],
        "other_n":         [len(bucket[sv]["other"]["top"]) for sv in sub_order],
        "min_distance_nm": min_distance_nm,
    }
    return out


def get_operator_pursuit(top_n: int = 25, days: int = 30) -> list[dict]:
    """
    Aggregate prospect signals at the operator level. Returns top-N operators
    by aggregate composite score across their tails — the sales pursuit list.
    """
    prospects = get_prospects(days=days)
    by_op: dict[str, dict] = {}
    for p in prospects:
        op = (p.get("operator") or "").strip() or "—"
        if op == "—":
            continue
        d = by_op.setdefault(op, {
            "operator":         op,
            "tails":            set(),
            "total_composite":  0,
            "total_hot":        0,
            "total_warm":       0,
            "total_chains":     0,
            "high_da_airports": set(),
            "subvariants":      set(),
            "max_distance_nm":  0,
        })
        d["tails"].add(p["tail_number"])
        d["total_composite"] += p["composite_score"]
        d["total_hot"]       += p.get("trips_hot", 0)
        d["total_warm"]      += p.get("trips_warm", 0)
        d["total_chains"]    += p.get("n_chains", 0)
        if p.get("label"):
            d["subvariants"].add(p["label"])
        if p.get("max_distance_nm"):
            d["max_distance_nm"] = max(d["max_distance_nm"], p["max_distance_nm"])
        # high-DA airports come through prospects as part of signals; we approximate
        # via the tail's da_score scaled back to airport count (da_score = unique_icaos * 2)
        if p.get("da_score"):
            d["high_da_airports"].add(p["tail_number"])   # placeholder dedup

    out: list[dict] = []
    for op, d in by_op.items():
        out.append({
            "operator":         d["operator"],
            "tails":            sorted(d["tails"]),
            "tail_count":       len(d["tails"]),
            "total_composite":  d["total_composite"],
            "total_hot":        d["total_hot"],
            "total_warm":       d["total_warm"],
            "total_chains":     d["total_chains"],
            "subvariants":      sorted(d["subvariants"]),
            "max_distance_nm":  d["max_distance_nm"],
        })
    out.sort(key=lambda x: -x["total_composite"])
    return out[:top_n]


def get_route_map_data(top_n: int = 80, region: str | None = None, family: str | None = None) -> dict:
    """
    Top N routes (origin → destination) over the full history, with lat/lon
    coordinates resolved for each endpoint. Returns:
        airports — list of {icao, lat, lon, name, ops_count}
        routes   — list of {o, d, count} where o/d are ICAOs
    Useful for plotting a great-circle arc map.

    Optional ``region`` filter (``'NA'`` | ``'EU_UK'`` | ``'OTHER'``) narrows
    the underlying dataset to a single sales-region bucket. Optional ``family``
    filter narrows to A320-family or Boeing 737-family ICAO type codes.
    """
    from airports import icao_coords, _airports as _ap_list   # noqa: F401
    region_sql = " AND region = ?" if region else ""
    region_args: tuple = (region,) if region else ()
    family_sql, family_args = family_where_clause(family)
    with _connect() as conn:
        route_rows = conn.execute(
            f"""
            SELECT UPPER(origin_icao) AS o, UPPER(dest_icao) AS d, COUNT(*) AS n
            FROM v_sightings_dedup
            WHERE origin_icao IS NOT NULL AND origin_icao != ''
              AND dest_icao   IS NOT NULL AND dest_icao   != ''
              AND (UPPER(ac_type) != 'C25C' AND UPPER(IFNULL(ac_subvariant,'')) != 'CJ4')
              {region_sql}{family_sql}
            GROUP BY o, d
            ORDER BY n DESC
            LIMIT ?
            """,
            (*region_args, *family_args, top_n),
        ).fetchall()
    routes: list[dict] = []
    icaos: set[str] = set()
    for r in route_rows:
        o, d, n = r["o"], r["d"], r["n"]
        routes.append({"o": o, "d": d, "count": n})
        icaos.add(o)
        icaos.add(d)

    # Resolve coordinates for every airport referenced
    airports_out: list[dict] = []
    for code in sorted(icaos):
        pos = icao_coords(code)
        if not pos:
            continue
        airports_out.append({"icao": code, "lat": pos[0], "lon": pos[1]})

    # Filter routes whose endpoints both have coords (otherwise we can't draw)
    valid = {a["icao"] for a in airports_out}
    routes = [r for r in routes if r["o"] in valid and r["d"] in valid]

    return {"airports": airports_out, "routes": routes}


def get_prospects(days: int = 30, region: str | None = None) -> list[dict]:
    """
    Score every tail seen in the last `days` days by composite ATLAS benefit.
    Composite = range score + chain bonus + high-DA airport bonus.
    Returns all tails with any signal, sorted by composite score.

    Optional ``region`` filter (``'NA'`` | ``'EU_UK'`` | ``'OTHER'``) narrows
    the underlying dataset to a single sales-region bucket.
    """
    from atlas_config import ATLAS, WEIGHT_HOT, WEIGHT_WARM, trip_tier, atlas_cfg
    from airports import airport_elevation_ft, density_altitude, isa_temp_c
    from wat_lookup import wat_analysis

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    params: list = [cutoff]
    region_sql = ""
    if region in ("NA", "EU_UK", "OTHER"):
        region_sql = " AND region = ? "
        params.append(region)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT tail_number, ac_type, ac_subvariant, operator,
                   origin_icao, dest_icao, distance_nm,
                   arrived_utc, tracking_url, departure_oat_c
            FROM v_sightings_dedup
            WHERE arrived_utc >= ?
              AND tail_number IS NOT NULL AND tail_number != ''
              {region_sql}
            ORDER BY tail_number, arrived_utc
            """,
            tuple(params),
        ).fetchall()

    # Pre-index fuel-stop chains per tail (region-scoped to match).
    chains = get_fuel_stop_chains(days=days, region=region)
    chain_by_tail: dict[str, int] = {}
    for c in chains:
        chain_by_tail[c["tail_number"]] = chain_by_tail.get(c["tail_number"], 0) + 1

    # Group by tail
    tails: dict[str, dict] = {}
    for row in rows:
        tail = row["tail_number"]
        ac   = (row["ac_type"] or "").upper()
        sv   = (row["ac_subvariant"] or "").upper()
        if tail not in tails:
            cfg = atlas_cfg(ac, sv)
            tails[tail] = {
                "tail_number":   tail,
                "ac_type":       ac,
                "ac_subvariant": sv,
                "label":         cfg.get("label", ac or "—"),
                "engine":        cfg.get("engine", ""),
                "fadec":         cfg.get("fadec"),
                "baseline_nm":   cfg.get("baseline_nm"),
                "atlas_gain_nm": cfg.get("atlas_gain_nm"),
                "operator":      None,
                "trips":         [],
                "last_seen":     "",
                "last_tracking_url": "",
                "da_departures": [],
            }
        if row["operator"]:
            tails[tail]["operator"] = row["operator"]
        if row["distance_nm"]:
            tails[tail]["trips"].append({
                "distance_nm": row["distance_nm"],
                "arrived_utc": row["arrived_utc"] or "",
                "origin_icao": row["origin_icao"] or "",
                "dest_icao":   row["dest_icao"] or "",
            })
        # Track high-DA departures
        if row["origin_icao"]:
            elev = airport_elevation_ft(row["origin_icao"])
            if elev and elev >= 2000:
                oat = row["departure_oat_c"]
                da = density_altitude(elev, oat) if oat is not None else elev
                if da >= 5000:
                    tails[tail]["da_departures"].append({
                        "icao": row["origin_icao"],
                        "da":   da,
                        "oat_c": oat,
                        "elev":  elev,
                    })
        arrived = row["arrived_utc"] or ""
        if arrived > tails[tail]["last_seen"]:
            tails[tail]["last_seen"] = arrived
            tails[tail]["last_tracking_url"] = row["tracking_url"] or ""

    prospects = []
    for data in tails.values():
        ac = data["ac_type"]
        sv = data["ac_subvariant"]

        # Range score — use sub-variant baseline when available
        hot  = sum(1 for t in data["trips"] if trip_tier(t["distance_nm"], ac, sv) == "hot")
        warm = sum(1 for t in data["trips"] if trip_tier(t["distance_nm"], ac, sv) == "warm")
        range_score = hot * WEIGHT_HOT + warm * WEIGHT_WARM

        # Chain bonus
        n_chains = chain_by_tail.get(data["tail_number"], 0)
        chain_score = n_chains * 5

        # High-DA airport bonus (unique airports)
        da_deps = data["da_departures"]
        unique_da_icaos = {d["icao"] for d in da_deps}
        da_score = len(unique_da_icaos) * 2

        composite = range_score + chain_score + da_score
        if composite == 0:
            continue

        # Signals list for badge display
        signals = []
        if hot:   signals.append("HOT RANGE")
        if warm:  signals.append("WARM RANGE")
        if n_chains: signals.append(f"{n_chains} FUEL STOP{'S' if n_chains>1 else ''}")
        if unique_da_icaos: signals.append(f"{len(unique_da_icaos)} HIGH-DA ARPT")

        # WAT / OEI analysis at worst DA departure — type-specific
        wat = None
        oei = None
        worst_da_icao = None
        if da_deps:
            worst = max(da_deps, key=lambda d: d["da"])
            worst_da_icao = worst["icao"]
            if ac == "C525":
                if worst["oat_c"] is not None:
                    try:
                        wat = wat_analysis(worst["elev"], worst["oat_c"])
                        if wat["atlas_gain_lb"] > 0:
                            signals.append(f"+{wat['atlas_gain_lb']:,} LB WAT")
                        elif wat.get("temp_advantage_c", 0) > 0:
                            signals.append(f"+{wat['temp_advantage_c']}°C TEMP ENV")
                    except Exception:
                        pass
            elif ac in ("C25A", "C25B", "C25M"):
                try:
                    from wat_lookup import oei_gradient_analysis
                    oei = oei_gradient_analysis(ac, worst["elev"], worst["oat_c"])
                    signals.append(f"+{oei['gradient_improvement_pct']:.0f}% OEI GRADIENT")
                except Exception:
                    pass

        best_trip = max(data["trips"], key=lambda t: t["distance_nm"]) if data["trips"] else None
        atlas_nm  = (data["baseline_nm"] or 0) + (data["atlas_gain_nm"] or 0)

        prospects.append({
            "tail_number":       data["tail_number"],
            "ac_type":           ac,
            "ac_subvariant":     sv,
            "label":             data["label"],
            "engine":            data.get("engine", ""),
            "fadec":             data.get("fadec"),
            "operator":          data["operator"] or "—",
            "composite_score":   composite,
            "range_score":       range_score,
            "chain_score":       chain_score,
            "da_score":          da_score,
            "trips_hot":         hot,
            "trips_warm":        warm,
            "trips_total":       len(data["trips"]),
            "n_chains":          n_chains,
            "high_da_airports":  sorted(unique_da_icaos),
            "high_da_count":     len(unique_da_icaos),
            "wat_gain_lb":       wat["atlas_gain_lb"] if wat else None,
            "wat_deficit_lb":    wat["deficit_lb"] if wat else None,
            "wat_temp_adv_c":    wat["temp_advantage_c"] if wat else None,
            "wat_max_oat_fw":    wat["max_oat_flatwing"] if wat else None,
            "wat_max_oat_tam":   wat["max_oat_tamarack"] if wat else None,
            "wat_benefit_payload": wat["benefit_payload"] if wat else None,
            "wat_benefit_temp":  wat["benefit_temp"] if wat else None,
            "wat_worst_icao":    worst_da_icao,
            "oei_gradient_pct":  oei["gradient_improvement_pct"] if oei else None,
            "oei_severity":      oei["severity"] if oei else None,
            "oei_benefit_str":   oei["benefit_str"] if oei else None,
            "signals":           signals,
            "max_distance_nm":   round(best_trip["distance_nm"]) if best_trip else 0,
            "best_origin":       best_trip["origin_icao"] if best_trip else "—",
            "best_dest":         best_trip["dest_icao"] if best_trip else "—",
            "baseline_nm":       data["baseline_nm"],
            "atlas_nm":          atlas_nm or None,
            "last_seen":         data["last_seen"][:16].replace("T", " "),
            "last_tracking_url": data["last_tracking_url"],
        })

    prospects.sort(key=lambda p: (-p["composite_score"], -p["max_distance_nm"]))
    return prospects


def get_operator_fleets(prospects: list[dict]) -> list[dict]:
    """
    Group a prospects list by operator name.
    Returns operators sorted by total composite score descending.
    """
    from collections import defaultdict
    ops: dict[str, dict] = defaultdict(lambda: {
        "operator": "", "tails": [], "total_composite": 0,
        "total_hot": 0, "total_chains": 0, "total_da": 0,
        "ac_types": set(), "signals": set(),
    })
    for p in prospects:
        op = p["operator"] or "—"
        d = ops[op]
        d["operator"] = op
        d["tails"].append(p)
        d["total_composite"] += p["composite_score"]
        d["total_hot"]       += p["trips_hot"]
        d["total_chains"]    += p["n_chains"]
        d["total_da"]        += p["high_da_count"]
        d["ac_types"].add(p["label"])
        for s in p["signals"]:
            d["signals"].add(s)

    result = []
    for op, d in ops.items():
        if op == "—" or len(d["tails"]) < 2:
            continue   # only show operators with 2+ tracked tails
        result.append({
            "operator":        op,
            "tail_count":      len(d["tails"]),
            "total_composite": d["total_composite"],
            "total_hot":       d["total_hot"],
            "total_chains":    d["total_chains"],
            "high_da_count":   d["total_da"],
            "ac_types":        ", ".join(sorted(d["ac_types"])),
            "tails":           [p["tail_number"] for p in d["tails"]],
            "top_tail":        d["tails"][0]["tail_number"],
        })
    result.sort(key=lambda x: -x["total_composite"])
    return result


def get_tail_detail(tail: str, days: int = 180) -> dict | None:
    """
    Full pre-call intelligence dossier for a single tail number.
    """
    from atlas_config import ATLAS, atlas_cfg, trip_tier
    from airports import airport_elevation_ft, density_altitude, isa_temp_c
    from wat_lookup import wat_analysis, MTOW

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source, flight_id, tail_number, ac_type, ac_subvariant, operator,
                   origin_icao, origin_name, dest_icao, dest_name,
                   departed_utc, arrived_utc, distance_nm,
                   tracking_url, departure_oat_c, scope_tier
            FROM v_sightings_dedup_all
            WHERE tail_number = ? AND arrived_utc >= ?
            ORDER BY arrived_utc DESC
            """,
            (tail, cutoff),
        ).fetchall()

    if not rows:
        return None

    flights = [dict(r) for r in rows]
    ac = (flights[0]["ac_type"] or "C525").upper()
    sv = (flights[0].get("ac_subvariant") or "").upper()
    scope_tier = flights[0].get("scope_tier") or "atlas"
    cfg = atlas_cfg(ac, sv)
    operator = next((f["operator"] for f in flights if f["operator"]), "—")
    baseline = cfg.get("baseline_nm")
    atlas_nm = (baseline or 0) + (cfg.get("atlas_gain_nm") or 0)

    # Classify trips
    for f in flights:
        f["tracking_url"] = resolve_tracking_url(
            f.get("source"),
            f.get("flight_id"),
            f.get("tracking_url"),
            f.get("tail_number"),
        )
        f["tier"] = trip_tier(f["distance_nm"], ac, sv) if f["distance_nm"] else None
        # DA analysis per departure
        elev = airport_elevation_ft(f["origin_icao"]) if f["origin_icao"] else None
        oat  = f["departure_oat_c"]
        if elev:
            da = density_altitude(elev, oat) if oat is not None else elev
            f["da"] = round(da)
            f["elev"] = round(elev)
        else:
            f["da"] = None
            f["elev"] = None

    # Per-airport WAT analysis for high-DA airports this tail uses
    apt_oat: dict[str, list[float]] = {}
    apt_elev: dict[str, float] = {}
    for f in flights:
        if f["origin_icao"] and f["departure_oat_c"] is not None:
            icao = f["origin_icao"].upper()
            apt_oat.setdefault(icao, []).append(f["departure_oat_c"])
        if f["origin_icao"] and f.get("elev"):
            apt_elev[f["origin_icao"].upper()] = f["elev"]

    wat_airports = []
    for icao, oats in apt_oat.items():
        elev = apt_elev.get(icao, airport_elevation_ft(icao))
        if not elev or elev < 3000:
            continue
        max_oat = max(oats)
        avg_oat = sum(oats) / len(oats)
        da_max = density_altitude(elev, max_oat)
        if da_max < 4000:
            continue
        if ac == "C525":
            try:
                wat = wat_analysis(elev, max_oat)
            except Exception:
                wat = None
            oei = None
        elif ac in ("C25A", "C25B", "C25M"):
            wat = None
            try:
                from wat_lookup import oei_gradient_analysis
                oei = oei_gradient_analysis(ac, elev, max_oat)
            except Exception:
                oei = None
        else:
            wat = None
            oei = None
        wat_airports.append({
            "icao":              icao,
            "elevation_ft":      round(elev),
            "da_max":            round(da_max),
            "oat_max_c":         round(max_oat, 1),
            "oat_avg_c":         round(avg_oat, 1),
            "n_obs":             len(oats),
            # C525 WAT weight / temperature fields
            "wat_flatwing":      wat["flatwing_lb"]      if wat else None,
            "wat_tamarack":      wat["tamarack_lb"]      if wat else None,
            "wat_gain":          wat["atlas_gain_lb"]    if wat else None,
            "wat_deficit":       wat["deficit_lb"]       if wat else None,
            "wat_temp_adv_c":    wat["temp_advantage_c"] if wat else None,
            "wat_max_oat_fw":    wat["max_oat_flatwing"] if wat else None,
            "wat_max_oat_tam":   wat["max_oat_tamarack"] if wat else None,
            "benefit_payload":   wat["benefit_payload"]  if wat else None,
            "benefit_temp":      wat["benefit_temp"]     if wat else None,
            # C25A/C25B OEI gradient fields
            "oei_gradient_pct":  oei["gradient_improvement_pct"] if oei else None,
            "oei_severity":      oei["severity"]         if oei else None,
            "oei_benefit_str":   oei["benefit_str"]      if oei else None,
        })
    wat_airports.sort(key=lambda x: -x["da_max"])

    # Fuel-stop chains for this tail
    all_chains = get_fuel_stop_chains(days=days, scope_tier=scope_tier or "atlas")
    tail_chains = [c for c in all_chains if c["tail_number"] == tail]

    hot   = sum(1 for f in flights if f["tier"] == "hot")
    warm  = sum(1 for f in flights if f["tier"] == "warm")
    dists = [f["distance_nm"] for f in flights if f["distance_nm"]]

    return {
        "tail_number":   tail,
        "ac_type":       ac,
        "ac_subvariant": sv,
        "scope_tier":    scope_tier or "atlas",
        "label":         cfg.get("label", ac),
        "engine":        cfg.get("engine", ""),
        "fadec":         cfg.get("fadec"),
        "operator":      operator,
        "baseline_nm":   baseline,
        "atlas_nm":      atlas_nm or None,
        "total_flights": len(flights),
        "trips_hot":     hot,
        "trips_warm":    warm,
        "avg_dist_nm":   round(sum(dists) / len(dists)) if dists else 0,
        "max_dist_nm":   round(max(dists)) if dists else 0,
        "n_chains":      len(tail_chains),
        "flights":       flights,
        "chains":        tail_chains,
        "wat_airports":  wat_airports,
        "days":          days,
    }



def get_airline_insights(region: str = "NA", limit: int = 15, family: str | None = None) -> dict:
    """Airline-safe insight rollup for A320/737 sightings. Optional A320/B737 family filter."""
    region = region if region in ("NA", "EU_UK", "OTHER") else "NA"
    family = normalize_family(family)
    family_sql, family_args = family_where_clause(family)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM v_sightings_dedup WHERE region=?{family_sql}", (region, *family_args)).fetchone()[0]
        recent_24h = conn.execute(f"SELECT COUNT(*) FROM v_sightings_dedup WHERE region=?{family_sql} AND arrived_utc >= datetime('now','-24 hours')", (region, *family_args)).fetchone()[0]
        active_tails = conn.execute(f"SELECT COUNT(DISTINCT tail_number) FROM v_sightings_dedup WHERE region=?{family_sql} AND tail_number IS NOT NULL AND tail_number!=''", (region, *family_args)).fetchone()[0]
        avg_distance = conn.execute(f"SELECT AVG(distance_nm) FROM v_sightings_dedup WHERE region=?{family_sql} AND distance_nm IS NOT NULL AND distance_nm > 0", (region, *family_args)).fetchone()[0]
        top_types = [dict(r) for r in conn.execute(f"""
            SELECT COALESCE(NULLIF(ac_subvariant,''), NULLIF(ac_type,''), 'Unknown') AS label, COUNT(*) AS n
            FROM v_sightings_dedup WHERE region=?{family_sql}
            GROUP BY label ORDER BY n DESC LIMIT ?
        """, (region, *family_args, limit)).fetchall()]
        top_operators = [dict(r) for r in conn.execute(f"""
            SELECT COALESCE(NULLIF(operator,''), 'Unknown') AS label, COUNT(*) AS n
            FROM v_sightings_dedup WHERE region=?{family_sql}
            GROUP BY label ORDER BY n DESC LIMIT ?
        """, (region, *family_args, limit)).fetchall()]
        top_airports = [dict(r) for r in conn.execute(f"""
            SELECT COALESCE(NULLIF(dest_icao,''), 'Unknown') AS label, COUNT(*) AS n
            FROM v_sightings_dedup WHERE region=?{family_sql}
            GROUP BY label ORDER BY n DESC LIMIT ?
        """, (region, *family_args, limit)).fetchall()]
        top_routes = [dict(r) for r in conn.execute(f"""
            SELECT COALESCE(NULLIF(origin_icao,''), '????') || ' → ' || COALESCE(NULLIF(dest_icao,''), '????') AS label,
                   COUNT(*) AS n, ROUND(AVG(distance_nm)) AS avg_nm
            FROM v_sightings_dedup WHERE region=?{family_sql} AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL
            GROUP BY label ORDER BY n DESC LIMIT ?
        """, (region, *family_args, limit)).fetchall()]
        longest = [dict(r) for r in conn.execute(f"""
            SELECT tail_number, COALESCE(NULLIF(ac_subvariant,''), NULLIF(ac_type,''), 'Unknown') AS type,
                   origin_icao, dest_icao, ROUND(distance_nm) AS distance_nm, arrived_utc, COALESCE(NULLIF(operator,''), 'Unknown') AS operator
            FROM v_sightings_dedup
            WHERE region=?{family_sql} AND distance_nm IS NOT NULL AND distance_nm > 0
            ORDER BY distance_nm DESC LIMIT ?
        """, (region, *family_args, limit)).fetchall()]
        metric_rows = [dict(r) for r in conn.execute(f"""
            SELECT distance_nm, departed_utc, arrived_utc,
                   sustained_top_alt_ft, top_altitude_ft, initial_cruise_alt_ft,
                   COALESCE(NULLIF(ac_subvariant,''), NULLIF(ac_type,''), 'Unknown') AS type
            FROM v_sightings_dedup
            WHERE region=?{family_sql} AND distance_nm IS NOT NULL AND distance_nm > 0
              AND departed_utc IS NOT NULL AND arrived_utc IS NOT NULL
        """, (region, *family_args)).fetchall()]
    distances = [float(r["distance_nm"]) for r in metric_rows if r.get("distance_nm")]
    altitudes = []
    for r in metric_rows:
        alt = r.get("sustained_top_alt_ft") or r.get("top_altitude_ft") or r.get("initial_cruise_alt_ft")
        if alt:
            altitudes.append(int(alt))
    bin_size = 250 if region == "EU_UK" else 500
    max_bin = max(1, int((max(distances) if distances else bin_size) // bin_size) + 1)
    dist_labels = [f"{i*bin_size}-{(i+1)*bin_size}" for i in range(max_bin)]
    dist_hist = [0] * max_bin
    scatter = []
    for r in metric_rows:
        d = float(r.get("distance_nm") or 0)
        if d <= 0:
            continue
        idx = min(int(d // bin_size), max_bin - 1)
        dist_hist[idx] += 1
        dur = _duration_h(r.get("departed_utc"), r.get("arrived_utc"))
        if dur and 0.1 <= dur <= 20:
            scatter.append({"x": round(d), "y": round(d / dur), "type": r.get("type") or "Unknown"})
    fl_bins = list(range(200, 451, 10))
    fl_hist = [0] * len(fl_bins)
    for alt in altitudes:
        fl = round(alt / 100)
        if 200 <= fl <= 450:
            idx = min(len(fl_bins) - 1, max(0, (fl - 200) // 10))
            fl_hist[idx] += 1
    fl_labels = [f"FL{v}" for v in fl_bins]
    avg_fl = round(sum(altitudes) / len(altitudes) / 100) if altitudes else None
    med_fl = None
    if altitudes:
        vals = sorted(round(a / 100) for a in altitudes)
        med_fl = vals[len(vals)//2]
    return {
        "region": region, "family": family, "total": total, "recent_24h": recent_24h, "active_tails": active_tails,
        "avg_distance": round(avg_distance) if avg_distance else None,
        "top_types": top_types, "top_operators": top_operators, "top_airports": top_airports,
        "top_routes": top_routes, "longest": longest,
        "dist_hist_labels": dist_labels, "dist_hist": dist_hist, "block_scatter": scatter[:500],
        "fl_hist_labels": fl_labels, "fl_hist": fl_hist, "avg_fl": avg_fl, "median_fl": med_fl,
    }

def get_mustang_insights() -> dict:
    """
    Mission profile statistics for C510 (Mustang) adjacent-tier flights.
    Similar shape to get_insights() but scoped to scope_tier='up'.

    Returns {} when there are fewer than 5 flights with distance data (too
    sparse to draw meaningful conclusions). The page shows a "building up"
    message in that case.
    """
    from collections import Counter

    _HARD_MAX_NM = 1400  # C510 physically can't exceed this on a single leg

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT tail_number, operator, origin_icao, dest_icao,
                   distance_nm, departed_utc, arrived_utc
            FROM sightings
            WHERE scope_tier = 'up'
              AND distance_nm IS NOT NULL AND distance_nm > 0
              AND distance_nm <= ?
              AND dest_icao IS NOT NULL AND dest_icao != ''
            ORDER BY arrived_utc DESC
            """,
            (_HARD_MAX_NM,),
        ).fetchall()

        all_rows = conn.execute(
            """
            SELECT tail_number, operator, dest_icao, arrived_utc
            FROM sightings
            WHERE scope_tier = 'up'
            ORDER BY arrived_utc DESC
            """,
        ).fetchall()

    if len(rows) < 5:
        # Not enough distance-tagged flights to compute anything meaningful
        total = len(all_rows)
        unique_tails = len({r["tail_number"] for r in all_rows if r["tail_number"]})
        return {
            "sparse": True,
            "total_flights": total,
            "unique_tails": unique_tails,
        }

    distances  = [r["distance_nm"] for r in rows]
    dur_by_row = [_duration_h(r["departed_utc"], r["arrived_utc"]) for r in rows]
    durations  = [h for h in dur_by_row if h]

    fleet_trip_kts = [
        r["distance_nm"] / h
        for r, h in zip(rows, dur_by_row)
        if r["distance_nm"] and h and h > 0
    ]
    avg_ground_kts = round(sum(fleet_trip_kts) / len(fleet_trip_kts)) if fleet_trip_kts else None

    # C510 baseline (MCT) from atlas_config — used for "range signal" tier
    from atlas_config import ATLAS as _ATLAS
    baseline_nm = (_ATLAS.get("C510") or {}).get("baseline_nm") or 1000
    hot   = sum(1 for d in distances if d > baseline_nm)
    warm  = sum(1 for d in distances if 0.8 * baseline_nm < d <= baseline_nm)
    other = len(distances) - hot - warm

    # Distance histogram (100 nm buckets up to 1400 nm)
    b = list(range(0, 1501, 100))
    dist_hist_full = [sum(1 for d in distances if b[i] <= d < b[i+1]) for i in range(len(b)-1)]
    last_nz = max((i for i, v in enumerate(dist_hist_full) if v > 0), default=-1)
    keep = min(len(dist_hist_full), last_nz + 2)
    dist_hist   = dist_hist_full[:keep] if keep > 0 else dist_hist_full[:1]
    hist_labels = [f"{b[i]}–{b[i]+100}" for i in range(keep)] if keep > 0 else ["0–100"]

    # Duration histogram (0.25h buckets, 0–6h)
    db2 = [i * 0.25 for i in range(25)]
    dur_hist   = [sum(1 for h in durations if db2[i] <= h < db2[i+1]) for i in range(len(db2)-1)]
    dur_labels = [f"{db2[i]:g}–{db2[i+1]:g}h" for i in range(len(db2)-1)]

    # Arrival-hour distribution (destination local time)
    import airports as _ap
    try:
        from zoneinfo import ZoneInfo
    except Exception:  # noqa: BLE001
        ZoneInfo = None
    _tz_cache: dict[str, object] = {}
    hour_dist = [0] * 24
    for r in rows:
        au = (r["arrived_utc"] or "").replace("Z", "+00:00")
        if not au:
            continue
        try:
            dt_utc = datetime.fromisoformat(au)
        except ValueError:
            continue
        dest = (r["dest_icao"] or "").upper()
        local_hour = None
        if dest and ZoneInfo is not None:
            tz_obj = _tz_cache.get(dest)
            if tz_obj is None and dest not in _tz_cache:
                tz_name = _ap._icao_timezone(dest)
                tz_obj  = ZoneInfo(tz_name) if tz_name else None
                _tz_cache[dest] = tz_obj
            if tz_obj is not None:
                local_hour = dt_utc.astimezone(tz_obj).hour
        if local_hour is None:
            local_hour = dt_utc.hour
        hour_dist[local_hour] += 1

    # Top airports and routes
    orig_c  = Counter(r["origin_icao"] for r in rows if r["origin_icao"])
    dest_c  = Counter(r["dest_icao"]   for r in rows if r["dest_icao"])
    route_c = Counter(
        f"{r['origin_icao']}→{r['dest_icao']}"
        for r in rows if r["origin_icao"] and r["dest_icao"]
    )

    # Operator leaderboard (from distance-tagged rows)
    chains = get_fuel_stop_chains(days=180, scope_tier="up")
    chain_tails = {(c["tail_number"] or "").upper() for c in chains}
    op_flights: Counter = Counter()
    op_tails: dict[str, set] = {}
    op_dist: dict[str, list] = {}
    for r in rows:
        op = (r["operator"] or "").strip() or "—"
        tail = (r["tail_number"] or "").upper()
        op_flights[op] += 1
        op_tails.setdefault(op, set()).add(tail)
        op_dist.setdefault(op, []).append(r["distance_nm"])
    op_chains: Counter = Counter()
    for c in chains:
        op = (c.get("operator") or "").strip() or "—"
        op_chains[op] += 1
    operator_board = sorted(
        [
            {
                "operator":   op,
                "flights":    op_flights[op],
                "tails":      len(op_tails.get(op, set())),
                "chain_hits": op_chains.get(op, 0),
                "avg_dist":   round(sum(op_dist[op]) / len(op_dist[op])) if op_dist.get(op) else 0,
                "max_dist":   round(max(op_dist[op])) if op_dist.get(op) else 0,
            }
            for op in op_flights
        ],
        key=lambda o: (-o["chain_hits"], -o["flights"], -o["tails"]),
    )[:25]

    unique_tails = len({r["tail_number"] for r in rows if r["tail_number"]})

    return {
        "sparse": False,
        "total_flights":    len(distances),
        "unique_tails":     unique_tails,
        "unique_operators": len(op_flights),
        "chain_hits":       len(chains),
        "avg_distance":     round(sum(distances) / len(distances)) if distances else 0,
        "max_distance":     round(max(distances)) if distances else 0,
        "avg_duration_h":   round(sum(durations) / len(durations), 1) if durations else None,
        "avg_ground_kts":   avg_ground_kts,
        "baseline_nm":      baseline_nm,
        "hot":              hot,
        "warm":             warm,
        "other":            other,
        "dist_hist_labels": hist_labels,
        "dist_hist_data":   dist_hist,
        "dist_hist_pct":    _as_pct(dist_hist),
        "dur_hist_labels":  dur_labels,
        "dur_hist_data":    dur_hist,
        "dur_hist_pct":     _as_pct(dur_hist),
        "hour_dist":        hour_dist,
        "hour_dist_pct":    _as_pct(hour_dist),
        "top_origins":  [{"icao": k, "count": v} for k, v in orig_c.most_common(10)],
        "top_dests":    [{"icao": k, "count": v} for k, v in dest_c.most_common(10)],
        "top_routes":   [{"route": k, "count": v} for k, v in route_c.most_common(10)],
        "operator_board": operator_board,
    }


def get_mustang_activity(days: int = 30, limit: int = 250) -> dict:
    """
    Adjacent-tier activity report for the /mustangs page.

    Returns:
        {
          "days": 30,
          "total_flights":  int,
          "unique_tails":   int,
          "unique_operators": int,
          "flights": [ {tail, operator, origin, dest, distance_nm, arrived_utc,
                        arrived_local, chain_hit, tracking_url}, ... ],
          "by_operator": [ {operator, tails, flights, chain_hits}, ... ],
          "chains": [ ...same shape as get_fuel_stop_chains, ... ],
        }
    Sourced from raw sightings (bypasses v_sightings_dedup's scope='atlas' gate).
    """
    import airports as _ap
    from collections import defaultdict

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source, flight_id, tail_number, ac_type, ac_subvariant,
                   operator, origin_icao, dest_icao, distance_nm,
                   departed_utc, arrived_utc, tracking_url
            FROM sightings
            WHERE scope_tier = 'up'
              AND arrived_utc >= ?
            ORDER BY arrived_utc DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    chains = get_fuel_stop_chains(days=days, scope_tier="up")
    chain_arrivals = {
        ((c["tail_number"] or "").upper(), c.get("arrived_utc") or "")
        for c in chains
    }

    flights: list[dict] = []
    op_tails: dict[str, set] = defaultdict(set)
    op_flights: dict[str, int] = defaultdict(int)
    op_chains: dict[str, int] = defaultdict(int)
    for r in rows:
        tail_up = (r["tail_number"] or "").upper()
        dest    = (r["dest_icao"] or "").upper()
        arrived_utc_str = r["arrived_utc"] or ""
        arrived_local   = _ap.local_time_at_icao(arrived_utc_str, dest) if arrived_utc_str else ""
        chain_hit = (tail_up, arrived_utc_str[:10]) in chain_arrivals
        operator  = (r["operator"] or "").strip() or "—"
        flights.append({
            "tail":          tail_up or "—",
            "operator":      operator,
            "origin":        (r["origin_icao"] or "").upper() or "?",
            "dest":          dest or "?",
            "distance_nm":   int(r["distance_nm"]) if r["distance_nm"] else None,
            "arrived_utc":   arrived_utc_str,
            "arrived_local": arrived_local,
            "chain_hit":     chain_hit,
            "tracking_url":  resolve_tracking_url(
                r["source"], r["flight_id"], r["tracking_url"], r["tail_number"]
            ),
        })
        if tail_up:
            op_tails[operator].add(tail_up)
        op_flights[operator] += 1
        if chain_hit:
            op_chains[operator] += 1

    by_operator = sorted(
        [
            {"operator": op, "tails": len(op_tails[op]),
             "flights": op_flights[op], "chain_hits": op_chains[op]}
            for op in op_flights
        ],
        key=lambda o: (-o["chain_hits"], -o["flights"], -o["tails"]),
    )

    return {
        "days":              days,
        "total_flights":     len(flights),
        "unique_tails":      len({f["tail"] for f in flights if f["tail"] != "—"}),
        "unique_operators":  len(op_flights),
        "flights":           flights,
        "by_operator":       by_operator,
        "chains":            chains,
    }


def get_weekly_hot(n: int = 5) -> list[dict]:
    """Top N composite-scored tails with activity in the last 7 days."""
    all_p = get_prospects(days=30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with _connect() as conn:
        recent_tails = {r[0] for r in conn.execute(
            "SELECT DISTINCT tail_number FROM sightings WHERE arrived_utc >= ?",
            (cutoff,)
        ).fetchall()}
    return [p for p in all_p if p["tail_number"] in recent_tails][:n]


def get_airline_opportunity_feed(
    region: str | None = "NA",
    family: str | None = None,
    limit: int = 10,
    days: int = 14,
) -> list[dict]:
    """
    Lightweight sales-intelligence feed for A320/737 sightings.

    Uses observed mission facts only — no simulator claims yet. The goal is to
    surface flights/operators worth asking about: long stages, high altitude,
    lower-altitude short/medium stages, and repeat activity.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params: list = [cutoff]
    region_sql = ""
    if region in ("NA", "EU_UK", "OTHER"):
        region_sql = " AND region = ?"
        params.append(region)
    family_sql, family_args = family_where_clause(family)
    params.extend(family_args)

    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""
            SELECT tail_number, ac_type, ac_subvariant, operator,
                   origin_icao, dest_icao, distance_nm, arrived_utc, tracking_url,
                   sustained_top_alt_ft, top_altitude_ft, initial_cruise_alt_ft
            FROM v_sightings_dedup
            WHERE arrived_utc >= ?{region_sql}{family_sql}
            ORDER BY arrived_utc DESC
            LIMIT 600
            """,
            tuple(params),
        ).fetchall()]

    op_counts: dict[str, int] = {}
    route_counts: dict[tuple[str, str], int] = {}
    for r in rows:
        op = (r.get("operator") or "Unknown").strip() or "Unknown"
        op_counts[op] = op_counts.get(op, 0) + 1
        o, d = r.get("origin_icao") or "", r.get("dest_icao") or ""
        if o and d:
            key = (o, d)
            route_counts[key] = route_counts.get(key, 0) + 1

    out: list[dict] = []
    for r in rows:
        dist = float(r.get("distance_nm") or 0)
        alt = r.get("sustained_top_alt_ft") or r.get("top_altitude_ft") or r.get("initial_cruise_alt_ft")
        alt = int(alt) if alt else None
        op = (r.get("operator") or "Unknown").strip() or "Unknown"
        route = (r.get("origin_icao") or "—", r.get("dest_icao") or "—")
        tags: list[str] = []
        score = 0
        why: list[str] = []

        if dist >= 1500:
            tags.append("LONG STAGE")
            score += 35
            why.append(f"{round(dist):,} nm stage length")
        elif dist >= 900:
            tags.append("MEDIUM/LONG")
            score += 22
            why.append(f"{round(dist):,} nm stage length")
        elif 250 <= dist <= 700 and alt and alt <= 31000:
            tags.append("LOWER ALT")
            score += 14
            why.append(f"{round(dist):,} nm at about FL{round(alt/100)}")

        if alt and alt >= 39000:
            tags.append("HIGH FL")
            score += 18
            why.append(f"observed near FL{round(alt/100)}")
        elif alt and alt <= 30000 and dist >= 500:
            tags.append("CONSTRAINED FL")
            score += 12
            why.append(f"longer leg capped near FL{round(alt/100)}")

        if op_counts.get(op, 0) >= 4:
            tags.append("REPEAT OPERATOR")
            score += min(20, op_counts[op] * 2)
            why.append(f"{op_counts[op]} recent sightings for operator")

        route_n = route_counts.get((route[0], route[1]), 0)
        if route_n >= 2:
            tags.append("REPEAT ROUTE")
            score += min(12, route_n * 3)

        if not tags:
            continue
        out.append({
            "score": score,
            "tags": tags[:4],
            "why": "; ".join(why[:3]) or "observed pattern worth reviewing",
            "tail_number": r.get("tail_number") or "—",
            "type": r.get("ac_subvariant") or r.get("ac_type") or "—",
            "operator": op,
            "origin_icao": route[0],
            "dest_icao": route[1],
            "distance_nm": round(dist) if dist else None,
            "altitude_ft": alt,
            "arrived_utc": r.get("arrived_utc"),
            "tracking_url": r.get("tracking_url") or "",
        })

    out.sort(key=lambda x: (-x["score"], x.get("arrived_utc") or ""), reverse=False)
    return out[:limit]


def get_airline_mission_bins(region: str | None = "NA", family: str | None = "A320") -> list[dict]:
    """
    Observed mission bins for simulator coupling.

    These are not benefit estimates yet. They summarize actual sighting distance
    and altitude buckets so an offline Tamarack simulator batch can run a small
    representative matrix instead of trying to simulate every live row.
    """
    region_sql = " AND region = ?" if region in ("NA", "EU_UK", "OTHER") else ""
    region_args: tuple = (region,) if region_sql else ()
    family_sql, family_args = family_where_clause(family)
    rows_sql = f"""
        SELECT distance_nm,
               COALESCE(sustained_top_alt_ft, top_altitude_ft, initial_cruise_alt_ft) AS alt_ft,
               origin_icao, dest_icao, tail_number, operator, arrived_utc
        FROM v_sightings_dedup
        WHERE distance_nm IS NOT NULL AND distance_nm > 0{region_sql}{family_sql}
    """
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(rows_sql, (*region_args, *family_args)).fetchall()]

    dist_bins = [
        (0, 250, "0–250 nm", 125),
        (250, 500, "250–500 nm", 375),
        (500, 750, "500–750 nm", 625),
        (750, 1000, "750–1000 nm", 875),
        (1000, 1500, "1000–1500 nm", 1250),
        (1500, 100000, "1500+ nm", 1750),
    ]
    alt_bins = [
        (0, 25000, "< FL250", 23000),
        (25000, 31000, "FL250–310", 29000),
        (31000, 35000, "FL310–350", 33000),
        (35000, 39000, "FL350–390", 37000),
        (39000, 100000, "FL390+", 39000),
    ]
    buckets: dict[tuple[str, str], dict] = {}
    for r in rows:
        dist = float(r.get("distance_nm") or 0)
        alt = int(r.get("alt_ft") or 0)
        d_bin = next((b for b in dist_bins if b[0] <= dist < b[1]), None)
        a_bin = next((b for b in alt_bins if b[0] <= alt < b[1]), None) if alt else None
        if not d_bin or not a_bin:
            continue
        key = (d_bin[2], a_bin[2])
        b = buckets.setdefault(key, {
            "distance_bin": d_bin[2],
            "altitude_bin": a_bin[2],
            "representative_distance_nm": d_bin[3],
            "representative_altitude_ft": a_bin[3],
            "count": 0,
            "sum_distance_nm": 0.0,
            "sum_altitude_ft": 0,
            "tails": set(),
            "representative_flight": None,
            "representative_error": 10**9,
        })
        b["count"] += 1
        b["sum_distance_nm"] += dist
        b["sum_altitude_ft"] += alt
        if r.get("tail_number"):
            b["tails"].add(str(r.get("tail_number")).upper())
        # Pick the real flight closest to the bin representative distance+altitude.
        # This gives the simulator an actual airport pair instead of a synthetic leg.
        err = abs(dist - d_bin[3]) + abs((alt - a_bin[3]) / 1000.0) * 25.0
        if r.get("origin_icao") and r.get("dest_icao") and err < b.get("representative_error", 10**9):
            b["representative_error"] = err
            b["representative_flight"] = {
                "origin_icao": r.get("origin_icao"),
                "dest_icao": r.get("dest_icao"),
                "tail_number": r.get("tail_number"),
                "operator": r.get("operator"),
                "distance_nm": round(dist),
                "altitude_ft": alt,
                "arrived_utc": r.get("arrived_utc"),
            }

    out = []
    for b in buckets.values():
        n = b["count"] or 1
        out.append({
            "distance_bin": b["distance_bin"],
            "altitude_bin": b["altitude_bin"],
            "representative_distance_nm": b["representative_distance_nm"],
            "representative_altitude_ft": b["representative_altitude_ft"],
            "count": b["count"],
            "unique_aircraft": len(b.get("tails") or []),
            "avg_distance_nm": round(b["sum_distance_nm"] / n),
            "avg_altitude_ft": round(b["sum_altitude_ft"] / n),
            "sim_status": "pending_a320_config",
            "representative_flight": b.get("representative_flight"),
        })
    out.sort(key=lambda x: (-x["count"], x["representative_distance_nm"], x["representative_altitude_ft"]))
    return out[:12]


def get_m2_yaw_damper_suspects(
    days:            int = 90,
    min_distance_nm: int = 300,
    max_alt_ft:      int = 28000,
    min_low_flights: int = 3,
) -> list[dict]:
    """
    Identify M2 (C25M) tails that consistently fly at or below FL280 on
    missions longer than `min_distance_nm`. Per the M2 AFM, an inoperative
    yaw damper imposes a max-altitude limitation of FL280 — so a tail
    habitually capped at that altitude on long legs is a strong signal of
    a deferred yaw-damper squawk (sales-conversation trigger).

    Returns one dict per qualifying tail:
        tail_number, operator, total_long_legs, low_alt_legs, pct_low,
        avg_top_alt_ft, last_seen, last_tracking_url, examples[]
    Sorted by pct_low DESC, low_alt_legs DESC.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT tail_number, operator, distance_nm, top_altitude_ft,
                   arrived_utc, tracking_url, origin_icao, dest_icao
            FROM v_sightings_dedup
            WHERE arrived_utc >= ?
              AND UPPER(COALESCE(ac_subvariant, '')) = 'M2'
              AND tail_number IS NOT NULL AND tail_number != ''
              AND distance_nm IS NOT NULL AND distance_nm > ?
              AND top_altitude_ft IS NOT NULL
            ORDER BY tail_number, arrived_utc DESC
            """,
            (cutoff, min_distance_nm),
        ).fetchall()

    per_tail: dict[str, dict] = {}
    for r in rows:
        t = r["tail_number"]
        d = per_tail.setdefault(t, {
            "tail_number":       t,
            "operator":          r["operator"] or "—",
            "total_long_legs":   0,
            "low_alt_legs":      0,
            "tops_low":          [],
            "last_seen":         "",
            "last_tracking_url": "",
            "examples":          [],
        })
        if r["operator"] and d["operator"] == "—":
            d["operator"] = r["operator"]
        d["total_long_legs"] += 1
        top_ft = r["top_altitude_ft"]
        if top_ft is not None and top_ft <= max_alt_ft:
            d["low_alt_legs"] += 1
            d["tops_low"].append(top_ft)
            if len(d["examples"]) < 3:
                d["examples"].append({
                    "arrived_utc":  r["arrived_utc"] or "",
                    "origin":       r["origin_icao"] or "",
                    "dest":         r["dest_icao"]   or "",
                    "distance_nm":  int(r["distance_nm"]),
                    "top_alt_ft":   int(top_ft),
                })
        if (r["arrived_utc"] or "") > d["last_seen"]:
            d["last_seen"]         = r["arrived_utc"] or ""
            d["last_tracking_url"] = r["tracking_url"] or ""

    out: list[dict] = []
    for d in per_tail.values():
        if d["low_alt_legs"] < min_low_flights:
            continue
        pct = round(100 * d["low_alt_legs"] / d["total_long_legs"]) if d["total_long_legs"] else 0
        avg_top = int(sum(d["tops_low"]) / len(d["tops_low"])) if d["tops_low"] else None
        out.append({
            "tail_number":        d["tail_number"],
            "operator":           d["operator"],
            "total_long_legs":    d["total_long_legs"],
            "low_alt_legs":       d["low_alt_legs"],
            "pct_low":            pct,
            "avg_top_alt_ft":     avg_top,
            "last_seen":          d["last_seen"],
            "last_tracking_url":  d["last_tracking_url"],
            "examples":           d["examples"],
        })
    out.sort(key=lambda x: (-x["pct_low"], -x["low_alt_legs"]))
    return out


def enrich_temperatures(limit: int = 50) -> int:
    """
    Fetch and store departure OAT (°C) for sightings that don't have it yet.
    Uses Open-Meteo (free, no API key). Returns count of rows updated.
    Call periodically from the polling loop or on insights page load.
    """
    from airports import icao_coords
    from weather import fetch_oat_c

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, origin_icao, departed_utc
            FROM sightings
            WHERE departure_oat_c IS NULL
              AND origin_icao IS NOT NULL AND origin_icao != ''
              AND departed_utc IS NOT NULL AND departed_utc != ''
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    updated = 0
    for row in rows:
        coords = icao_coords(row["origin_icao"])
        if not coords:
            continue
        try:
            dt = datetime.fromisoformat(row["departed_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        oat = fetch_oat_c(coords[0], coords[1], dt, icao=row["origin_icao"])
        if oat is not None:
            with _connect() as conn:
                conn.execute(
                    "UPDATE sightings SET departure_oat_c=? WHERE id=?",
                    (oat, row["id"]),
                )
                conn.commit()
            updated += 1
    return updated


def bulk_enrich_temperatures() -> int:
    """
    Enrich ALL sightings missing OAT, prioritising high-elevation airports.
    Runs in background at startup. Rate-limited to ~1 req/s to be polite to
    Open-Meteo. Returns total count updated.
    """
    import time
    from airports import icao_coords, airport_elevation_ft
    from weather import fetch_oat_c

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, origin_icao, departed_utc
            FROM sightings
            WHERE departure_oat_c IS NULL
              AND origin_icao IS NOT NULL AND origin_icao != ''
              AND departed_utc IS NOT NULL AND departed_utc != ''
            ORDER BY id DESC
            """
        ).fetchall()

    # Prioritise: high-elevation airports first (most useful for WAT analysis)
    def _priority(r):
        elev = airport_elevation_ft(r["origin_icao"]) or 0
        return -elev

    rows_sorted = sorted(rows, key=_priority)
    updated = 0
    for row in rows_sorted:
        coords = icao_coords(row["origin_icao"])
        if not coords:
            continue
        try:
            dt = datetime.fromisoformat(row["departed_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        oat = fetch_oat_c(coords[0], coords[1], dt, icao=row["origin_icao"])
        if oat is not None:
            with _connect() as conn:
                conn.execute(
                    "UPDATE sightings SET departure_oat_c=? WHERE id=?",
                    (oat, row["id"]),
                )
                conn.commit()
            updated += 1
        time.sleep(0.3)   # gentle to both IEM and Open-Meteo
    log.info("bulk_enrich_temperatures complete: %d updated", updated)
    return updated


# ── Distance validation (long-haul sanity check) ──────────────────────────
# Long CJ-family flights near the ATLAS-extended range ceiling can't be
# trusted at face value — sometimes the ICAO pair is wrong, sometimes there
# was an unrecorded fuel stop captured as one leg. For any flight above
# _DISTANCE_SUSPECT_NM we fetch the FA track and audit it. See
# track_fetcher.validate_distance_from_track for the actual check.
_DISTANCE_SUSPECT_NM = 1800   # near CJ3+ ATLAS-extended max (~2000 nm)


def enrich_distance_validations(limit: int = 20) -> int:
    """
    Walk suspect long-haul sightings (distance_nm >= _DISTANCE_SUSPECT_NM
    and distance_validated IS NULL) and validate each via the FA track.
    Writes distance_validated (0/1) and distance_nm_validated (track-derived
    actual distance). Returns count updated. Rate-limited by FA throttle.
    """
    import track_fetcher
    from airports import icao_coords

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, flight_id, source, origin_icao, dest_icao, distance_nm
            FROM sightings
            WHERE distance_validated IS NULL
              AND distance_nm IS NOT NULL
              AND distance_nm >= ?
              AND source = 'flightaware'
              AND flight_id IS NOT NULL AND flight_id != ''
            ORDER BY distance_nm DESC
            LIMIT ?
            """,
            (_DISTANCE_SUSPECT_NM, limit),
        ).fetchall()

    updated = 0
    for row in rows:
        orig = icao_coords((row["origin_icao"] or "").upper())
        dest = icao_coords((row["dest_icao"]   or "").upper())
        result = track_fetcher.validate_distance_from_track(
            row["flight_id"],
            recorded_nm=row["distance_nm"],
            origin_lat_lon=orig,
            dest_lat_lon=dest,
        )
        if result["status"] == "no_track":
            continue   # leave NULL so we retry later
        flag = 1 if result["status"] == "validated" else 0
        with _connect() as conn:
            conn.execute(
                "UPDATE sightings SET distance_validated=?, "
                "distance_nm_validated=? WHERE id=?",
                (flag, result["track_distance_nm"], row["id"]),
            )
            conn.commit()
        log.info("distance validation id=%d recorded=%s track=%s → %s (%s)",
                 row["id"], row["distance_nm"],
                 result["track_distance_nm"], result["status"], result["reason"])
        updated += 1
        import time as _t
        _t.sleep(track_fetcher.THROTTLE_SECONDS)
    return updated


def enrich_arrival_temperatures(limit: int = 50) -> int:
    """
    Fetch and store arrival OAT (°C) for sightings that don't have it yet.
    Parallel to enrich_temperatures() but keyed on dest_icao + arrived_utc.
    Lets the insights page show real-world landing DA instead of ISA-only.
    """
    from airports import icao_coords
    from weather import fetch_oat_c

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, dest_icao, arrived_utc
            FROM sightings
            WHERE arrival_oat_c IS NULL
              AND dest_icao IS NOT NULL AND dest_icao != ''
              AND arrived_utc IS NOT NULL AND arrived_utc != ''
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    updated = 0
    for row in rows:
        coords = icao_coords(row["dest_icao"])
        if not coords:
            continue
        try:
            dt = datetime.fromisoformat(row["arrived_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        oat = fetch_oat_c(coords[0], coords[1], dt, icao=row["dest_icao"])
        if oat is not None:
            with _connect() as conn:
                conn.execute(
                    "UPDATE sightings SET arrival_oat_c=? WHERE id=?",
                    (oat, row["id"]),
                )
                conn.commit()
            updated += 1
    return updated


def bulk_enrich_arrival_temperatures() -> int:
    """
    Enrich ALL sightings missing arrival_oat_c, prioritising high-elevation
    destinations first. Runs in background at startup.
    """
    import time
    from airports import icao_coords, airport_elevation_ft
    from weather import fetch_oat_c

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, dest_icao, arrived_utc
            FROM sightings
            WHERE arrival_oat_c IS NULL
              AND dest_icao IS NOT NULL AND dest_icao != ''
              AND arrived_utc IS NOT NULL AND arrived_utc != ''
            ORDER BY id DESC
            """
        ).fetchall()

    def _priority(r):
        elev = airport_elevation_ft(r["dest_icao"]) or 0
        return -elev

    rows_sorted = sorted(rows, key=_priority)
    updated = 0
    for row in rows_sorted:
        coords = icao_coords(row["dest_icao"])
        if not coords:
            continue
        try:
            dt = datetime.fromisoformat(row["arrived_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        oat = fetch_oat_c(coords[0], coords[1], dt, icao=row["dest_icao"])
        if oat is not None:
            with _connect() as conn:
                conn.execute(
                    "UPDATE sightings SET arrival_oat_c=? WHERE id=?",
                    (oat, row["id"]),
                )
                conn.commit()
            updated += 1
        time.sleep(0.3)
    log.info("bulk_enrich_arrival_temperatures complete: %d updated", updated)
    return updated


def get_fuel_stop_chains(days: int = 90, scope_tier: str = "atlas",
                          tail_number: str | None = None,
                          region: str | None = None) -> list[dict]:
    """
    Find same-tail consecutive legs connected by a short fuel stop (ground
    time < 3 h) where the combined distance >= 80% of the type's baseline.
    The second leg must continue past the fuel stop in the same general
    direction as the first leg; simple drop-off / pickup returns are excluded.
    These are trips ATLAS would have made non-stop.

    scope_tier: 'atlas' (default) — CJ family chains for ATLAS opportunity math.
                'up'              — Mustang chains (up-purchase signal).
                'down'             — CJ4 chains.
    tail_number: optional single-tail filter (used by the record-time detector
                to check whether a specific new landing closed a chain).
    region: optional 'NA' | 'EU_UK' | 'OTHER' filter for the region toggle.
    """
    from atlas_config import ATLAS
    from collections import defaultdict
    from airports import icao_coords
    import math

    def _vector_nm(origin_icao: str, target_icao: str) -> tuple[float, float] | None:
        """Approximate a route vector in nautical miles using local tangent coordinates."""
        origin = icao_coords(origin_icao)
        target = icao_coords(target_icao)
        if not origin or not target:
            return None
        lat1, lon1 = origin
        lat2, lon2 = target
        mean_lat = math.radians((lat1 + lat2) / 2.0)
        dx = (lon2 - lon1) * math.cos(mean_lat) * 60.0
        dy = (lat2 - lat1) * 60.0
        return dx, dy

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    where_extra = ""
    params: list = [scope_tier, cutoff]
    if tail_number:
        where_extra += " AND UPPER(tail_number) = UPPER(?)"
        params.append(tail_number)
    if region in ("NA", "EU_UK", "OTHER"):
        where_extra += " AND region = ? "
        params.append(region)

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT tail_number, ac_type, origin_icao, dest_icao,
                   departed_utc, arrived_utc, distance_nm, operator
            FROM sightings
            WHERE tail_number IS NOT NULL AND tail_number != ''
              AND distance_nm IS NOT NULL AND distance_nm > 0
              AND COALESCE(scope_tier, 'atlas') = ?
              AND arrived_utc >= ?
              {where_extra}
            ORDER BY tail_number, arrived_utc
            """,
            params,
        ).fetchall()

    tail_legs: dict[str, list] = defaultdict(list)
    for r in rows:
        tail_legs[r["tail_number"]].append(dict(r))

    chains = []
    for tail, legs in tail_legs.items():
        legs.sort(key=lambda x: x["arrived_utc"] or "")
        ac      = (legs[0]["ac_type"] or "C525").upper()
        cfg     = ATLAS.get(ac, {})
        baseline = cfg.get("baseline_nm", 1300)

        for i in range(len(legs) - 1):
            a, b = legs[i], legs[i + 1]
            if not (a.get("dest_icao") and b.get("origin_icao")):
                continue
            if a["dest_icao"].upper() != b["origin_icao"].upper():
                continue

            origin_icao = (a.get("origin_icao") or "").upper()
            stop_icao   = (a.get("dest_icao") or "").upper()
            final_icao  = (b.get("dest_icao") or "").upper()
            if not origin_icao or not stop_icao or not final_icao:
                continue

            # Require the final destination to keep moving beyond the fuel stop
            # rather than looping back to the origin for a passenger drop/pickup.
            leg_a_vec = _vector_nm(origin_icao, stop_icao)
            leg_b_vec = _vector_nm(origin_icao, final_icao)
            if not leg_a_vec or not leg_b_vec:
                continue
            ax, ay = leg_a_vec
            bx, by = leg_b_vec
            leg_a_len = math.hypot(ax, ay)
            if leg_a_len <= 0:
                continue

            # Dot product projection: the second leg must progress farther along
            # the first leg's direction than the stop itself.
            if (ax * bx + ay * by) <= (leg_a_len * leg_a_len):
                continue

            # Explicitly reject same-airport returns.
            if final_icao == origin_icao or final_icao == stop_icao:
                continue

            # Estimate ground time at the fuel stop
            try:
                arr_a = datetime.fromisoformat(a["arrived_utc"].replace("Z", "+00:00"))
                dep_b_str = b.get("departed_utc") or ""
                if dep_b_str:
                    dep_b = datetime.fromisoformat(dep_b_str.replace("Z", "+00:00"))
                else:
                    # Back-calculate: arrival B minus estimated leg B flight time
                    arr_b  = datetime.fromisoformat(b["arrived_utc"].replace("Z", "+00:00"))
                    est_h  = b["distance_nm"] / 400.0   # ~400 kt CJ cruise
                    dep_b  = arr_b - timedelta(hours=est_h)
                ground_h = (dep_b - arr_a).total_seconds() / 3600
            except Exception:
                continue

            if not (0 < ground_h < 3.0):
                continue

            combined = (a["distance_nm"] or 0) + (b["distance_nm"] or 0)
            if combined < 0.80 * baseline:
                continue

            chains.append({
                "tail_number":      tail,
                "ac_type":          ac,
                "label":            cfg.get("label", ac),
                "operator":         (b.get("operator") or a.get("operator") or ""),
                "baseline_nm":      baseline,
                "combined_nm":      round(combined),
                "leg_a_nm":         round(a["distance_nm"]),
                "leg_b_nm":         round(b["distance_nm"]),
                "fuel_stop_icao":   stop_icao,
                "origin_icao":      origin_icao or "—",
                "dest_icao":        final_icao or "—",
                "ground_h":         round(ground_h, 1),
                "nonstop_possible": combined <= (baseline + cfg.get("atlas_gain_nm", 0)),
                # Tighter ATLAS classification:
                #   beyond     — even ATLAS can't make it nonstop (combined > baseline + gain)
                #   range_win  — flat-wing CAN'T, ATLAS CAN (baseline < combined <= baseline+gain)
                #               (true range-eliminated fuel stop)
                #   operational — both configurations could nonstop the combined distance,
                #               so the fuel stop was likely for payload/WAT, fuel arbitrage,
                #               crew rest, or operator preference (not range)
                "atlas_advantage": (
                    "beyond"      if combined >  baseline + cfg.get("atlas_gain_nm", 0) else
                    "range_win"   if combined >  baseline else
                    "operational"
                ),
                "arrived_utc":      b["arrived_utc"][:10] if b["arrived_utc"] else "",
            })

    chains.sort(key=lambda c: -c["combined_nm"])
    return chains


def get_performance_airports(da_threshold: float = 5000.0,
                             short_runway_ft: int = 5000) -> dict:
    """
    Return two lists used for the 'Performance-Limited Airports' insight:
      high_da   — airports where density altitude exceeds da_threshold
      short_rwy — airports where longest runway < short_runway_ft

    Uses cached departure_oat_c where available; falls back to ISA standard
    (which means da = elevation for low-ISA-deviation cases).
    """
    from airports import (airport_elevation_ft, airport_longest_runway_ft,
                          density_altitude, isa_temp_c, icao_coords)
    from collections import defaultdict

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT origin_icao, dest_icao, departure_oat_c, tail_number
            FROM sightings
            WHERE (origin_icao IS NOT NULL AND origin_icao != '')
            """
        ).fetchall()

    # Collect per-airport stats
    airport_oat:    dict[str, list[float]] = defaultdict(list)
    airport_tails:  dict[str, set]         = defaultdict(set)
    airport_flights: dict[str, int]        = defaultdict(int)

    for r in rows:
        for icao in filter(None, [r["origin_icao"], r["dest_icao"]]):
            icao = icao.upper()
            airport_flights[icao] += 1
            if r["tail_number"]:
                airport_tails[icao].add(r["tail_number"])
        if r["origin_icao"] and r["departure_oat_c"] is not None:
            airport_oat[r["origin_icao"].upper()].append(r["departure_oat_c"])

    high_da   = []
    short_rwy = []

    all_icaos = set(airport_flights.keys())
    for icao in all_icaos:
        elev = airport_elevation_ft(icao)
        rwy  = airport_longest_runway_ft(icao)
        n_flights = airport_flights[icao]
        n_tails   = len(airport_tails[icao])

        # --- High DA check ---
        if elev is not None and elev >= 2000:
            oat_samples = airport_oat.get(icao, [])
            if oat_samples:
                avg_oat = sum(oat_samples) / len(oat_samples)
                max_oat = max(oat_samples)
            else:
                # ISA standard — da equals elevation; flag if elev ≥ threshold
                avg_oat = isa_temp_c(elev)
                max_oat = avg_oat
            da_avg = density_altitude(elev, avg_oat)
            da_max = density_altitude(elev, max_oat)
            if da_max >= da_threshold or da_avg >= da_threshold:
                # WAT analysis at worst observed temperature (or ISA if no OAT)
                try:
                    from wat_lookup import wat_analysis
                    wat = wat_analysis(elev, max_oat)
                except Exception:
                    wat = None
                high_da.append({
                    "icao":            icao,
                    "elevation_ft":    round(elev),
                    "da_avg":          round(da_avg),
                    "da_max":          round(da_max),
                    "oat_avg_c":       round(avg_oat, 1) if oat_samples else None,
                    "oat_max_c":       round(max_oat, 1) if oat_samples else None,
                    "oat_samples":     len(oat_samples),
                    "tail_count":      n_tails,
                    "flight_count":    n_flights,
                    "wat_flatwing_lb": wat["flatwing_lb"]   if wat else None,
                    "wat_tamarack_lb": wat["tamarack_lb"]   if wat else None,
                    "wat_gain_lb":     wat["atlas_gain_lb"] if wat else None,
                    "wat_deficit_lb":  wat["deficit_lb"]    if wat else None,
                    "wat_limited":     wat["limited"]       if wat else None,
                })

        # --- Short runway check ---
        if rwy is not None and rwy < short_runway_ft:
            high_da_flag = any(
                a["icao"] == icao for a in high_da
            )
            short_rwy.append({
                "icao":              icao,
                "longest_runway_ft": rwy,
                "elevation_ft":      round(elev) if elev is not None else None,
                "also_high_da":      high_da_flag,
                "tail_count":        n_tails,
                "flight_count":      n_flights,
            })

    high_da.sort(key=lambda x: -x["da_max"])
    short_rwy.sort(key=lambda x: x["longest_runway_ft"])
    return {"high_da": high_da[:20], "short_rwy": short_rwy[:20]}


def get_da_distribution() -> dict:
    """
    Distribution of takeoffs (origin) and landings (dest) by density-altitude
    bucket. Drives the 'High/Hot Distribution' chart on the insights page.

    Takeoff DA uses recorded departure_oat_c when available, otherwise ISA
    standard at field elevation. Landing DA uses ISA standard (we don't
    enrich arrival OAT). Buckets are MSL DA in feet.

    Returns {
      "labels":   ["<2k", "2-4k", "4-6k", "6-8k", "8-10k", "10k+"],
      "takeoffs": [n, n, n, n, n, n],
      "landings": [n, n, n, n, n, n],
      "totals":   {"takeoffs": int, "landings": int,
                   "takeoffs_high": int, "landings_high": int,
                   "takeoffs_with_oat": int},
    }
    A 'high' takeoff/landing is DA >= 4,000 ft (where weight limits begin to
    bite on flat-wing CJs).
    """
    from airports import airport_elevation_ft, density_altitude, isa_temp_c

    # Bucket edges (ft DA). Last bucket is open-ended.
    edges  = [2000, 4000, 6000, 8000, 10000]
    labels = ["<2k ft", "2–4k ft", "4–6k ft", "6–8k ft", "8–10k ft", "10k+ ft"]

    def _bucket(da_ft: float) -> int:
        for i, e in enumerate(edges):
            if da_ft < e:
                return i
        return len(edges)

    takeoffs = [0] * len(labels)
    landings = [0] * len(labels)
    n_to = n_la = n_to_oat = n_la_oat = 0
    high_to = high_la = 0
    HIGH_DA = 4000

    # Cache elevations to avoid repeated lookups across many rows
    elev_cache: dict[str, float | None] = {}
    def _elev(icao: str) -> float | None:
        if icao not in elev_cache:
            elev_cache[icao] = airport_elevation_ft(icao)
        return elev_cache[icao]

    with _connect() as conn:
        rows = conn.execute(
            "SELECT origin_icao, dest_icao, departure_oat_c, arrival_oat_c FROM sightings"
        ).fetchall()

    for r in rows:
        # Takeoff
        ori = (r["origin_icao"] or "").upper()
        if ori:
            elev = _elev(ori)
            if elev is not None:
                oat = r["departure_oat_c"]
                if oat is None:
                    oat = isa_temp_c(elev)
                else:
                    n_to_oat += 1
                da = density_altitude(elev, oat)
                takeoffs[_bucket(da)] += 1
                n_to += 1
                if da >= HIGH_DA:
                    high_to += 1
        # Landing — use recorded arrival_oat_c when available, else ISA
        dst = (r["dest_icao"] or "").upper()
        if dst:
            elev = _elev(dst)
            if elev is not None:
                oat = r["arrival_oat_c"] if "arrival_oat_c" in r.keys() else None
                if oat is None:
                    oat = isa_temp_c(elev)
                else:
                    n_la_oat += 1
                da = density_altitude(elev, oat)
                landings[_bucket(da)] += 1
                n_la += 1
                if da >= HIGH_DA:
                    high_la += 1

    return {
        "labels":   labels,
        "takeoffs": takeoffs,
        "landings": landings,
        "totals": {
            "takeoffs":          n_to,
            "landings":          n_la,
            "takeoffs_high":     high_to,
            "landings_high":     high_la,
            "takeoffs_with_oat": n_to_oat,
            "landings_with_oat": n_la_oat,
        },
    }
