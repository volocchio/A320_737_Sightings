"""
teams_notifier.py — Post Adaptive Cards to a Microsoft Teams channel via an
Incoming Workflow webhook.

Setup (one-time, done in Teams by the channel owner):
  1. Open the target channel → ⋯ → Workflows.
  2. Pick the template "Post to a channel when a webhook request is received".
  3. Finish the flow; copy the generated webhook URL.
  4. Set env var TEAMS_WEBHOOK_URL=<that URL> and redeploy.

Teams Workflows webhooks accept the Adaptive Card v1.4 format wrapped in the
standard message envelope shown below.
"""

from __future__ import annotations

import logging
import re

import requests

import config

log = logging.getLogger(__name__)

# Keep cards on the most broadly supported Teams renderer profile.
_CARD_SCHEMA = "https://adaptivecards.io/schemas/adaptive-card.json"
_CARD_VERSION = "1.2"


def _clean_text(s: str | None, limit: int = 2500) -> str:
    """Sanitize text for Teams card fields to avoid renderer failures."""
    if not s:
        return ""
    text = str(s).replace("\r\n", "\n").replace("\r", "\n")
    # Strip non-printable control chars except newlines/tabs.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _adaptive_card(title: str, facts: list[tuple[str, str]],
                   subtitle: str = "", action_url: str = "",
                   action_label: str = "Open dashboard") -> dict:
    """Build a Teams-compatible Adaptive Card payload."""
    title = _clean_text(title, limit=180)
    subtitle = _clean_text(subtitle, limit=500)
    body: list[dict] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": title, "wrap": True},
    ]
    if subtitle:
        body.append({"type": "TextBlock", "spacing": "None",
                     "text": subtitle, "wrap": True, "isSubtle": True})
    if facts:
        body.append({
            "type": "FactSet",
            "facts": [{"title": _clean_text(k, limit=80),
                       "value": _clean_text(v, limit=600)} for k, v in facts],
        })

    actions: list[dict] = []
    if action_url:
        actions.append({"type": "Action.OpenUrl",
                        "title": action_label, "url": action_url})

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema":   _CARD_SCHEMA,
                "type":      "AdaptiveCard",
                "version":   _CARD_VERSION,
                "body":      body,
                "actions":   actions,
            },
        }],
    }
    return card


