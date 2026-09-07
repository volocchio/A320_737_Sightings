#!/usr/bin/env bash
# deploy.sh — push and restart A320/737 Sightings on the VPS
#
# Usage:
#   bash deploy.sh                   # uses voloaltro.tech (requires SSH bypass Cloudflare)
#   bash deploy.sh 95.179.x.x       # use direct VPS IP (recommended)
#
# Preferred: just push to GitHub — GitHub Actions will auto-deploy via .github/workflows/deploy.yml

set -e

VPS="root@${1:-voloaltro.tech}"
REMOTE_DIR="/opt/a320737_sightings"

echo "==> Syncing repo to VPS ($VPS)..."
rsync -az --exclude='.env' --exclude='sightings.db' --exclude='__pycache__' \
  ./ "$VPS:$REMOTE_DIR/"

echo "==> Copying .env to VPS (first deploy only — skipped if already exists)..."
ssh "$VPS" "test -f $REMOTE_DIR/.env || echo 'REMINDER: copy .env manually to $REMOTE_DIR/.env'"

echo "==> Building and restarting container..."
ssh "$VPS" "cd $REMOTE_DIR && docker compose up -d --build"

echo "==> Tailing logs (Ctrl+C to exit)..."
ssh "$VPS" "docker logs -f a320737_sightings"
