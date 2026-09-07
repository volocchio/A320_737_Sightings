#!/usr/bin/env bash
# setup_deploy_key.sh — one-time setup for GitHub Actions auto-deploy
#
# Run this from your LOCAL machine (WSL or Git Bash):
#   bash setup_deploy_key.sh YOUR_VPS_IP
#
# It will:
#   1. Generate a dedicated deploy SSH key pair
#   2. Install the public key on the VPS
#   3. Print the private key for you to paste into GitHub Secrets

set -e

VPS_IP="${1:?Usage: bash setup_deploy_key.sh YOUR_VPS_IP}"
KEY_FILE="$HOME/.ssh/525_deploy_key"

echo "==> Generating deploy SSH key..."
ssh-keygen -t ed25519 -C "github-deploy-a320737sightings" -f "$KEY_FILE" -N ""

echo "==> Installing public key on VPS ($VPS_IP)..."
ssh-copy-id -i "$KEY_FILE.pub" "root@$VPS_IP"

echo ""
echo "==> Done! Now add these two secrets to GitHub:"
echo "    github.com/volocchio/A320_737_Sightings → Settings → Secrets → Actions"
echo ""
echo "Secret name:  VPS_HOST"
echo "Secret value: $VPS_IP"
echo ""
echo "Secret name:  VPS_SSH_KEY"
echo "Secret value: (contents of $KEY_FILE below)"
echo "------------------------------------------------------------"
cat "$KEY_FILE"
echo "------------------------------------------------------------"
echo ""
echo "After adding both secrets, every 'git push' will auto-deploy."