def send(payload: dict, webhook_url: str | None = None) -> bool:
    """POST a payload to the configured Teams webhook. Returns True on success.

    If ``webhook_url`` is provided, post there instead of the default
    ``config.TEAMS_WEBHOOK_URL``. Used by ``notify_daily_digest()`` to route
    the private usage report to a separate chat.
    """
    url = webhook_url or config.TEAMS_WEBHOOK_URL
    if not url:
        log.debug("Teams disabled (no webhook URL); skipping notify")
        return False
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 400:
            log.warning("Teams webhook returned %d: %s",
                        resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.warning("Teams webhook POST failed: %s", exc)
        return False


def notify_sighting(sighting: dict,
                    matched_airports: list[str] | None = None,
                    matched_tail: str | None = None) -> bool:
    """
    Notify the Teams channel about a sighting that matched the airport
    watch list, the tail watch list, or both. Returns True if the webhook
    accepted the message.
    """
    matched_airports = matched_airports or []
    tail = sighting.get("tail_number") or "—"
    origin = sighting.get("origin_icao") or "?"
    dest   = sighting.get("dest_icao")   or "?"
    sub    = sighting.get("ac_subvariant") or sighting.get("ac_type") or ""
    operator = sighting.get("operator") or "—"
    dist     = sighting.get("distance_nm")
    arrived_utc_str = sighting.get("arrived_utc") or ""
    arrived  = arrived_utc_str[:16].replace("T", " ")

    # Local time at the destination airport (e.g. "5:07 PM PDT")
    try:
        import airports as _ap
        arrived_local = _ap.local_time_at_icao(arrived_utc_str, sighting.get("dest_icao") or "")
    except Exception:                                   # noqa: BLE001
        arrived_local = ""

    # Build trigger label from whichever watch list(s) matched
    triggers: list[str] = []
    if matched_tail:
        triggers.append(f"watched tail: {matched_tail}")
    if matched_airports:
        triggers.append(f"watched airport: {', '.join(matched_airports)}")
    trigger_label = " \u00b7 ".join(triggers) if triggers else "sighting"

    title    = f"\U0001F6EC  {tail} \u2014 {trigger_label}"
    subtitle = f"{sub} \u00b7 {origin} \u2192 {dest}"
    if dist:
        subtitle += f" \u00b7 {int(dist)} nm"

    if arrived_local and arrived:
        arrived_value = f"{arrived_local}  \u00b7  {arrived} UTC"
    elif arrived:
        arrived_value = f"{arrived} UTC"
    else:
        arrived_value = "—"

    facts = [
        ("Type",     str(sub) or "—"),
        ("Operator", str(operator)),
        ("Arrived",  arrived_value),
    ]
    if sighting.get("tracking_url"):
        facts.append(("Track", sighting["tracking_url"]))

    # JETNET owner enrichment — surface the registered owner + contact on
    # every watched-tail card when we have it cached. Silently skipped when
    # JETNET is disabled or the tail hasn't been enriched yet.
    try:
        import jetnet_enrichment as _je
        _owner_row = _je.get_owner_cached(tail) if tail and tail != "—" else None
        _owner_line = _je.format_owner_line(_owner_row)
        if _owner_line:
            facts.append(("Registered owner", _owner_line))
        for _rel, _val in _je.contact_facts(_owner_row):
            facts.append((_rel, _val))
    except Exception:                                    # noqa: BLE001
        pass

    payload = _adaptive_card(
        title       = title,
        subtitle    = subtitle,
        facts       = facts,
        action_url  = config.DASHBOARD_URL,
        action_label= "Open A320/737 Sightings dashboard",
    )
    return send(payload)


def notify_ownership_change(diff: dict,
                            recent_flights: list[dict] | None = None) -> bool:
    """
    Fire a Teams card when the nightly JETNET sweep detects that a tail
    has flipped owner or operator since we last saw it. `diff` is the dict
    returned by `jetnet_enrichment.snapshot_and_diff()`. `recent_flights` is
    optional context — a few recent legs to help the sales team recognize
    the tail.
    """
    tail = (diff.get("nnumber") or "—").upper()
    before = diff.get("before") or {}
    after  = diff.get("after")  or {}
    payload = diff.get("payload") or {}
    contact = (payload.get("contact") or {})

    old_owner    = (before.get("owner")    or "—").strip() or "—"
    new_owner    = (after.get("owner")     or "—").strip() or "—"
    old_operator = (before.get("operator") or "—").strip() or "—"
    new_operator = (after.get("operator")  or "—").strip() or "—"

    owner_changed    = old_owner.lower()    != new_owner.lower()
    operator_changed = old_operator.lower() != new_operator.lower()

    change_bits: list[str] = []
    if owner_changed:    change_bits.append("owner")
    if operator_changed: change_bits.append("operator")
    change_label = " + ".join(change_bits) or "record"

    title    = f"\U0001F504  Ownership change \u2014 {tail} ({change_label})"
    subtitle = f"JETNET registry flip detected in nightly sweep"

    facts: list[tuple[str, str]] = []
    if owner_changed:
        facts.append(("Previous owner", old_owner))
        facts.append(("New owner",      new_owner))
    else:
        facts.append(("Owner", new_owner))
    if operator_changed:
        facts.append(("Previous operator", old_operator))
        facts.append(("New operator",      new_operator))
    elif new_operator != "—" and new_operator.lower() != new_owner.lower():
        facts.append(("Operator", new_operator))

    contact_added = False
    try:
        import jetnet_enrichment as _je
        for _rel, _val in _je.contact_facts(payload):
            facts.append((_rel, _val))
            contact_added = True
    except Exception:                                    # noqa: BLE001
        pass
    if not contact_added:
        contact_parts: list[str] = []
        if contact.get("phone"): contact_parts.append(str(contact["phone"]))
        if contact.get("email"): contact_parts.append(str(contact["email"]))
        loc = ", ".join(x for x in [contact.get("city"), contact.get("state")] if x)
        if loc: contact_parts.append(loc)
        if contact_parts:
            facts.append(("Contact", " \u00b7 ".join(contact_parts)))

    if recent_flights:
        legs = []
        for f in recent_flights[:3]:
            o = (f.get("origin_icao") or "?").upper()
            d = (f.get("dest_icao")   or "?").upper()
            when = (f.get("arrived_utc") or "")[:10]
            legs.append(f"{o}→{d} ({when})")
        if legs:
            facts.append(("Recent legs", " \u00b7 ".join(legs)))

    facts.append(("Signal", "Fresh owner is a warm reach-out — new decision-maker on a tail we already track."))

    tail_url = ""
    base = (config.DASHBOARD_URL or "").rstrip("/")
    if base and tail and tail != "—":
        tail_url = f"{base}/tail/{tail}"

    payload_card = _adaptive_card(
        title        = title,
        subtitle     = subtitle,
        facts        = facts,
        action_url   = tail_url or config.DASHBOARD_URL,
        action_label = f"Open {tail} dossier" if tail_url else "Open A320/737 Sightings dashboard",
    )
    return send(payload_card)


def notify_up_purchase_chain(chain: dict) -> bool:
    """
    Fire an "up-purchase signal" Adaptive Card when a Mustang (or other
    adjacent-tier tail) is caught making a fuel-stop chain. Distinct styling
    from the standard landing card so the sales team learns the difference:
    a chain trip on a Mustang means the operator is outgrowing the airplane
    and is a warm CJ / ATLAS conversation.
    """
    tail        = chain.get("tail_number") or "—"
    label       = chain.get("label") or chain.get("ac_type") or "—"
    operator    = chain.get("operator") or "—"
    origin      = chain.get("origin_icao") or "?"
    stop        = chain.get("fuel_stop_icao") or "?"
    dest        = chain.get("dest_icao") or "?"
    leg_a       = chain.get("leg_a_nm") or 0
    leg_b       = chain.get("leg_b_nm") or 0
    combined    = chain.get("combined_nm") or (leg_a + leg_b)
    ground_h    = chain.get("ground_h")
    baseline    = chain.get("baseline_nm") or 1000

    title       = f"\U0001F3AF  Up-purchase signal — {tail} ({label}) making chain trips"
    subtitle    = f"{origin} \u2192 {stop} \u2192 {dest} \u00b7 {leg_a} + {leg_b} = {combined} nm"

    facts = [
        ("Operator",   str(operator)),
        ("Type",       str(label)),
        ("Combined",   f"{combined} nm (baseline {baseline} nm)"),
        ("Fuel stop",  f"{stop} \u00b7 {ground_h}h ground" if ground_h is not None else stop),
        ("Signal",     "Operator likely outgrowing the airplane — warm CJ / ATLAS conversation."),
    ]

    payload = _adaptive_card(
        title        = title,
        subtitle     = subtitle,
        facts        = facts,
        action_url   = config.DASHBOARD_URL,
        action_label = "Open A320/737 Sightings dashboard",
    )
    return send(payload)


def send_test() -> bool:
    """Fire a sample card so a freshly configured webhook can be verified."""
    import watch_airports as _wa
    import watch_tails as _wt
    payload = _adaptive_card(
        title       = "\u2705 A320/737 Sightings — Teams integration is live",
        subtitle    = "This is a test message confirming the webhook works.",
        facts       = [
            ("Source",         "A320/737 Sightings"),
            ("Watched airports", ", ".join(_wa.get_watch_list()) or "(none yet)"),
            ("Watched tails",  ", ".join(_wt.get_watch_list()) or "(none yet)"),
        ],
        action_url  = config.DASHBOARD_URL,
        action_label= "Open dashboard",
    )
    return send(payload)


def notify_jetnet_live(info: dict) -> bool:
    """One-time card fired when the JETNET Marketplace entitlement goes live."""
    facts = [
        ("Service type",      str(info.get("subscriptiontier") or "—")),
        ("History available", "yes" if info.get("history_available") else "no"),
        ("Service frequency", str(info.get("servicefrequency") or "—")),
        ("Record cap",        str(info.get("ratelimit") or "—")),
    ]
    payload = _adaptive_card(
        title       = "\U0001F7E2 JETNET API is LIVE",
        subtitle    = "Entitlement flipped — owner enrichment, nightly ownership "
                      "sweep, and chat JETNET tools are now active.",
        facts       = facts,
        action_url  = config.DASHBOARD_URL,
        action_label= "Open dashboard",
    )
    return send(payload)


def _suggest_texts(p: dict) -> list[str]:
    """Compose the first two outreach text messages for a scored prospect.

    Picks the strongest ATLAS signal on the prospect (chain > hot range >
    WAT payload > OEI gradient > high-DA > generic) and returns two
    SMS-friendly messages: a hook keyed to the observed pattern, and a
    value-close with the ATLAS numbers + a soft ask.
    """
    tail   = p.get("tail_number") or "—"
    op     = (p.get("operator") or "").strip()
    op_par = f" ({op})" if op and op != "—" else ""
    label  = p.get("label") or p.get("ac_type") or "CJ"
    orig   = p.get("best_origin") or ""
    dest   = p.get("best_dest") or ""
    max_d  = p.get("max_distance_nm") or 0
    base   = p.get("baseline_nm") or 0
    atlas  = p.get("atlas_nm") or 0
    n_ch   = p.get("n_chains") or 0
    n_hot  = p.get("trips_hot") or 0
    wat_lb = p.get("wat_gain_lb") or 0
    wat_ap = p.get("wat_worst_icao") or ""
    oei_pc = p.get("oei_gradient_pct") or 0
    da_ct  = p.get("high_da_count") or 0
    da_ap  = p.get("high_da_airports") or []
    route  = f"{orig}→{dest}" if orig and dest else "a recent leg"

    if n_ch > 0:
        m1 = (f"Hey — spotted {tail}{op_par} making a fuel stop on {route} "
              f"({max_d:,} nm total). That's the exact leg ATLAS flies nonstop. "
              f"Worth 10 min?")
        m2 = (f"{label} baseline is {base:,} nm at MCT — ATLAS gets you to "
              f"{atlas:,} nm. Your {max_d:,}-nm mission lands nonstop. "
              f"Send you the 1-pager on {tail}?")
    elif n_hot > 0 and base and max_d > base:
        over = max_d - base
        m1 = (f"Hey — {tail}{op_par} flew {route} at {max_d:,} nm last month, "
              f"{over:,} nm past the {label} MCT baseline. ATLAS would've done "
              f"it nonstop.")
        m2 = (f"ATLAS pushes the {label} from {base:,} nm to {atlas:,} nm at "
              f"MCT — exactly the mission profile you're already flying. "
              f"Want the pre-call brief on {tail}?")
    elif wat_lb and wat_lb > 0:
        ap = wat_ap or "your high-DA base"
        m1 = (f"Hey — {tail}{op_par} has been operating out of {ap}. At summer "
              f"temps you're leaving payload on the ground. ATLAS gives you "
              f"about +{wat_lb:,} lb there.")
        m2 = (f"ATLAS raises MTOW on the {label} at {ap} by ~{wat_lb:,} lb — "
              f"that's real charter revenue. 10 min to walk you through the "
              f"numbers on {tail}?")
    elif oei_pc and oei_pc > 0:
        ap = wat_ap or "your hot-and-high base"
        m1 = (f"Hey — {tail}{op_par} is operating out of {ap} regularly. ATLAS "
              f"improves your OEI climb gradient there by +{oei_pc:.0f}%. Opens "
              f"up hot/high options you don't have today.")
        m2 = (f"OEI gradient on the {label} at {ap} goes from marginal to "
              f"comfortable with ATLAS (+{oei_pc:.0f}%). Worth a quick call "
              f"on {tail}?")
    elif da_ct > 0:
        sample = ", ".join(da_ap[:3]) if da_ap else "high-DA fields"
        m1 = (f"Hey — noticed {tail}{op_par} hitting {da_ct} high-DA airport"
              f"{'s' if da_ct != 1 else ''} lately ({sample}). ATLAS gives you "
              f"real takeoff-performance relief at those temps.")
        m2 = (f"The {label} + ATLAS story is exactly high-DA + hot temps. "
              f"Send you the pre-call brief on {tail}?")
    else:
        atlas_s = f"{atlas:,} nm" if atlas else "a longer nonstop"
        m1 = (f"Hey — flagged {tail}{op_par} as an ATLAS prospect based on the "
              f"last 30 days (e.g. {route} at {max_d:,} nm). Worth a quick call?")
        m2 = (f"ATLAS extends the {label} to {atlas_s} at MCT — 20–30% more "
              f"mission flexibility. Send you the 1-pager on {tail}?")
    return [m1, m2]


def notify_morning_prospects(prospects: list[dict], top_n: int = 5) -> bool:
    """
    Post the morning top-N prospects card with two suggested outreach text
    messages per prospect. Routes to the shared A320/737 Sightings Thread
    (``TEAMS_WEBHOOK_URL``) so the sales team sees the same list.
    """
    top = list(prospects)[:top_n]

    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("America/Los_Angeles")).strftime("%A, %b %d")
    except Exception:                                   # noqa: BLE001
        from datetime import datetime as _dt
        today = _dt.utcnow().strftime("%A, %b %d")

    title    = f"\U0001F305  Morning Prospects — Top {min(top_n, len(top)) or top_n}"
    subtitle = f"{today} \u00b7 first two outreach texts, ready to send"

    body: list[dict] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": _clean_text(title, 180), "wrap": True},
        {"type": "TextBlock", "spacing": "None",
         "text": _clean_text(subtitle, 500), "wrap": True, "isSubtle": True},
    ]

    if not top:
        body.append({"type": "TextBlock", "spacing": "Medium", "wrap": True,
                     "text": "_No scored prospects in the last 30 days yet._",
                     "isSubtle": True})
    else:
        for i, p in enumerate(top, 1):
            tail  = p.get("tail_number") or "—"
            op    = p.get("operator") or "—"
            label = p.get("label") or p.get("ac_type") or ""
            score = p.get("composite_score") or 0
            sigs  = p.get("signals") or []
            sig_s = " \u00b7 ".join(sigs[:3])

            header = f"**#{i}  {tail}** \u00b7 {label} \u00b7 {op} \u00b7 score **{score}**"
            if sig_s:
                header += f"  \n_{sig_s}_"

            texts = _suggest_texts(p)
            msg_block = "  \n".join(f"**Text {j+1}:** {t}"
                                    for j, t in enumerate(texts))

            body.append({
                "type":    "Container",
                "style":   "emphasis",
                "spacing": "Medium",
                "items": [
                    {"type": "TextBlock", "wrap": True,
                     "text": _clean_text(header, 800)},
                    {"type": "TextBlock", "wrap": True, "spacing": "Small",
                     "text": _clean_text(msg_block, 2000)},
                ],
            })

    prospects_url = (config.DASHBOARD_URL or "").rstrip("/") + "/prospects"
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": _CARD_SCHEMA,
                "type":    "AdaptiveCard",
                "version": _CARD_VERSION,
                "body":    body,
                "actions": [{
                    "type":  "Action.OpenUrl",
                    "title": "Open Prospects dashboard",
                    "url":   prospects_url,
                }],
            },
        }],
    }
    return send(payload)


