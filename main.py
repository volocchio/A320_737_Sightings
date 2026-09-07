"""
main.py — A320/737 Sightings polling daemon

Runs three threads:
  1. Polling loop   — queries flight sources every POLL_INTERVAL_SECONDS
  2. Web dashboard  — Flask status page on port 8737
  3. Watchdog       — marks status 'error' if the polling loop stalls

Usage:
    python main.py

Stop with Ctrl+C.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import schedule
from flask import Flask

import config
import database
import airports
import usage_tracker
from sources import Sighting
from sources import adsbexchange, adsblol, flightaware, opensky
from webapp import app as flask_app, daemon_state

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("a320737_sightings")

# Suppress Flask request logs (keep our own logs clean)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _update_state(**kwargs) -> None:
    daemon_state.update(kwargs)


def _poll() -> None:
    log.info("─── Poll starting at %s ───", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    _update_state(status="running", last_error=None)

    all_sightings: list[Sighting] = []

    # adsb.lol: community network, does NOT honor LADD/BARR — catches privacy-
    # blocked tails that FlightAware and FR24 hide. Free, no key required.
    if config.ADSBLOL_ACTIVE:
        try:
            all_sightings.extend(adsblol.fetch_landings(config.LOOKBACK_MINUTES))
        except Exception as exc:  # noqa: BLE001
            log.error("adsb.lol fetch failed: %s", exc)

    # ADS-B Exchange is polled first: it does NOT honor NBAA BARR privacy blocks,
    # so it surfaces tail numbers that FlightAware/FR24 hide at operator request.
    if config.ADSBEXCHANGE_ACTIVE:
        try:
            all_sightings.extend(adsbexchange.fetch_landings(config.LOOKBACK_MINUTES))
        except Exception as exc:  # noqa: BLE001
            log.error("ADS-B Exchange fetch failed: %s", exc)

    # FlightAware: richest metadata (origin, dest, times, operator) but honors
    # BARR — privacy-blocked tails will not appear here.
    if config.FLIGHTAWARE_ACTIVE:
        try:
            all_sightings.extend(flightaware.fetch_landings(config.LOOKBACK_MINUTES))
        except Exception as exc:  # noqa: BLE001
            log.error("FlightAware fetch failed: %s", exc)

    # OpenSky: free fallback; also shows BARR-blocked tails via raw ADS-B.
    if config.OPENSKY_ACTIVE:
        try:
            all_sightings.extend(opensky.fetch_landings(config.LOOKBACK_MINUTES))
        except Exception as exc:  # noqa: BLE001
            log.error("OpenSky fetch failed: %s", exc)

    new_count = 0
    for sighting in all_sightings:
        source = sighting.get("source", "unknown")
        flight_id = sighting.get("flight_id", "")

        if not flight_id:
            continue

        if database.is_already_notified(source, flight_id):
            continue

        log.info(
            "NEW sighting: %s %s → %s (via %s)",
            sighting.get("tail_number", "?"),
            sighting.get("origin_icao", "?"),
            sighting.get("dest_icao", "?"),
            source,
        )

        # Enrich non-FA sightings with FlightAware data (origin, dep-time, operator)
        if source in ("opensky", "adsbexchange", "adsblol") and config.FLIGHTAWARE_ACTIVE:
            tail = sighting.get("tail_number") or ""
            arrived = sighting.get("arrived_utc") or ""
            if tail and arrived:
                enrichment = flightaware.enrich_sighting(tail, arrived)
                if enrichment:
                    log.info("  FA enrichment: %s", enrichment)
                    if enrichment.get("origin_icao") and not sighting.get("origin_icao"):
                        sighting["origin_icao"] = enrichment["origin_icao"]
                        sighting["origin_name"] = enrichment.get("origin_name", "")
                    if enrichment.get("departed_utc") and not sighting.get("departed_utc"):
                        sighting["departed_utc"] = enrichment["departed_utc"]
                    if enrichment.get("operator") and not sighting.get("operator"):
                        sighting["operator"] = enrichment["operator"]
                    # FA canonical flight id unlocks /flights/{id}/track for
                    # non-FA sourced rows (EU adsb.lol tails, mostly).
                    if enrichment.get("fa_flight_id"):
                        sighting["fa_flight_id"] = enrichment["fa_flight_id"]

        origin = sighting.get("origin_icao") or ""
        dest = sighting.get("dest_icao") or ""
        distance_nm = airports.icao_distance_nm(origin, dest) if origin and dest else None
        if distance_nm:
            sighting["distance_nm"] = distance_nm
            log.info("  Distance: %.0f nm", distance_nm)
        database.record_sighting(
            {
                "source": source,
                "flight_id": flight_id,
                "tail_number": sighting.get("tail_number"),
                "ac_type": sighting.get("ac_type"),
                "origin_icao": origin,
                "origin_name": sighting.get("origin_name"),
                "dest_icao": dest,
                "dest_name": sighting.get("dest_name"),
                "departed_utc": sighting.get("departed_utc"),
                "arrived_utc": sighting.get("arrived_utc"),
                "operator": sighting.get("operator"),
                "tracking_url": sighting.get("tracking_url"),
                "distance_nm": distance_nm,
                "fa_flight_id": sighting.get("fa_flight_id"),
            }
        )
        new_count += 1

    now = datetime.now(timezone.utc).isoformat()
    _update_state(
        last_poll_utc=now,
        sightings_total=_total_sightings(),
    )
    log.info("Poll complete — %d new sighting(s) this cycle", new_count)


def _total_sightings() -> int:
    try:
        import sqlite3
        from pathlib import Path
        db = Path(__file__).parent / "sightings.db"
        if not db.exists():
            return 0
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


def _run_scheduler() -> None:
    """Polling loop thread — runs schedule forever."""
    _poll()
    schedule.every(config.POLL_INTERVAL_SECONDS).seconds.do(_poll)
    while True:
        schedule.run_pending()
        time.sleep(10)


def _run_watchdog() -> None:
    """
        Watchdog thread — checks every 2× poll interval whether the daemon
    has polled recently. If not, marks status 'error'.
    """
    stall_seconds = config.POLL_INTERVAL_SECONDS * 2
    alerted = False
    time.sleep(stall_seconds)  # give it time to start before first check

    while True:
        last = daemon_state.get("last_poll_utc")
        if last:
            from datetime import timedelta
            last_dt = datetime.fromisoformat(last)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if age > stall_seconds and not alerted:
                msg = f"A320/737 Sightings daemon has not polled in {int(age)}s — possible crash."
                log.error(msg)
                _update_state(status="error", last_error=msg)
                alerted = True
            elif age <= stall_seconds:
                alerted = False  # reset after recovery
        time.sleep(60)


def _run_hourly_summary() -> None:
    """
    Fixed-time Teams recap card. Fires at each hour in `config.SUMMARY_HOURS`
    (local time, `HOURLY_SUMMARY_TZ`). Each card covers everything since the
    previous scheduled fire (which may be the last slot from the previous
    day). No-op when Teams isn't configured or the feature is disabled.
    DST flips are handled by zoneinfo since the wake times are recomputed
    each loop against wall-clock local time.
    """
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
    except Exception as e:                              # noqa: BLE001
        log.warning("zoneinfo unavailable; recap disabled: %s", e)
        return

    try:
        tz = ZoneInfo(config.HOURLY_SUMMARY_TZ)
    except Exception as e:                              # noqa: BLE001
        log.warning("Bad HOURLY_SUMMARY_TZ %r (%s); recap disabled",
                    config.HOURLY_SUMMARY_TZ, e)
        return

    slots = sorted(set(config.SUMMARY_HOURS))
    if not slots:
        log.warning("SUMMARY_HOURS is empty; recap disabled")
        return

    def _next_fire(now_local: datetime) -> datetime:
        """Return next slot >= now_local (rolls to tomorrow if all today have passed)."""
        today_slots = [now_local.replace(hour=h, minute=0, second=0, microsecond=0)
                       for h in slots]
        for t in today_slots:
            if t > now_local:
                return t
        # Rolled past the last slot today — first slot tomorrow.
        return (now_local + timedelta(days=1)).replace(
            hour=slots[0], minute=0, second=0, microsecond=0)

    def _prev_fire(fire_local: datetime) -> datetime:
        """Slot immediately before `fire_local`. May be yesterday."""
        idx = slots.index(fire_local.hour)
        if idx > 0:
            return fire_local.replace(hour=slots[idx - 1])
        # Wrap to previous day's last slot.
        return (fire_local - timedelta(days=1)).replace(hour=slots[-1])

    while True:
        now_local = datetime.now(tz)
        fire_at   = _next_fire(now_local)
        # +5s slop so any sighting recorded at :59:59 makes it into the dedup view.
        sleep_s = (fire_at - now_local).total_seconds() + 5
        if sleep_s > 0:
            time.sleep(sleep_s)

        if not (config.TEAMS_ENABLED and config.HOURLY_SUMMARY_ENABLED):
            continue

        try:
            local_now  = datetime.now(tz)
            prev_fire  = _prev_fire(fire_at)
            window_hrs = max(0.5, (fire_at - prev_fire).total_seconds() / 3600.0)

            import teams_notifier as _tn
            summary = database.get_hourly_summary(hours=window_hrs)
            # %-I is POSIX-only (works in the Linux container); fall back to
            # %I (zero-padded) on Windows and strip the leading 0.
            try:
                label = local_now.strftime("%-I:00 %p %Z")
            except ValueError:
                label = local_now.strftime("%I:00 %p %Z").lstrip("0")
            ok = _tn.notify_hourly_summary(summary, local_label=label)
            log.info("Recap card sent=%s at %s (window=%.1fh, count=%d, today=%d)",
                     ok, label, window_hrs,
                     summary["count_window"], summary["count_today"])
        except Exception as e:                          # noqa: BLE001
            log.warning("Recap card failed: %s", e)


def _run_daily_digest() -> None:
    """
    Fires once per day at 4:00 PM Pacific time.
    Sends a Teams card summarising who used the dashboard today,
    how long each person was active, and who hasn't shown up yet.
    """
    import time as _time
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
    except Exception as e:                              # noqa: BLE001
        log.warning("zoneinfo unavailable; daily digest disabled: %s", e)
        return

    TZ    = ZoneInfo("America/Los_Angeles")
    HOUR  = 16   # 4 PM

    while True:
        now_local  = datetime.now(TZ)
        fire_today = now_local.replace(hour=HOUR, minute=0, second=0, microsecond=0)
        if now_local >= fire_today:
            # Already past 4 PM today — schedule for tomorrow
            fire_at = fire_today + timedelta(days=1)
        else:
            fire_at = fire_today

        sleep_s = (fire_at - now_local).total_seconds() + 5   # +5s slop
        log.info("Daily digest next fire in %.0f min at %s PT",
                 sleep_s / 60, fire_at.strftime("%Y-%m-%d %H:%M"))
        _time.sleep(sleep_s)

        if not config.TEAMS_ENABLED:
            log.info("Daily digest: Teams not configured, skipping")
            continue

        try:
            import teams_notifier as _tn
            digest = usage_tracker.get_daily_digest(tz_name="America/Los_Angeles")
            ok = _tn.notify_daily_digest(digest)
            log.info("Daily digest sent=%s (%d active, %d no-shows)",
                     ok, len(digest["users"]), len(digest["no_shows"]))
        except Exception as e:                          # noqa: BLE001
            log.warning("Daily digest failed: %s", e)


def _run_morning_prospects() -> None:
    """
    Fires once per day at 8:00 AM Pacific — the "Daily Flight Plan" nudge.
    Posts the team goal, observation + coaching of the day, top trip clusters,
    and the top untouched prospects (rule-tagged, one text each) to the shared
    A320/737 Sightings Thread, linking into the /plan working surface.
    """
    import time as _time
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
    except Exception as e:                              # noqa: BLE001
        log.warning("zoneinfo unavailable; morning prospects disabled: %s", e)
        return

    TZ   = ZoneInfo("America/Los_Angeles")
    HOUR = 8   # 8 AM PT

    while True:
        now_local  = datetime.now(TZ)
        fire_today = now_local.replace(hour=HOUR, minute=0, second=0, microsecond=0)
        fire_at    = fire_today if now_local < fire_today else fire_today + timedelta(days=1)

        sleep_s = (fire_at - now_local).total_seconds() + 5   # +5s slop
        log.info("Daily Flight Plan next fire in %.0f min at %s PT",
                 sleep_s / 60, fire_at.strftime("%Y-%m-%d %H:%M"))
        _time.sleep(sleep_s)

        if not config.TEAMS_ENABLED:
            log.info("Daily Flight Plan: Teams not configured, skipping")
            continue

        try:
            import teams_notifier as _tn
            import sales_plan
            plan = sales_plan.build_plan(days=30, limit=6)
            ok   = _tn.notify_daily_flight_plan(plan, top_n=5)
            log.info("Daily Flight Plan sent=%s (call_list=%d, tails=%s)",
                     ok, len(plan["call_list"]),
                     ",".join(p["tail_number"] for p in plan["call_list"]))
        except Exception as e:                          # noqa: BLE001
            log.warning("Daily Flight Plan failed: %s", e)


def _run_jetnet_ownership_sweep() -> None:
    """
    Nightly JETNET ownership sweep — fires at 6:00 AM Pacific (before the
    7 AM morning prospects and 8 AM recap). Iterates every tail seen in
    the last 90 days, snapshots owner+operator, and fires a Teams
    ownership-change card for each detected flip.

    No-op when JETNET is disabled or Teams isn't configured.
    """
    import time as _time
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
    except Exception as e:                              # noqa: BLE001
        log.warning("zoneinfo unavailable; JETNET sweep disabled: %s", e)
        return

    TZ   = ZoneInfo("America/Los_Angeles")
    HOUR = 6   # 6 AM PT — ahead of morning prospects at 7 AM

    while True:
        now_local  = datetime.now(TZ)
        fire_today = now_local.replace(hour=HOUR, minute=0, second=0, microsecond=0)
        fire_at    = fire_today if now_local < fire_today else fire_today + timedelta(days=1)

        sleep_s = (fire_at - now_local).total_seconds() + 5
        log.info("JETNET ownership sweep next fire in %.0f min at %s PT",
                 sleep_s / 60, fire_at.strftime("%Y-%m-%d %H:%M"))
        _time.sleep(sleep_s)

        if not config.JETNET_ACTIVE:
            log.info("JETNET sweep: JETNET_ACTIVE=false, skipping")
            continue

        try:
            import jetnet_enrichment as _je
            import teams_notifier as _tn
            summary = _je.sweep_active_tails(days=90, limit=500)
            log.info("JETNET sweep: checked=%d changed=%d elapsed=%.1fs",
                     summary["checked"], summary["changed"], summary["elapsed_s"])

            if not config.TEAMS_ENABLED:
                continue

            for diff in summary["diffs"]:
                tail = diff.get("nnumber") or ""
                try:
                    recent = []
                    if tail:
                        dossier = database.get_tail_detail(tail, days=90)
                        if dossier and dossier.get("flights"):
                            recent = dossier["flights"][:3]
                    _tn.notify_ownership_change(diff, recent_flights=recent)
                except Exception as e:                  # noqa: BLE001
                    log.warning("JETNET ownership card for %s failed: %s", tail, e)
        except Exception as e:                          # noqa: BLE001
            log.warning("JETNET ownership sweep failed: %s", e)



def _run_jetnet_entitlement_watch() -> None:
    """
    Poll JETNET every 15 min until the Marketplace entitlement flips, then
    fire a one-time Teams card and stop. Runs on the VPS so it survives the
    laptop being off. A marker file next to the DB prevents re-firing on
    container restarts once the entitlement is confirmed live.

    No-op when JETNET creds aren't configured.
    """
    import time as _time
    import sources.jetnet as _jn
    import teams_notifier as _tn

    marker   = database.DB_PATH.parent / ".jetnet_entitlement_notified"
    INTERVAL = 15 * 60

    if marker.exists():
        log.info("JETNET entitlement already confirmed (marker present); watch idle")
        return
    if not config.JETNET_ACTIVE:
        log.info("JETNET entitlement watch: no creds configured, idle")
        return

    log.info("JETNET entitlement watch started — polling every 15 min until live")
    while True:
        try:
            info = _jn.check_credentials()
            if info.get("ok"):
                log.info("JETNET entitlement is LIVE: servicetype=%s history=%s",
                         info.get("subscriptiontier"), info.get("history_available"))
                try:
                    if config.TEAMS_ENABLED:
                        _tn.notify_jetnet_live(info)
                except Exception as e:                  # noqa: BLE001
                    log.warning("JETNET live card failed: %s", e)
                try:
                    marker.write_text(datetime.now(timezone.utc).isoformat())
                except Exception:                       # noqa: BLE001
                    pass
                return
        except Exception as e:                          # noqa: BLE001
            log.warning("JETNET entitlement watch check failed: %s", e)
        _time.sleep(INTERVAL)



def main() -> None:
    log.info("A320/737 Sightings starting up")
    log.info("Tracking types : %s", ", ".join(config.AIRCRAFT_TYPES))
    log.info("Poll interval  : %ds", config.POLL_INTERVAL_SECONDS)
    log.info(
        "Active sources : %s",
        ", ".join(
            src
            for src, active in [
                ("FlightAware", config.FLIGHTAWARE_ACTIVE),
                ("ADS-B Exchange", config.ADSBEXCHANGE_ACTIVE),
                ("OpenSky", config.OPENSKY_ACTIVE),
            ]
            if active
        ),
    )
    database.init_db()
    usage_tracker.init_table()
    import guest_access
    guest_access.init_tables()
    import jetnet_enrichment
    jetnet_enrichment.init_tables()
    import sales_plan
    sales_plan.init_table()
    backfilled = database.backfill_distances()
    if backfilled:
        log.info("Backfilled distance_nm for %d existing sightings", backfilled)

    # Region classification (NA / EU_UK / OTHER) — populates the sales-region
    # filter dropdown. Runs against pre-migration history only; new rows are
    # tagged at insert time.
    def _backfill_regions():
        n = database.backfill_regions()
        if n:
            log.info("Region backfilled for %d existing sightings", n)
    threading.Thread(target=_backfill_regions, daemon=True, name="region-backfill").start()

    # Load FAA registry in background so N-number → S/N lookups work for fleet detection
    def _load_faa_and_backfill():
        import tamarack_fleet as _tf
        ok = _tf.load_faa_registry()
        if ok:
            log.info("FAA registry loaded — %d 525-series aircraft mapped", len(_tf._nnum_to_sn))
            updated = database.backfill_fleet_status()
            if updated:
                log.info("Fleet status backfilled for %d existing sightings", updated)
        else:
            log.warning("FAA registry load failed — fleet detection unavailable")
    threading.Thread(target=_load_faa_and_backfill, daemon=True).start()

    # Bulk-enrich departure OAT in background (high-elevation airports first)
    # so the WAT analysis on the insights page has real temperature data.
    threading.Thread(target=database.bulk_enrich_temperatures, daemon=True).start()
    # Bulk-enrich arrival OAT in background so landing-side DA on /insights
    # reflects real-world heat (high-elevation destinations first).
    threading.Thread(target=database.bulk_enrich_arrival_temperatures,
                     daemon=True).start()
    log.info("Background OAT enrichment started")

    # Background backfill of climb-profile metrics from FlightAware track API
    # (throttled at 1.5s per call). Processes ~40 sightings/minute.
    def _backfill_climb_profiles():
        import time as _time
        import track_fetcher
        _time.sleep(30)   # let other init settle first
        while True:
            try:
                summary = track_fetcher.backfill_pending(limit=50)
                if summary["processed"] == 0:
                    _time.sleep(900)   # nothing pending — sleep 15 min
                else:
                    log.info("Climb-profile backfill: %s", summary)
                    _time.sleep(10)
            except Exception as e:   # noqa: BLE001
                log.warning("Climb-profile backfill error: %s", e)
                _time.sleep(60)
    threading.Thread(target=_backfill_climb_profiles, daemon=True,
                     name="climb-backfill").start()
    log.info("Background climb-profile backfill started")

        # Fixed-time Teams recap thread — fires at each hour in SUMMARY_HOURS
    # (local time, HOURLY_SUMMARY_TZ). No-op if Teams isn't configured or
    # HOURLY_SUMMARY_ENABLED is false.
    threading.Thread(target=_run_hourly_summary, daemon=True,
                     name="hourly-summary").start()

    # Daily usage digest — fires at 4:00 PM Pacific every day.
    threading.Thread(target=_run_daily_digest, daemon=True,
                     name="daily-digest").start()
    log.info("Daily usage digest scheduled at 4:00 PM PT")

    # Daily Flight Plan — fires at 8:00 AM Pacific every day (team goal +
    # observation/coaching + trip clusters + top untouched prospects).
    threading.Thread(target=_run_morning_prospects, daemon=True,
                     name="daily-flight-plan").start()
    log.info("Daily Flight Plan scheduled at 8:00 AM PT")

    # JETNET ownership sweep — fires at 6:00 AM PT, ahead of morning
    # prospects and the 8am recap. Only does work when JETNET_ACTIVE is on.
    threading.Thread(target=_run_jetnet_ownership_sweep, daemon=True,
                     name="jetnet-sweep").start()
    log.info("JETNET ownership sweep scheduled at 6:00 AM PT "
             "(active=%s)", config.JETNET_ACTIVE)

    # One-time entitlement watch — polls every 15 min until the Marketplace
    # entitlement flips, fires a Teams card, then stops. No-op if no creds.
    threading.Thread(target=_run_jetnet_entitlement_watch, daemon=True,
                     name="jetnet-entitlement-watch").start()
    if config.TEAMS_ENABLED and config.HOURLY_SUMMARY_ENABLED:
        log.info("Teams recap scheduled at %s local (%s)",
                 ", ".join(f"{h:02d}:00" for h in config.SUMMARY_HOURS),
                 config.HOURLY_SUMMARY_TZ)

    active = [
        src for src, active in [
            ("FlightAware", config.FLIGHTAWARE_ACTIVE),
            ("ADS-B Exchange", config.ADSBEXCHANGE_ACTIVE),
            ("OpenSky", config.OPENSKY_ACTIVE),
        ] if active
    ]
    _update_state(active_sources=active, sightings_total=_total_sightings())

    # Thread 1 — polling daemon
    t_poll = threading.Thread(target=_run_scheduler, daemon=True, name="poller")
    t_poll.start()

    # Thread 2 — watchdog
    t_watch = threading.Thread(target=_run_watchdog, daemon=True, name="watchdog")
    t_watch.start()

    # Thread 3 — Flask dashboard on port 8737
    log.info("Dashboard running at http://0.0.0.0:8737")
    flask_app.run(host="0.0.0.0", port=8737, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
