"""
sources/__init__.py

Shared data model for a landing sighting.
Each source returns a list of these dicts.
"""

from typing import TypedDict


class Sighting(TypedDict, total=False):
    source: str          # "flightaware" | "opensky" | "adsbexchange"
    flight_id: str       # unique ID within that source
    tail_number: str     # N-number / registration
    ac_type: str         # ICAO type code e.g. C525
    origin_icao: str
    origin_name: str
    dest_icao: str
    dest_name: str
    departed_utc: str    # ISO-8601
    arrived_utc: str     # ISO-8601
    operator: str
    tracking_url: str
