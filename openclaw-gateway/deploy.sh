#!/usr/bin/env bash
# ---------------------------------------------------------------
# OpenClaw Gateway — EC2 Deployment Script
#
# Run this ON the EC2 instance after copying the gateway files.
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - Ubuntu 22.04+ on EC2 (Amazon Linux works with minor tweaks)
#   - Python 3.11+
#   - openssl installed
# ---------------------------------------------------------------
set -euo pipefail

INSTALL_DIR="/home/ubuntu/openclaw-gateway"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "  OpenClaw Gateway — Deployment"
echo "============================================"
echo ""

# --- 1. System dependencies ---
echo "[1/6] Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip openssl

# --- 2. Copy files to install dir ---
echo "[2/6] Copying gateway files to ${INSTALL_DIR}…"
mkdir -p "${INSTALL_DIR}"
cp -v "${SCRIPT_DIR}/gateway_config.py" "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/gateway.py"        "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/api.py"            "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/cli.py"            "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/main.py"           "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/requirements.txt"  "${INSTALL_DIR}/"
cp -v "${SCRIPT_DIR}/setup_tls.sh"      "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/setup_tls.sh"

# --- 3. Python venv ---
echo "[3/6] Setting up Python virtual environment…"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

# --- 4. TLS certificates ---
echo "[4/6] Generating TLS certificates…"
cd "${INSTALL_DIR}"
./setup_tls.sh

# --- 5. Generate auth token if not set ---
echo "[5/6] Auth token setup…"
if [ -z "${OPENCLAW_AUTH_TOKEN:-}" ]; then
    TOKEN=$("${INSTALL_DIR}/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
    echo ""
    echo "  ┌──────────────────────────────────────────────────┐"
    echo "  │  GENERATED AUTH TOKEN (save this!)               │"
    echo "  │                                                  │"
    echo "  │  ${TOKEN}"
    echo "  │                                                  │"
    echo "  │  Set this SAME token on your laptop agent:       │"
    echo "  │  \$env:OPENCLAW_AUTH_TOKEN = \"${TOKEN}\" "
    echo "  └──────────────────────────────────────────────────┘"
    echo ""

    # Write it to the systemd unit.
    sed -i "s|REPLACE_ME_WITH_REAL_TOKEN|${TOKEN}|" "${SCRIPT_DIR}/openclaw-gateway.service"
else
    TOKEN="${OPENCLAW_AUTH_TOKEN}"
    echo "  Using existing OPENCLAW_AUTH_TOKEN from environment."
    sed -i "s|REPLACE_ME_WITH_REAL_TOKEN|${TOKEN}|" "${SCRIPT_DIR}/openclaw-gateway.service"
fi

# --- 6. Install systemd service ---
echo "[6/6] Installing systemd service…"
sudo cp "${SCRIPT_DIR}/openclaw-gateway.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway
sudo systemctl start openclaw-gateway

echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
echo ""
echo "  Service status:"
sudo systemctl status openclaw-gateway --no-pager -l
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status openclaw-gateway"
echo "    sudo systemctl restart openclaw-gateway"
echo "    sudo journalctl -u openclaw-gateway -f"
echo "    python3 ${INSTALL_DIR}/cli.py"
echo ""
echo "  Your laptop agent should connect to:"
echo "    wss://100.50.2.232:8765/agent/ws"
echo ""
echo "  Don't forget to open port 8765 in your EC2 Security Group!"
echo ""
