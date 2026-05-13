#!/usr/bin/env bash
# Install the Longhouse canary harness on cube as systemd user services.
#
# Idempotent — re-run after code/env changes; services will be restarted.
#
# Prerequisites (once):
#   1. SSH access to cube.
#   2. On the david010 Longhouse server, set LONGHOUSE_CANARY_TOKEN to a
#      shared secret and redeploy.
#   3. Obtain an agents device token from the Longhouse admin (via the
#      same admin surface you use for normal machine registration).
#
# Env inputs (passed via `ssh cube env FOO=bar ... bash install_cube.sh`):
#   LONGHOUSE_CANARY_URL     — e.g. https://david010.longhouse.ai
#   LONGHOUSE_AGENTS_TOKEN   — agents device token for ingest
#   LONGHOUSE_CANARY_TOKEN   — shared secret set on the server
#
# Optional:
#   LONGHOUSE_CANARY_MACHINE — override the hostname label (default: `cube`)
#   LONGHOUSE_SLA_WEBHOOK    — ntfy/slack webhook for SLA breaches

set -euo pipefail

: "${LONGHOUSE_CANARY_URL:?missing LONGHOUSE_CANARY_URL}"
: "${LONGHOUSE_AGENTS_TOKEN:?missing LONGHOUSE_AGENTS_TOKEN}"
: "${LONGHOUSE_CANARY_TOKEN:?missing LONGHOUSE_CANARY_TOKEN}"

INSTALL_ROOT="$HOME/.local/share/longhouse-canary"
ENV_FILE="$HOME/.config/longhouse-canary/env"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "==> Installing canary into $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT" "$(dirname "$ENV_FILE")" "$SYSTEMD_USER_DIR"

echo "==> Writing env file at $ENV_FILE"
umask 077
cat > "$ENV_FILE" <<EOF
LONGHOUSE_CANARY_URL=$LONGHOUSE_CANARY_URL
LONGHOUSE_AGENTS_TOKEN=$LONGHOUSE_AGENTS_TOKEN
LONGHOUSE_CANARY_TOKEN=$LONGHOUSE_CANARY_TOKEN
LONGHOUSE_CANARY_MACHINE=${LONGHOUSE_CANARY_MACHINE:-cube}
LONGHOUSE_CANARY_INTERVAL_S=${LONGHOUSE_CANARY_INTERVAL_S:-30}
LONGHOUSE_SLA_WEBHOOK=${LONGHOUSE_SLA_WEBHOOK:-}
LONGHOUSE_SLA_CHECK_INTERVAL_S=${LONGHOUSE_SLA_CHECK_INTERVAL_S:-60}
LONGHOUSE_SLA_P95_MS=${LONGHOUSE_SLA_P95_MS:-300}
PYTHONUNBUFFERED=1
EOF
umask 022

echo "==> Setting up venv in $INSTALL_ROOT/venv"
if [ ! -d "$INSTALL_ROOT/venv" ]; then
    python3 -m venv "$INSTALL_ROOT/venv"
fi
"$INSTALL_ROOT/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$INSTALL_ROOT/venv/bin/pip" install --quiet httpx[http2] >/dev/null

echo "==> Writing systemd units to $SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/longhouse-canary-producer.service" <<EOF
[Unit]
Description=Longhouse canary producer (synthetic runtime observations)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_ROOT/venv/bin/python $INSTALL_ROOT/producer.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_USER_DIR/longhouse-canary-observer.service" <<EOF
[Unit]
Description=Longhouse canary observer (SSE wake latency)
After=longhouse-canary-producer.service
Wants=longhouse-canary-producer.service

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_ROOT/venv/bin/python $INSTALL_ROOT/observer.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_USER_DIR/longhouse-canary-sla-watch.service" <<EOF
[Unit]
Description=Longhouse canary SLA watch (webhook on p95 breach)
After=longhouse-canary-observer.service

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_ROOT/venv/bin/python $INSTALL_ROOT/sla_watch.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

echo "==> Enabling linger so services survive logout"
# loginctl enable-linger requires no special privs for own user on modern systemd
loginctl enable-linger "$USER" 2>/dev/null || true

echo "==> Reloading systemd"
systemctl --user daemon-reload

echo "==> Enabling + starting services"
systemctl --user enable --now longhouse-canary-producer.service
systemctl --user enable --now longhouse-canary-observer.service
if [ -n "${LONGHOUSE_SLA_WEBHOOK:-}" ]; then
    systemctl --user enable --now longhouse-canary-sla-watch.service
else
    echo "    (skipping sla-watch: no LONGHOUSE_SLA_WEBHOOK set)"
fi

echo
echo "==> Done. Check logs with:"
echo "     journalctl --user -u longhouse-canary-producer -f"
echo "     journalctl --user -u longhouse-canary-observer -f"
echo
echo "==> Verify on server:"
echo "     curl -sS $LONGHOUSE_CANARY_URL/metrics | grep canary_seq_last_seen"
