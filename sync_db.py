#!/usr/bin/env python3
"""
sync_db.py — push all local sightings to the server's DB.
Run once after setting up the server to backfill history.

Usage:
    python sync_db.py
"""
import os, sqlite3, json, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

LOCAL_DB  = Path(__file__).parent / "sightings.db"
SERVER    = "https://a320737sightings.voloaltro.tech"
SECRET    = os.getenv("DEPLOY_SECRET", "")

if not SECRET:
    print("ERROR: DEPLOY_SECRET not set in .env"); exit(1)

conn = sqlite3.connect(LOCAL_DB)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """SELECT source, flight_id, tail_number, ac_type,
              origin_icao, origin_name, dest_icao, dest_name,
              departed_utc, arrived_utc, operator, tracking_url,
              distance_nm, notified_at
       FROM sightings ORDER BY id"""
).fetchall()
conn.close()

payload = [dict(r) for r in rows]
print(f"Pushing {len(payload)} sightings to {SERVER} ...")

r = requests.post(
    f"{SERVER}/admin/import-sightings",
    headers={"X-Deploy-Secret": SECRET, "Content-Type": "application/json"},
    data=json.dumps(payload),
    timeout=30,
)
print(f"Response: {r.status_code} — {r.text[:200]}")
