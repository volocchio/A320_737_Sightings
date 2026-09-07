"""
notifier.py — send HTML landing-alert email via Gmail SMTP
"""

import logging
import smtplib
import ssl
from datetime import datetime, timezone

import config
from sources import Sighting

log = logging.getLogger(__name__)

# Display name for each source
_SOURCE_LABELS = {
    "flightaware": "FlightAware",
    "opensky": "OpenSky Network",
    "adsbexchange": "ADS-B Exchange",
}

# Friendly names for C525 variants (CJ4/C25C intentionally excluded — not in ATLAS scope)
_TYPE_NAMES = {
    "A318": "Airbus A318",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A19N": "Airbus A319neo",
    "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",
    "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",
    "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",
}


def _format_utc(iso_str: str) -> str:
    """Convert ISO-8601 string to a readable format, or return '—' if blank."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_str


def _row(label: str, value: str) -> str:
    return (
        f'<tr>'
        f'<td style="padding:6px 12px 6px 0;color:#666;font-size:13px;'
        f'white-space:nowrap;vertical-align:top;">{label}</td>'
        f'<td style="padding:6px 0;font-size:13px;color:#111;">{value}</td>'
        f'</tr>'
    )


def _build_html(s: Sighting) -> str:
    ac_type = s.get("ac_type", "C525")
    type_label = _TYPE_NAMES.get(ac_type.upper(), ac_type)
    source_label = _SOURCE_LABELS.get(s.get("source", ""), s.get("source", ""))
    tail = s.get("tail_number") or "—"
    operator = s.get("operator") or "—"

    origin = s.get("origin_icao") or "—"
    if s.get("origin_name"):
        origin += f'  <span style="color:#666;">({s["origin_name"]})</span>'

    dest = s.get("dest_icao") or "—"
    if s.get("dest_name"):
        dest += f'  <span style="color:#666;">({s["dest_name"]})</span>'

    # Header landing label: "KSLC" or city name if available
    dest_icao_val = s.get("dest_icao") or ""
    dest_name_val = s.get("dest_name") or ""
    if dest_icao_val and dest_name_val:
        landing_label = f"{dest_icao_val} — {dest_name_val}"
    elif dest_icao_val:
        landing_label = dest_icao_val
    else:
        landing_label = "USA"

    departed = _format_utc(s.get("departed_utc", ""))
    arrived = _format_utc(s.get("arrived_utc", ""))
    dist = s.get("distance_nm")
    dist_str = f"{int(dist):,} nm" if dist else "—"
    tracking_url = s.get("tracking_url", "")
    track_link = (
        f'<a href="{tracking_url}" style="color:#1a73e8;">{tracking_url}</a>'
        if tracking_url else "—"
    )

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="580" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:8px;overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,.08);">

        <!-- Header bar -->
        <tr>
          <td style="background:#003087;padding:20px 28px;">
            <div style="font-size:11px;color:#99b8e8;letter-spacing:1px;
                        text-transform:uppercase;">A320/737 Sightings</div>
            <div style="font-size:22px;font-weight:bold;color:#fff;margin-top:4px;">
              {type_label} — Landed at {landing_label}
            </div>
            <div style="font-size:13px;color:#aac4e8;margin-top:4px;">
              Detected via {source_label}
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:24px 28px;">
            <table cellpadding="0" cellspacing="0" style="width:100%;
                   border-collapse:collapse;">
              {_row("Registration", f"<strong>{tail}</strong>")}
              {_row("Type", type_label)}
              {_row("Operator", operator)}
              {_row("Origin", origin)}
              {_row("Destination", dest)}
              {_row("Distance", f'<strong style="color:#b45309;">{dist_str}</strong>')}
              {_row("Departed", departed)}
              {_row("Arrived", arrived)}
              {_row("Track flight", track_link)}
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f9f9f9;padding:14px 28px;border-top:1px solid #eee;">
            <div style="font-size:11px;color:#999;">
              Powered by A320/737 Sightings &nbsp;·&nbsp;
              Tamarack Aerospace Group &nbsp;·&nbsp;
              {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    return html


def _build_plain(s: Sighting) -> str:
    ac_type = s.get("ac_type", "C525")
    type_label = _TYPE_NAMES.get(ac_type.upper(), ac_type)
    source_label = _SOURCE_LABELS.get(s.get("source", ""), s.get("source", ""))

    lines = [
        f"525 SIGHTINGS — {type_label} LANDED IN USA",
        f"Detected by: {source_label}",
        "",
        f"Registration : {s.get('tail_number') or '—'}",
        f"Type         : {type_label}",
        f"Operator     : {s.get('operator') or '—'}",
        f"Origin       : {s.get('origin_icao') or '—'} {s.get('origin_name', '')}",
        f"Destination  : {s.get('dest_icao') or '—'} {s.get('dest_name', '')}",
        f"Distance     : {f"{int(s['distance_nm']):,} nm" if s.get('distance_nm') else '—'}",
        f"Departed     : {_format_utc(s.get('departed_utc', ''))}",
        f"Arrived      : {_format_utc(s.get('arrived_utc', ''))}",
        f"Track        : {s.get('tracking_url') or '—'}",
    ]
    return "\n".join(lines)


def send_alert(sighting: Sighting) -> None:
    # Email notifications hard-disabled 2026-06-24 per user request.
    # All sighting alerts now go exclusively through teams_notifier (Adaptive Cards).
    # Do NOT re-enable without explicit instruction from Nick.
    return