def notify_daily_flight_plan(plan: dict, top_n: int = 5) -> bool:
    """
    Post the 8 AM "Daily Flight Plan" nudge — the morning sales ritual.
    `plan` is the dict from ``sales_plan.build_plan()``. Shows the team goal,
    the observation + coaching of the day, top trip clusters, and the top-N
    untouched prospects (rule-tagged, with one ready-to-send text each).
    Routes to the shared A320/737 Sightings Thread (``TEAMS_WEBHOOK_URL``).
    """
    try:
        import sales_plan
    except Exception:                                    # noqa: BLE001
        sales_plan = None

    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("America/Los_Angeles")).strftime("%A, %b %d")
    except Exception:                                    # noqa: BLE001
        from datetime import datetime as _dt
        today = _dt.utcnow().strftime("%A, %b %d")

    g = plan.get("goal") or {}
    call_list = list(plan.get("call_list") or [])[:top_n]
    clusters  = list(plan.get("clusters") or [])[:3]

    body: list[dict] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": _clean_text("\U0001F6EB  Daily Flight Plan", 120), "wrap": True},
        {"type": "TextBlock", "spacing": "None", "isSubtle": True, "wrap": True,
         "text": _clean_text(
             f"{today} \u00b7 team goal {g.get('count',0)}/{g.get('goal',10)} touches "
             f"\u00b7 {plan.get('total_pool',0)} scored prospects", 300)},
    ]

    if plan.get("observation"):
        body.append({"type": "TextBlock", "spacing": "Medium", "wrap": True,
                     "text": _clean_text("\U0001F4A1 " + plan["observation"], 500)})
    if plan.get("coaching"):
        body.append({"type": "TextBlock", "spacing": "Small", "wrap": True, "isSubtle": True,
                     "text": _clean_text("\U0001F3AF " + plan["coaching"], 400)})

    if clusters:
        lines = []
        for c in clusters:
            reps = ", ".join(r.split()[0] for r in (c.get("reps") or []))
            lines.append(f"**{c['icao']}** — {c['n_tails']} CJs \u00b7 {reps} ({c['base']})")
        body.append({"type": "TextBlock", "spacing": "Medium", "weight": "Bolder",
                     "text": "\U0001F5FA Trip suggestions", "wrap": True})
        body.append({"type": "TextBlock", "spacing": "None", "wrap": True,
                     "text": _clean_text("  \n".join(lines), 900)})

    body.append({"type": "TextBlock", "spacing": "Medium", "weight": "Bolder",
                 "text": "\U0001F4DE Today's call / text list", "wrap": True})

    if not call_list:
        body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                     "text": "_All caught up — check follow-ups on the dashboard._"})
    else:
        for i, p in enumerate(call_list, 1):
            tail  = p.get("tail_number") or "—"
            label = p.get("label") or ""
            owner = p.get("owner_name") or p.get("operator") or "—"
            rule  = p.get("rule") or "unknown"
            rlabel = sales_plan.rule_meta(rule)["label"] if sales_plan else rule
            phone = (p.get("phone") or "").strip()
            texts = _suggest_texts(p)
            t1 = texts[0] if texts else ""
            head = f"**#{i}  {tail}** \u00b7 {label} \u00b7 {owner} \u00b7 _{rlabel}_"
            if phone:
                head += f"  \n\U0001F4DE {phone}"
            body.append({
                "type": "Container", "style": "emphasis", "spacing": "Medium",
                "items": [
                    {"type": "TextBlock", "wrap": True, "text": _clean_text(head, 700)},
                    {"type": "TextBlock", "wrap": True, "spacing": "Small",
                     "text": _clean_text(f"**Text:** {t1}", 1200)},
                ],
            })

    plan_url = (config.DASHBOARD_URL or "").rstrip("/") + "/plan"
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": _CARD_SCHEMA,
                "type":    "AdaptiveCard",
                "version": _CARD_VERSION,
                "body":    body,
                "actions": [{
                    "type":  "Action.OpenUrl",
                    "title": "Open Daily Flight Plan",
                    "url":   plan_url,
                }],
            },
        }],
    }
    return send(payload)


