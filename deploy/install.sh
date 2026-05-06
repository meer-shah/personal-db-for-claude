#!/usr/bin/env bash
# PKP install helper. Run on a fresh server after cloning the repo.
# Usage: sudo ./deploy/install.sh
#
# What it does:
#   1. Substitutes <USER> and <APP_DIR> in the systemd unit templates.
#   2. Substitutes <DOMAIN> in the Caddyfile template (prompts if not set).
#   3. Installs systemd units to /etc/systemd/system/.
#   4. Installs Caddyfile to /etc/caddy/Caddyfile.
#   5. Creates /var/log/pkp/ and /var/pkp/ with correct ownership.
#
# It does NOT: install packages, create the venv, or start services.
# Follow README.md for the full sequence.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname)}"

if [[ -z "${PKP_DOMAIN:-}" ]]; then
    read -rp "Public domain (e.g. example.com or 1-2-3-4.nip.io): " PKP_DOMAIN
fi

echo "→ User:    $RUN_USER"
echo "→ App dir: $APP_DIR"
echo "→ Domain:  $PKP_DOMAIN"

# systemd units
for unit in pkp-mcp.service pkp-indexer.service; do
    sed -e "s|<USER>|$RUN_USER|g" -e "s|<APP_DIR>|$APP_DIR|g" \
        "$APP_DIR/deploy/systemd/$unit.template" \
        > "/etc/systemd/system/$unit"
done
cp "$APP_DIR/deploy/systemd/pkp-indexer.timer" /etc/systemd/system/

# Caddyfile
mkdir -p /etc/caddy
sed "s|<DOMAIN>|$PKP_DOMAIN|g" "$APP_DIR/deploy/Caddyfile.template" \
    > /etc/caddy/Caddyfile

# Runtime dirs
mkdir -p /var/log/pkp /var/pkp
chown "$RUN_USER:$RUN_USER" /var/log/pkp /var/pkp

systemctl daemon-reload

echo
echo "✓ Installed. Next steps (see README.md):"
echo "  1. Fill in .env"
echo "  2. Run: docker compose up -d"
echo "  3. Run: python onedrive.py   (one-time auth)"
echo "  4. sudo systemctl enable --now pkp-mcp.service pkp-indexer.timer caddy"
