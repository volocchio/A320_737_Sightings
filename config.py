"""
config.py — load and validate environment variables
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is not set. See .env.example.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ── API keys ──────────────────────────────────────────────────────────────────
FLIGHTAWARE_API_KEY: str = _optional("FLIGHTAWARE_API_KEY")
ADSBEXCHANGE_API_KEY: str = _optional("ADSBEXCHANGE_API_KEY")
OPENSKY_USERNAME: str = _optional("OPENSKY_USERNAME")
OPENSKY_PASSWORD: str = _optional("OPENSKY_PASSWORD")

# ── Microsoft Teams (Incoming Workflow webhook) ──────────────────────────────
# Create a webhook URL in Teams: channel ⋯ → Workflows → "Post to a channel
# when a webhook request is received". Paste the generated URL here. When set,
# new sightings at watched airports are posted as Adaptive Cards.
TEAMS_WEBHOOK_URL: str = _optional("TEAMS_WEBHOOK_URL")
TEAMS_ENABLED: bool = bool(TEAMS_WEBHOOK_URL)
TEAMS_MUTED: bool = _optional("TEAMS_MUTED", "false").lower() in {"1", "true", "yes", "on"}

# Optional separate webhook for the 4 PM daily USAGE DIGEST card. If set,
# `notify_daily_digest()` posts here instead of TEAMS_WEBHOOK_URL — use this
# to route the internal "who used the tool today" report to a private chat
# (e.g. a DM to yourself) so the whole sales team doesn't see it. If unset,
# the digest is SKIPPED (it does NOT fall back to the shared webhook — that
# defeats the purpose of separating it).
TEAMS_DIGEST_WEBHOOK_URL: str = _optional("TEAMS_DIGEST_WEBHOOK_URL")
TEAMS_DIGEST_ENABLED: bool = bool(TEAMS_DIGEST_WEBHOOK_URL)

# Public URL where the dashboard lives — Teams card "Open dashboard" button
# links here. Override if running behind a different domain.
DASHBOARD_URL: str = _optional("DASHBOARD_URL", "https://a320737sightings.voloaltro.tech/")

# ── Teams recap cards (posted to A320/737 Sightings Thread) ──────────────────────
# Fires at each hour in SUMMARY_HOURS (local time), covering all sightings
# since the previous scheduled fire. Default cadence: 8am, 12pm, 4pm Pacific.
# Each card summarizes everything up to that report. Set
# HOURLY_SUMMARY_ENABLED=0 to disable without touching per-sighting alerts.
HOURLY_SUMMARY_ENABLED: bool = _optional("HOURLY_SUMMARY_ENABLED", "1") not in ("0", "false", "False", "")
HOURLY_SUMMARY_TZ:      str  = _optional("HOURLY_SUMMARY_TZ", "America/Los_Angeles")
SUMMARY_HOURS: list[int] = sorted({
    int(h.strip()) for h in _optional("SUMMARY_HOURS", "8,12,16").split(",")
    if h.strip().isdigit() and 0 <= int(h.strip()) <= 23
}) or [8, 12, 16]

# ── OpenAI (powers the dashboard chat) ───────────────────────────────────────
# Get a key from https://platform.openai.com/api-keys
OPENAI_API_KEY: str = _optional("OPENAI_API_KEY")
OPENAI_MODEL:   str = _optional("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_ENABLED: bool = bool(OPENAI_API_KEY)

# ── Polling ───────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS: int = int(_optional("POLL_INTERVAL_SECONDS", "300"))
LOOKBACK_MINUTES: int = int(_optional("LOOKBACK_MINUTES", "10"))

# ── Aircraft ──────────────────────────────────────────────────────────────────
# Airbus A320-family and Boeing 737-family ICAO type codes.
AIRCRAFT_TYPES: list[str] = [
    t.strip().upper()
    for t in _optional("AIRCRAFT_TYPES", "A318,A319,A320,A321,A19N,A20N,A21N,B736,B737,B738,B739,B37M,B38M,B39M,B3XM").split(",")
    if t.strip()
]

# Adjacent-tier tracking (Mustang / C510). When TRACK_ADJACENT_TYPES=true we
# expand AIRCRAFT_TYPES to also poll the Mustang, tag those sightings with
# scope_tier='up', and fire a dedicated "up-purchase signal" Teams card when
# a Mustang is caught making a fuel-stop chain. When false: byte-for-byte
# identical to prior behavior (Mustangs never enter the pipeline).
TRACK_ADJACENT_TYPES: bool = _optional("TRACK_ADJACENT_TYPES", "false").lower() in ("1", "true", "yes")
if TRACK_ADJACENT_TYPES and "C510" not in AIRCRAFT_TYPES:
    AIRCRAFT_TYPES.append("C510")

# ── Sources active ────────────────────────────────────────────────────────────
# A source is "active" if its required credentials are present
FLIGHTAWARE_ACTIVE: bool = bool(FLIGHTAWARE_API_KEY)
ADSBEXCHANGE_ACTIVE: bool = bool(ADSBEXCHANGE_API_KEY)
OPENSKY_ACTIVE: bool = True  # works anonymously (rate-limited)
ADSBLOL_ACTIVE: bool = True  # adsb.lol community network; no key; ignores LADD blocks

# ── JETNET Connect API ───────────────────────────────────────────────────────
# Aviation ownership / contact / transaction data. Used to enrich prospects
# with owner name, base airport, phone/email, broker of record. Register at
# customer.jetnetconnect.com. Auth is typically username+password → bearer
# token; some tiers issue a static API key instead. Populate whichever pair
# your subscription uses; JETNET_ACTIVE flips true when either is present.
JETNET_USERNAME: str  = _optional("JETNET_USERNAME")
JETNET_PASSWORD: str  = _optional("JETNET_PASSWORD")
JETNET_API_KEY:  str  = _optional("JETNET_API_KEY")
JETNET_BASE_URL: str  = _optional("JETNET_BASE_URL", "https://customer.jetnetconnect.com/api")
JETNET_ACTIVE:   bool = bool((JETNET_USERNAME and JETNET_PASSWORD) or JETNET_API_KEY)