def notify_daily_digest(digest: dict) -> bool:
    """
    Post the 4 PM daily usage digest card to the PRIVATE digest webhook.

    `digest` is the dict returned by `usage_tracker.get_daily_digest()`.
    Shows who used the dashboard today, how long, and who didn't show up.

    Routes to ``TEAMS_DIGEST_WEBHOOK_URL`` (a separate chat, e.g. a DM to
    yourself) — NOT to the shared A320/737 Sightings Thread. If that env var is
    unset the card is skipped entirely, so this internal report never leaks
    to the sales team.
    """
    if not config.TEAMS_DIGEST_ENABLED:
        log.info("Daily digest webhook not configured (TEAMS_DIGEST_WEBHOOK_URL); "
                 "skipping to avoid leaking usage report to shared chat")
        return False
    date      = digest.get("date", "")
    users     = digest.get("users") or []
    no_shows  = digest.get("no_shows") or []

    title    = f"\U0001F4CA  A320/737 Sightings \u2014 Daily Usage Report"
    subtitle = f"{date} \u00b7 {len(users)} of {digest.get('team_size', 0)} team members active today"

    body: list[dict] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": title, "wrap": True},
        {"type": "TextBlock", "spacing": "None",
         "text": subtitle, "wrap": True, "isSubtle": True},
    ]

    if users:
        lines: list[str] = []
        for u in users:
            mins  = u["total_minutes"]
            sess  = u["sessions"]
            dur_s = f"{mins} min" if mins < 60 else f"{mins // 60}h {mins % 60}m"
            sess_s = f"{sess} session{'s' if sess != 1 else ''}"
            top   = u["top_pages"]
            pages_s = ", ".join(p[0] for p in top[:3]) if top else ""
            line  = f"**{u['name']}** \u00b7 {dur_s} \u00b7 {sess_s}"
            if pages_s:
                line += f" \u00b7 _pages: {pages_s}_"
            line += f" \u00b7 last seen {u['last_seen']}"
            lines.append(line)
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Medium",
                     "text": "  \n".join(lines)})
    else:
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Medium",
                     "text": "_No team members visited today yet._",
                     "isSubtle": True})

    if no_shows:
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                     "text": f"\u274c Not seen today: {', '.join(no_shows)}",
                     "isSubtle": True})

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": _CARD_SCHEMA,
                "type":    "AdaptiveCard",
                "version": _CARD_VERSION,
                "body":    body,
                "actions": [{
                    "type":  "Action.OpenUrl",
                    "title": "Open A320/737 Sightings dashboard",
                    "url":   config.DASHBOARD_URL,
                }],
            },
        }],
    }
    return send(payload, webhook_url=config.TEAMS_DIGEST_WEBHOOK_URL)


