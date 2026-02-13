#!/usr/bin/env bash
# ---------------------------------------------------------------
# OpenClaw Gateway — Generate a self-signed TLS certificate
#
# Usage:
#   chmod +x setup_tls.sh
#   ./setup_tls.sh
#
# Produces:
#   certs/server.crt   (self-signed certificate, valid 365 days)
#   certs/server.key   (private key)
#
# For production, replace these with Let's Encrypt certs via
# certbot or use an AWS ALB with ACM certificates.
# ---------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="${SCRIPT_DIR}/certs"

mkdir -p "${CERT_DIR}"

echo "Generating self-signed TLS certificate…"

openssl req -x509 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/server.key" \
    -out "${CERT_DIR}/server.crt" \
    -days 365 \
    -nodes \
    -subj "/CN=openclaw-gateway" \
    -addext "subjectAltName=IP:100.50.2.232,DNS:ec2-100-50-2-232.compute-1.amazonaws.com"

chmod 600 "${CERT_DIR}/server.key"
chmod 644 "${CERT_DIR}/server.crt"

echo ""
echo "Done.  Certificate written to:"
echo "  ${CERT_DIR}/server.crt"
echo "  ${CERT_DIR}/server.key"
echo ""
echo "The cert includes SAN entries for your Elastic IP and public DNS."
