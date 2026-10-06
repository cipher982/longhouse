#!/usr/bin/env bash
# Copy the source-owned canary bundle and user units to cube, then restart them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_HOME="$(ssh cube 'printf %s "$HOME"')"
[[ "$REMOTE_HOME" =~ ^/[A-Za-z0-9/._-]+$ ]] || {
  echo "could not resolve a safe cube home directory" >&2
  exit 2
}
REMOTE_APP="$REMOTE_HOME/.local/share/longhouse-canary"
REMOTE_CONFIG="$REMOTE_HOME/.config/longhouse-canary"
REMOTE_UNITS="$REMOTE_HOME/.config/systemd/user"

for path in \
  "$ROOT/scripts/canary/producer.py" \
  "$ROOT/scripts/canary/observer.py" \
  "$ROOT/scripts/canary/requirements.txt" \
  "$ROOT/scripts/lib/storage_v2_wire.py" \
  "$ROOT/scripts/canary/systemd/longhouse-canary-producer.service" \
  "$ROOT/scripts/canary/systemd/longhouse-canary-observer.service"; do
  [[ -f "$path" ]] || { echo "missing canary deploy input: $path" >&2; exit 2; }
done

# The device and canary secrets are provisioned out of band; this command never
# reads, copies, or logs their values.
ssh cube "test -f '$REMOTE_CONFIG/env' && test ! -L '$REMOTE_CONFIG/env'" || {
  echo "cube is missing ~/.config/longhouse-canary/env; provision it before deployment" >&2
  exit 2
}
ssh cube "command -v uv >/dev/null && command -v python3 >/dev/null" || {
  echo "cube needs uv and python3 before the canary can be installed" >&2
  exit 2
}
ssh cube "systemctl --user stop longhouse-canary-observer.service longhouse-canary-producer.service 2>/dev/null || true"
ssh cube "install -d -m 0755 '$REMOTE_APP' '$REMOTE_UNITS'"
scp \
  "$ROOT/scripts/canary/producer.py" \
  "$ROOT/scripts/canary/observer.py" \
  "$ROOT/scripts/canary/requirements.txt" \
  "$ROOT/scripts/lib/storage_v2_wire.py" \
  "cube:$REMOTE_APP/"
scp \
  "$ROOT/scripts/canary/systemd/longhouse-canary-producer.service" \
  "$ROOT/scripts/canary/systemd/longhouse-canary-observer.service" \
  "cube:$REMOTE_UNITS/"

ssh cube bash -s -- "$REMOTE_HOME" <<'REMOTE'
set -euo pipefail
home="$1"
app="$home/.local/share/longhouse-canary"
config="$home/.config/longhouse-canary/env"
units="$home/.config/systemd/user"

chmod 0600 "$config"
chmod 0644 \
  "$app/producer.py" \
  "$app/observer.py" \
  "$app/requirements.txt" \
  "$app/storage_v2_wire.py" \
  "$units/longhouse-canary-producer.service" \
  "$units/longhouse-canary-observer.service"
command -v uv >/dev/null || { echo "uv is required to install the canary runtime" >&2; exit 2; }
uv venv --allow-existing "$app/venv" --python python3
uv pip install --python "$app/venv/bin/python" -r "$app/requirements.txt"

# The old standalone SLA watcher is replaced by the authenticated Sauron job.
systemctl --user disable --now longhouse-canary-sla-watch.service 2>/dev/null || true
rm -f "$units/longhouse-canary-sla-watch.service" "$app/sla_watch.py"
if [[ "$(loginctl show-user "$USER" -p Linger --value)" != yes ]]; then
  sudo -n loginctl enable-linger "$USER"
fi
systemctl --user daemon-reload
systemctl --user enable --now longhouse-canary-producer.service
systemctl --user enable --now longhouse-canary-observer.service
REMOTE