def notify_hourly_summary(summary: dict, local_label: str = "") -> bool:
    """
    Post a compact hourly recap Adaptive Card to the configured Teams channel.

    `summary` is the dict returned by `database.get_hourly_summary()`.
    `local_label` is a short timestamp string (e.g. "10:00 AM PDT") used in
    the subtitle so readers can confirm the hour without parsing UTC.
    """
    sightings = summary.get("sightings") or []
    n_window  = summary.get("count_window", 0)
    n_today   = summary.get("count_today", 0)
    n_week    = summary.get("count_week", 0)
    hrs       = summary.get("window_hours", 1.0)

    title = f"\U0001F4CA  A320/737 Sightings — Recap"
    if local_label:
        subtitle = f"{local_label} \u00b7 {n_window} sighting(s) since last report"
        if hrs and hrs != 1.0:
            subtitle += f" ({hrs:g}h window)"
    else:
        subtitle = f"{n_window} sighting(s) since last report"
        if hrs and hrs != 1.0:
            subtitle += f" ({hrs:g}h window)"

    body: list[dict] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": title, "wrap": True},
        {"type": "TextBlock", "spacing": "None",
         "text": subtitle, "wrap": True, "isSubtle": True},
        {"type": "FactSet", "facts": [
            {"title": "This window", "value": str(n_window)},
            {"title": "Last 24h",    "value": str(n_today)},
            {"title": "Last 7d",     "value": str(n_week)},
        ]},
    ]

    if sightings:
        # Show up to 8 trips; collapse the rest into a "+N more" line.
        shown = sightings[:8]
        lines: list[str] = []
        for s in shown:
            tail   = s.get("tail") or "—"
            label  = s.get("label") or ""
            origin = s.get("origin") or "?"
            dest   = s.get("dest") or "?"
            dist   = s.get("distance_nm")
            dist_s = f" \u00b7 {dist:,} nm" if dist else ""
            local  = s.get("arrived_local") or ""
            local_s = f" \u00b7 _landed {local}_" if local else ""
            sig    = s.get("atlas_signal") or ""
            badge  = ""
            if s.get("watch_hit"):
                badge = "  \U0001F50D"   # 🔍 watched
            elif sig == "range_win":
                badge = "  \U0001F525"   # 🔥 ATLAS NON-STOP candidate
            elif sig == "beyond":
                badge = "  \u26A0\uFE0F"  # ⚠️ beyond ATLAS too
            lines.append(f"**{tail}** \u00b7 {label} \u00b7 {origin} \u2192 {dest}{dist_s}{local_s}{badge}")
        if len(sightings) > len(shown):
            lines.append(f"_… and {len(sightings) - len(shown)} more_")
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Medium",
                     "text": "  \n".join(lines)})
    else:
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Medium",
                     "text": "_No 525-family sightings since the last report._",
                     "isSubtle": True})

    # ── Mustang (up-purchase) section ──────────────────────────────────────
    # Adjacent-tier report. Every Mustang landing shown regardless of chain
    # status — full movement feed as sales-team signal (potential CJ upgrade
    # AND potential 510-winglet market data).
    m_sightings = summary.get("mustangs") or []
    m_window    = summary.get("mustangs_window", 0)
    m_today     = summary.get("mustangs_today", 0)
    m_week      = summary.get("mustangs_week", 0)
    if m_sightings or m_window or m_today or m_week:
        body.append({"type": "TextBlock", "size": "Medium", "weight": "Bolder",
                     "spacing": "Large", "separator": True,
                     "text": "\U0001F6E9\uFE0F  Mustang movement (adjacent-tier)",
                     "wrap": True})
        body.append({"type": "FactSet", "facts": [
            {"title": "This window", "value": str(m_window)},
            {"title": "Last 24h",    "value": str(m_today)},
            {"title": "Last 7d",     "value": str(m_week)},
        ]})
        if m_sightings:
            m_shown = m_sightings[:8]
            m_lines: list[str] = []
            for s in m_shown:
                tail   = s.get("tail") or "—"
                origin = s.get("origin") or "?"
                dest   = s.get("dest") or "?"
                dist   = s.get("distance_nm")
                dist_s = f" \u00b7 {dist:,} nm" if dist else ""
                local  = s.get("arrived_local") or ""
                local_s = f" \u00b7 _landed {local}_" if local else ""
                badge  = "  \U0001F3AF" if s.get("chain_hit") else ""   # 🎯 chain hit
                m_lines.append(f"**{tail}**{dist_s}{local_s} \u00b7 {origin} \u2192 {dest}{badge}")
            if len(m_sightings) > len(m_shown):
                m_lines.append(f"_… and {len(m_sightings) - len(m_shown)} more_")
            body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                         "text": "  \n".join(m_lines)})
        else:
            body.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                         "text": "_No Mustang movement in this window._",
                         "isSubtle": True})

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": _CARD_SCHEMA,
                "type":    "AdaptiveCard",
                "version": _CARD_VERSION,
                "body":    body,
                "actions": [{
                    "type":  "Action.OpenUrl",
                    "title": "Open A320/737 Sightings dashboard",
                    "url":   config.DASHBOARD_URL,
                }],
            },
        }],
    }
    return send(payload)
