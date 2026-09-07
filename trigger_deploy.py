#!/usr/bin/env python3
"""
trigger_deploy.py — push a deploy and confirm it landed.

Usage:
    python trigger_deploy.py                    # deploys to production
    python trigger_deploy.py http://localhost:8737  # deploys locally

What it does:
  1. Reads the current local git HEAD so it knows what commit to expect.
  2. Calls /webhook/deploy  (server does: git pull → docker restart).
  3. Polls /_version every 5 s for up to 90 s until the running commit matches.
  4. Prints ✓ DEPLOYED or clear manual-recovery instructions on timeout.
"""
import os
import subprocess
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://a320737sightings.voloaltro.tech"
SECRET   = os.getenv("DEPLOY_SECRET", "")

if not SECRET:
    print("ERROR: DEPLOY_SECRET not set in .env")
    sys.exit(1)

# ── 1. What commit are we expecting? ─────────────────────────────────────────
try:
    expected = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True
    ).strip()
except Exception:
    expected = None

print(f"Deploying {BASE_URL}  (expecting commit: {expected or 'unknown'}) ...")

# ── 2. Fire the deploy webhook ────────────────────────────────────────────────
try:
    r = requests.post(
        f"{BASE_URL}/webhook/deploy",
        headers={"X-Deploy-Secret": SECRET},
        timeout=15,
    )
    print(f"  → webhook: {r.status_code} {r.json()}")
except requests.RequestException as e:
    print(f"ERROR calling webhook: {e}")
    sys.exit(1)

# ── 3. Poll /_version until new commit is live ────────────────────────────────
TIMEOUT   = 90   # seconds
INTERVAL  = 5
deadline  = time.time() + TIMEOUT
confirmed = False

print(f"  → polling /_version", end="", flush=True)
while time.time() < deadline:
    time.sleep(INTERVAL)
    print(".", end="", flush=True)
    try:
        v = requests.get(f"{BASE_URL}/_version", timeout=5).json()
        live_commit = v.get("commit", "")
        if expected and live_commit == expected:
            confirmed = True
            break
        if not expected and live_commit:
            # Can't compare — just confirm server is responding after restart
            confirmed = True
            break
    except Exception:
        pass  # server mid-restart, keep polling

print()  # newline after dots

# ── 4. Report ─────────────────────────────────────────────────────────────────
if confirmed:
    v = requests.get(f"{BASE_URL}/_version", timeout=5).json()
    print(f"\n✓ DEPLOYED  commit={v['commit']}  started_at={v['started_at']}")
    print(f"  {BASE_URL}/insights")
else:
    print(f"\n✗ TIMEOUT — server did not restart within {TIMEOUT}s")
    print()
    print("  The code was git-pulled but the container did not restart.")
    print("  Fix it with ONE command in the Hetzner web console:")
    print()
    print("    docker restart a320737_sightings")
    print()
    print("  Then re-run this script to confirm.")
    sys.exit(1)

