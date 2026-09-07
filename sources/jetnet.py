"""
sources/jetnet.py — JETNET Connect API client.

STATUS: scaffolded, INERT until `JETNET_USERNAME`/`JETNET_PASSWORD` land in
the env. Jason Kessler at JETNET (2026-09-02) confirmed our tier (Live
refresh · Business Jets + Turboprops · Marketplace + History) at
$8,717.40/year, and the three endpoints we asked about are all available:
  · Aircraft/getRegNumber       — N-number lookup
  · Aircraft/getRelationships   — current owner + operator
  · Aircraft/getHistoryList     — transaction history

Auth model (reconciled 2026-09-03 against JETNET help center docs):
    POST {BASE}/Admin/APILogin  {emailAddress, password}
        -> {bearerToken, apiToken}
    Every downstream call is `{Controller}/{method}/{...positional}/{apiToken}`
    AND carries `Authorization: Bearer {bearerToken}`. apiToken lives as the
    final path segment, NOT a query param.

Token lifetime (per help center "API Token Longevity" article):
    The apiToken is valid for 60 minutes from the LAST request made — i.e.
    activity-based, not absolute. We proactively re-login after 45 minutes
    of idle to leave a 15 min safety buffer, AND we react to text-based
    "ERROR: EXPIRED SECURITY TOKEN" responses by dumping tokens and retrying.

Error model (per help center "API Error Codes & Troubleshooting"):
    Responses use HTTP 200 even for logical errors. Real signal is the
    `responsestatus` field in the JSON body:
        "SUCCESS"                       — normal
        "ERROR: EXPIRED SECURITY TOKEN" — force re-login
        "ERROR: INVALID SECURITY TOKEN" — force re-login
        "ERROR: NO RESULTS FOUND [...]" — return None cleanly (not an error)
        "ERROR: RESULTS EXCEEDED LIMITS [N]" — quota; log and skip
        "ERROR: INVALID PARAMETER [...]" / "ERROR: MISSING REQUIRED [...]"
                                        — programming bug; log
        "ERROR: INVALID ACCOUNT"        — account not authorized; log

Some tiers issue a static X-API-Key instead — this client falls back to
header auth when only `JETNET_API_KEY` is present.

Docs (need Tiara's Evolution creds to open Swagger):
  · Swagger      https://customer.jetnetconnect.com/swagger/index.html
  · SDK          https://github.com/jetnet-llc/jtcTestClient
  · Login proc   https://support.jetnet.com/hc/en-us/articles/35573721572749
  · Token life   https://support.jetnet.com/hc/en-us/articles/35653040131597
  · Errors       https://support.jetnet.com/hc/en-us/articles/35661577258893
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config

log = logging.getLogger(__name__)

_bearer_token: str | None = None
_api_token:    str | None = None
_token_last_used_at: float = 0.0
_last_login_error: str | None = None
_TOKEN_IDLE_LIMIT_S: float = 45 * 60   # docs say 60 min idle; leave 15 min buffer


def is_enabled() -> bool:
    """True when at least one form of JETNET credential is configured."""
    return bool(config.JETNET_ACTIVE)


def _base() -> str:
    return (config.JETNET_BASE_URL or "").rstrip("/")


def _reset_tokens() -> None:
    """Drop cached tokens so the next call forces a fresh login."""
    global _bearer_token, _api_token, _token_last_used_at
    _bearer_token = None
    _api_token = None
    _token_last_used_at = 0.0


def _login() -> tuple[str, str] | None:
    """
    Exchange username+password for the (bearerToken, apiToken) pair. Cached
    for 45 minutes of idle time (docs: 60 min from last request, we leave
    15 min slack). Returns None when creds are unset (static-key tiers) or
    the exchange fails.

    Per JETNET docs: this is POST with a JSON body `{emailAddress, password}`
    (camelCase A). The `jtcTestClient` C# sample uses PUT — kept as a
    fallback in case the endpoint accepts both and one tier prefers the
    other. bearerToken → Authorization header; apiToken → URL path segment.

    On failure, stashes the JETNET-returned error text in `_last_login_error`
    so `check_credentials()` can surface it — this is how we distinguish
    "wrong password" from "account not yet authorized for API access" from
    "network unreachable" from "endpoint moved."
    """
    global _bearer_token, _api_token, _token_last_used_at, _last_login_error
    if _bearer_token and _api_token \
       and (time.time() - _token_last_used_at) < _TOKEN_IDLE_LIMIT_S:
        return _bearer_token, _api_token
    if not (config.JETNET_USERNAME and config.JETNET_PASSWORD):
        _last_login_error = "credentials not configured"
        return None
    url  = f"{_base()}/Admin/APILogin"
    body = {"emailAddress": config.JETNET_USERNAME,
            "password":     config.JETNET_PASSWORD}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    resp = None
    last_error: str | None = None
    for method in ("POST", "PUT"):
        try:
            resp = requests.request(method, url, json=body,
                                    headers=headers, timeout=15)
        except requests.RequestException as e:
            if not (last_error and last_error.startswith("ERROR:")):
                last_error = f"network error ({method}): {e}"
            log.warning("JETNET login %s network error: %s", method, e)
            resp = None
            continue
        if resp.status_code == 200:
            break
        # Try to peel a JETNET-specific error out of the body. On 401 the
        # body is often JSON with the real error hiding in `apiToken`.
        this_error: str
        try:
            body_json = resp.json()
            token_field = str(body_json.get("apiToken") or "")
            if token_field.startswith("ERROR:"):
                this_error = token_field
            else:
                this_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except ValueError:
            this_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        # Preserve the FIRST JETNET-app-level ERROR: message — a subsequent
        # HTTP transport error (e.g. PUT returning 405) shouldn't clobber
        # the more meaningful "INVALID ACCOUNT" from the first attempt.
        if not (last_error and last_error.startswith("ERROR:")):
            last_error = this_error
        log.info("JETNET login %s -> HTTP %d — %s",
                 method, resp.status_code, this_error[:200])
        # If JETNET's app layer already answered with a definitive ERROR:,
        # trying a different HTTP method won't change the answer.
        if this_error.startswith("ERROR:"):
            resp = None
            break

    if resp is None or resp.status_code != 200:
        _last_login_error = last_error or "login failed"
        log.warning("JETNET login failed: %s", _last_login_error[:200])
        return None

    try:
        data = resp.json() or {}
    except ValueError:
        _last_login_error = f"non-JSON response: {resp.text[:200]}"
        log.warning("JETNET login returned non-JSON: %s", resp.text[:200])
        return None

    bearer = data.get("bearerToken") or data.get("token")
    api    = data.get("apiToken")   or data.get("apiAccessToken")
    if not bearer or not api:
        _last_login_error = f"missing tokens in response: keys={list(data.keys())}"
        log.warning("JETNET login OK but missing tokens: keys=%s", list(data.keys()))
        return None

    _bearer_token = str(bearer)
    _api_token    = str(api)
    _token_last_used_at = time.time()
    _last_login_error = None
    log.info("JETNET login OK (bearer %s..., api %s...)",
             _bearer_token[:8], _api_token[:8])
    return _bearer_token, _api_token


def last_login_error() -> str | None:
    """Expose the last login failure reason for diagnostics endpoints."""
    return _last_login_error


def _response_status(payload: Any) -> str:
    """
    Extract JETNET's `responsestatus` field from a decoded JSON response.
    Handles list responses (`getHistoryList` returns a list) by peeking at
    the first element. Returns an empty string when no status is present.
    """
    if isinstance(payload, dict):
        return str(payload.get("responsestatus") or "").strip()
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return str(payload[0].get("responsestatus") or "").strip()
    return ""


def _is_expired_token_error(status: str) -> bool:
    up = status.upper()
    return ("EXPIRED SECURITY TOKEN" in up) or ("INVALID SECURITY TOKEN" in up)


def _is_no_results_error(status: str) -> bool:
    return status.upper().startswith("ERROR: NO RESULTS FOUND")


def _is_error(status: str) -> bool:
    return status.upper().startswith("ERROR:")


def _call(
    path: str,
    method: str = "GET",
    body:   dict[str, Any] | None = None,
    _retry_on_expired: bool = True,
) -> dict | list | None:
    """
    Hit a JETNET Connect endpoint. `path` should be the controller/method
    fragment WITHOUT the trailing apiToken segment — this function appends
    it. Uses bearer-token auth when username/password are configured;
    falls back to `X-API-Key` for static-key tiers.

    Handles two failure modes:
      · HTTP 401 / 5xx      — dumps tokens, returns None
      · JSON responsestatus:
          EXPIRED / INVALID TOKEN → dump + retry once (auto re-login)
          NO RESULTS FOUND        → return None (empty result, not an error)
          any other ERROR:        → log and return None
    """
    global _token_last_used_at
    if not is_enabled():
        return None
    headers: dict[str, str] = {"Accept": "application/json"}
    creds = _login()
    if creds:
        bearer, api = creds
        headers["Authorization"] = f"Bearer {bearer}"
        url = f"{_base()}/{path.strip('/')}/{api}"
    elif config.JETNET_API_KEY:
        headers["X-API-Key"] = config.JETNET_API_KEY
        url = f"{_base()}/{path.strip('/')}"
    else:
        return None

    try:
        resp = requests.request(method.upper(), url,
                                json=body, headers=headers, timeout=25)
    except requests.RequestException as e:
        log.warning("JETNET %s %s network error: %s", method, path, e)
        return None

    if resp.status_code == 401 and creds:
        _reset_tokens()
        log.info("JETNET 401 on %s — tokens invalidated", path)
        if _retry_on_expired:
            return _call(path, method=method, body=body, _retry_on_expired=False)
        return None
    if resp.status_code != 200:
        log.warning("JETNET %s %s -> HTTP %d %s",
                    method, path, resp.status_code, resp.text[:200])
        return None

    try:
        payload = resp.json()
    except ValueError:
        log.warning("JETNET %s %s returned non-JSON", method, path)
        return None

    status = _response_status(payload)
    if _is_expired_token_error(status) and creds:
        _reset_tokens()
        log.info("JETNET expired-token body on %s — re-login and retry", path)
        if _retry_on_expired:
            return _call(path, method=method, body=body, _retry_on_expired=False)
        return None
    if _is_no_results_error(status):
        return None
    if _is_error(status):
        log.warning("JETNET %s %s responded: %s", method, path, status[:200])
        return None

    _token_last_used_at = time.time()
    return payload


def check_credentials() -> dict:
    """
    Diagnostic helper for `/admin/test-jetnet`. When bearer creds are set,
    actually hits `Utility/getAccountInfo` to confirm the login round-trips
    end-to-end. Surfaces the capability fields JETNET returns.

    Real `getAccountInfo` response (verified 2026-09-03 against a live account)
    puts fields at the ROOT with explicit booleans, not an entitlements array:
      · servicetype        Marketplace | Aerodex
      · servicefrequency   Live | Weekly | Monthly
      · maxrecords         per-request record cap
      · historyavailable   transaction-history access (bool)
      · flightsavailable   flight-data access (bool)
      · evaluesavailable   valuation access (bool)
      · subid              subscription id
    The Claude skill's `examples/account-info.json` showed a nested
    `accountinfo` + `entitlements[]` shape; we handle both, preferring the
    explicit booleans when present.
    Never raises.
    """
    if not is_enabled():
        return {"ok": False, "reason": "JETNET creds not configured"}
    if config.JETNET_USERNAME and config.JETNET_PASSWORD:
        creds = _login()
        if not creds:
            return {"ok": False, "auth_mode": "bearer", "base_url": _base(),
                    "reason": last_login_error() or "login failed (see server log)"}
        info = _call("Utility/getAccountInfo") or {}
        ai = info.get("accountinfo") if isinstance(info, dict) else None
        # Some tiers put fields at the root — try the sub-object first, then
        # fall back to root. Case-insensitive lookup because docs vary.
        source = ai if isinstance(ai, dict) else info
        def _pick(*keys: str) -> Any:
            for k in keys:
                if isinstance(source, dict) and source.get(k) is not None:
                    return source.get(k)
            return None
        # Prefer the explicit boolean when the account returns one; only fall
        # back to the entitlements-array membership for tiers that use it.
        history_bool = _pick("historyavailable", "Historyavailable")
        if history_bool is not None:
            history_available = bool(history_bool)
        else:
            ents = _pick("entitlements") or []
            ent_set = {str(e).strip().lower() for e in ents} if isinstance(ents, list) else set()
            history_available = ("history" in ent_set) if ent_set else None
        ok = bool(info)
        return {
            "ok":               ok,
            "auth_mode":        "bearer",
            "base_url":         _base(),
            "reason":           "login + getAccountInfo OK" if ok
                                else "login OK but getAccountInfo returned nothing",
            "accountname":      _pick("accountname",     "AccountName"),
            "subscriptiontier": _pick("subscriptiontier", "SubscriptionTier",
                                      "servicetype",      "Servicetype"),
            "servicefrequency": _pick("servicefrequency", "Servicefrequency"),
            "ratelimit":        _pick("ratelimit",       "RateLimit",
                                      "maxrecords",      "Maxrecords"),
            "history_available":  history_available,
            "flights_available":  (bool(_pick("flightsavailable", "Flightsavailable"))
                                   if _pick("flightsavailable", "Flightsavailable") is not None else None),
            "evalues_available":  (bool(_pick("evaluesavailable", "Evaluesavailable"))
                                   if _pick("evaluesavailable", "Evaluesavailable") is not None else None),
            "expirationdate":   _pick("expirationdate",  "ExpirationDate"),
            "account":          info if ok else None,
        }
    return {
        "ok":        True,   # can't verify a static key without a real call
        "auth_mode": "api_key_header",
        "base_url":  _base(),
        "reason":    "static X-API-Key configured (verify with a real lookup)",
    }



def _normalize_reg(nnumber: str) -> str:
    """JETNET reg lookups accept the full tail as-is (e.g. `N525AB`)."""
    return nnumber.strip().upper()


def _pick_relationship(companyrelationships: list, kind: str) -> dict:
    """
    Pick the first `companyrelationships` entry with `companyrelation == kind`
    (e.g. "Owner", "Operator"). Returns {} when the array is empty or the
    kind isn't present. Case-insensitive match.
    """
    if not companyrelationships:
        return {}
    kind_lc = kind.strip().lower()
    for r in companyrelationships:
        if str(r.get("companyrelation") or "").strip().lower() == kind_lc:
            return r
    return {}


def lookup_aircraft(nnumber: str) -> dict | None:
    """
    Look up an aircraft by N-number via `GET Aircraft/getRegNumber/{reg}`.
    Returns a normalized dict of the fields A320/737 Sightings cares about, or
    None on miss / disabled / error.

    Per the JETNET Claude skill: the response nests every field under
    `aircraftresult`, and the same call also carries `companyrelationships`
    (Owner, Operator, and other roles) as an inline flat-prefixed array —
    so `lookup_owner()` can share this call and never hit the network twice
    for the same tail.
    """
    if not is_enabled() or not nnumber:
        return None
    reg = _normalize_reg(nnumber)
    data = _call(f"Aircraft/getRegNumber/{reg}")
    if not data or not isinstance(data, dict):
        return None
    return _normalize_aircraft(nnumber, data)


def lookup_owner(nnumber: str) -> dict | None:
    """
    Current registered owner + operator for a tail. Sourced from
    `Aircraft/getRegNumber`'s inline `companyrelationships` array so we
    make ONE API call, not two.

    We deliberately skip `Aircraft/getRelationships` (which requires
    a prior `aircraftid` lookup + a POST + returns a different response
    shape) because for our owner-enrichment use case `getRegNumber` alone
    gives Owner + Operator + phone + email + address in one round-trip.
    """
    if not is_enabled() or not nnumber:
        return None
    reg = _normalize_reg(nnumber)
    data = _call(f"Aircraft/getRegNumber/{reg}")
    if not data or not isinstance(data, dict):
        return None
    return _normalize_owner(nnumber, data)


def lookup_history(nnumber: str, days: int | None = None) -> list[dict] | None:
    """
    Transaction history for a tail via `POST Aircraft/getHistoryList`.

    JETNET's `AcHistoryOptions` schema expects `aircraftid` (integer), not
    `regnbrlist`. So this is a two-call flow:
      1. Resolve `nnumber` → `aircraftid` via `getRegNumber`.
      2. POST `getHistoryList` with `{aircraftid, startdate, ...}`.

    `days`, when set, becomes the `startdate` (MM/DD/YYYY) filter. Returns
    a list of history entries, or None on disabled / not-found / error.
    """
    if not is_enabled() or not nnumber:
        return None
    ac = lookup_aircraft(nnumber)
    if not ac or not ac.get("aircraftid"):
        return None
    body: dict[str, Any] = {
        "aircraftid":   ac["aircraftid"],
        "airframetype": "None",
        "maketype":     "None",
        "modelid":      0,
        "aclist":       [],
        "modlist":      [],
        "companyid":    0,
        "transtype":    ["None"],
    }
    if days and days > 0:
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc) - timedelta(days=days)
        body["startdate"] = start.strftime("%m/%d/%Y")
    data = _call("Aircraft/getHistoryList", method="POST", body=body)
    if not data or not isinstance(data, dict):
        return None
    return data.get("history") or []


def _normalize_aircraft(nnumber: str, data: dict) -> dict:
    """
    Map a raw `getRegNumber` response into the A320/737 Sightings aircraft shape.

    Response layout (per Claude skill `examples/tail-lookup.json`):
      response.aircraftresult.{aircraftid, modelid, make, model,
                               serialnbr, regnbr, yearmfr, yeardlv,
                               weightclass, baseicao, baseairport,
                               ownership, usage, maintained, icaotype,
                               companyrelationships[]}

    We keep the raw dict so downstream code can peek at anything we didn't
    normalize. `aircraftid` is the durable join key across every other call.
    """
    ar = data.get("aircraftresult") or {}
    return {
        "nnumber":     nnumber.upper(),
        "aircraftid":  ar.get("aircraftid"),
        "modelid":     ar.get("modelid"),
        "make":        ar.get("make"),
        "model":       ar.get("model"),
        "serial":      ar.get("serialnbr") or ar.get("sernbr"),
        "year_mfr":    ar.get("yearmfr"),
        "year_dlv":    ar.get("yeardlv"),
        "weightclass": ar.get("weightclass"),
        "icaotype":    ar.get("icaotype"),
        "base_icao":   ar.get("baseicao"),
        "base_name":   ar.get("baseairport"),
        "ownership":   ar.get("ownership"),
        "usage":       ar.get("usage"),
        "maintained":  ar.get("maintained"),
        "raw":         data,
    }


def _normalize_owner(nnumber: str, data: dict) -> dict:
    """
    Extract current Owner + Operator from `getRegNumber`'s inline
    `aircraftresult.companyrelationships` array.

    Per Claude skill `examples/tail-lookup.json`: this endpoint returns a
    FLAT structure with fields prefixed `companyname`, `contactfirstname`,
    `contactbestphone`, etc. and the relationship kind lives in
    `companyrelation`. (Contrast with `getRelationships`, which returns a
    NESTED `company.name` / `contact.firstname` shape and uses
    `relationtype` — we deliberately avoid that endpoint here.)

    Returns a dict with the OWNER company/contact fields promoted to the
    top level (matching what `_upsert_owner_row` in `jetnet_enrichment.py`
    expects) plus a separate `operator` string when a distinct Operator
    entry is present.
    """
    ar = data.get("aircraftresult") or {}
    rels = ar.get("companyrelationships") or []
    owner_row    = _pick_relationship(rels, "Owner")
    operator_row = _pick_relationship(rels, "Operator")

    owner_name    = owner_row.get("companyname")    if owner_row    else None
    operator_name = operator_row.get("companyname") if operator_row else None

    # Prefer Owner contact details; fall back to Operator if Owner has none.
    def _contact(row: dict) -> dict:
        first  = (row.get("contactfirstname") or "").strip()
        last   = (row.get("contactlastname")  or "").strip()
        full   = f"{first} {last}".strip()
        title  = (row.get("contacttitle")     or "").strip()
        return {
            "name":    full or None,
            "title":   title or None,
            "phone":   row.get("contactbestphone") or row.get("contactphone"),
            "email":   row.get("contactemail"),
            "address": row.get("companyaddress1") or row.get("companyaddress"),
            "city":    row.get("companycity"),
            "state":   row.get("companystate") or row.get("companystateabbr"),
            "country": row.get("companycountry"),
        }

    contact = _contact(owner_row) if owner_row else _contact(operator_row)

    return {
        "nnumber":    nnumber.upper(),
        "aircraftid": ar.get("aircraftid"),
        "companyid":  owner_row.get("companyid") if owner_row else (
                      operator_row.get("companyid") if operator_row else None),
        "contactid":  owner_row.get("contactid") if owner_row else (
                      operator_row.get("contactid") if operator_row else None),
        "owner":      owner_name,
        "raw": {
            **data,
            # Flatten operator name into the raw dict for _upsert_owner_row
            # (which reads raw.operatorname / raw.operator).
            "operatorname": operator_name,
        },
        "contact":    contact,
    }


