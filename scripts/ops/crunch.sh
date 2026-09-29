#!/usr/bin/env bash
# Run heavy builds and tests on the crunch VM instead of this laptop's heavy-build lock. The crunch
# VM is a 14-vCPU / 48 GiB KVM guest on the hosted Runtime Host machine (it also runs the private
# control-plane CI). The working tree (uncommitted edits included, .gitignore'd output excluded) is
# mirrored to a per-run directory in the guest, the command runs there, output streams back live, and
# named outputs are copied back to /tmp/agents/crunch/<run>/. Same shape as bench.sh.
#
#   scripts/ops/crunch.sh run [options] <command...>
#   scripts/ops/crunch.sh status     slots in use, load, memory, disk of the guest
#   scripts/ops/crunch.sh ssh        a shell in the guest
#
#   scripts/ops/crunch.sh run make test                           # backend suite in the guest's Docker
#   scripts/ops/crunch.sh run make test-engine-projection-failure # one engine lane
#   scripts/ops/crunch.sh run --out artifacts/foo.json 'make foo'
#
# Options for `run`:
#   --out PATH     copy PATH (relative to the checkout root) back after the run (repeatable)
#   --env K=V      set K=V for the command (repeatable)
#   --timeout S    kill the run after S seconds (default $CRUNCH_TIMEOUT, else 3600)
#   --keep         leave the run's guest directory in place (debugging; the guest GC removes it after 6 h)
# The command is one shell string (`run make test`, or quote it: `run 'a && b'`). In it, $CRUNCH_OUT is a fresh directory whose contents come back to /tmp/agents/crunch/<run>/out/.
# Every run also brings back run.log and exit there.
#
# What runs in the guest: it has Docker, make, git, python3 and rsync, and nothing else. `make test*`,
# `make validate*` and the other goals Makefile dispatches to test-isolation.py run in their disposable
# container exactly as on the laptop (the first run builds the test image, about 10 minutes, then it is
# cached), sized by LONGHOUSE_TEST_CPUS (default here: 8; memory follows it, and the container's xdist workers
# follow PYTEST_XDIST_WORKERS, default here: the CPU count). Pass overrides with --env: this shell's environment
# is not forwarded. Anything else must be self-contained (docker run ...). Never run a provider CLI here.
#
# The guest has no .git of yours: it builds a one-commit repository of the mirrored tree (uncommitted edits
# included), so a goal that stamps git identity or dirtiness (release, validate-build-identity) would record that
# synthetic commit. The test lanes never see it (their container gets no .git). Keep those goals on the laptop.
#
# Up to CRUNCH_SLOTS runs (default 3) execute at once; a fourth waits for a free slot. Each run has its own
# directory in the guest's tmpfs; the container image, Cargo dependencies and uv cache live in the image and
# are shared, so runs are warm. The guest is reached through the hosted machine's ssh alias, then AF_VSOCK
# (no network listener; the VM has no route from the tailnet): see control-plane
# docs/specs/build-compute-pipeline.md 6.2.8.
#
# Environment:
#   CRUNCH_VIA       ssh alias of the machine hosting the VM (default: zerg)
#   CRUNCH_KEY       private key the guest's `crunch` user trusts (default: ~/.ssh/crunch_vm_ed25519)
#   CRUNCH_SLOTS     concurrent runs (default: 3)
#   CRUNCH_TIMEOUT   per-run limit in seconds (default: 3600)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="$(basename "$ROOT_DIR" | tr -c 'A-Za-z0-9._\n-' '_')"
VIA="${CRUNCH_VIA:-zerg}"
KEY="${CRUNCH_KEY:-$HOME/.ssh/crunch_vm_ed25519}"
SLOTS="${CRUNCH_SLOTS:-3}"
TIMEOUT="${CRUNCH_TIMEOUT:-3600}"
CID=3
CONNECT=/usr/local/lib/crunch/vsock-connect.py
OUT_ROOT=/tmp/agents/crunch
SSH_CONFIG="$OUT_ROOT/ssh_config"

die() { echo "crunch: $*" >&2; exit 1; }

[[ -f "$KEY" ]] || die "no key at $KEY (the guest trusts ~/.ssh/crunch_vm_ed25519; see control-plane scripts/ops/crunch/)"

write_ssh_config() {
  mkdir -p "$OUT_ROOT" && chmod 700 "$OUT_ROOT"
  # One config per invocation: the ProxyCommand goes through the hosted machine (an ordinary `ssh zerg`,
  # so it uses this laptop's alias, key and multiplexing) and then AF_VSOCK to the guest's sshd.
  # The guest's host key is pinned on first use in its own file, never mixed into known_hosts.
  cat > "$SSH_CONFIG.$$" <<EOF
Host crunch
  HostName crunch
  User crunch
  IdentityFile $KEY
  IdentitiesOnly yes
  BatchMode yes
  ProxyCommand ssh -o BatchMode=yes $VIA python3 $CONNECT $CID 22
  HostKeyAlias crunch-guest
  UserKnownHostsFile $HOME/.ssh/known_hosts_crunch
  StrictHostKeyChecking accept-new
  ServerAliveInterval 20
  ServerAliveCountMax 6
  ControlMaster auto
  ControlPath $OUT_ROOT/cm-%C
  ControlPersist 5m
EOF
  mv "$SSH_CONFIG.$$" "$SSH_CONFIG"
}

g() { ssh -F "$SSH_CONFIG" crunch "$@"; }

guest_probe() {
  if ! g true 2>/dev/null; then
    echo "crunch: cannot reach the guest. Check 'ssh $VIA true', then 'ssh $VIA sudo virsh -c qemu:///system domstate crunch' (must say" >&2
    echo "        running; after a crash the host's breaker restarts it within seconds). The guest agent path works without the network:" >&2
    echo "        ssh $VIA sudo python3 /usr/local/lib/crunch/gexec.py 'uptime'" >&2
    g true || true
    exit 1
  fi
}

# Same recipe as bench.sh: git decides what is ignored, not rsync. `.git` is never sent: the guest builds a
# one-commit repository of the mirrored tree, which is all `git ls-files`-driven tooling needs. (`add -f`:
# a tracked file that also matches an ignore pattern, like server/tests_lite/*_legacy*, must stay in it.)
sync_tree() {
  local run="$1" excludes started=$SECONDS
  excludes="$(mktemp)"
  {
    printf '%s\n' /.git /.build/ /server/.venv/ /artifacts/ node_modules/ .DS_Store
    git -C "$ROOT_DIR" ls-files --others --ignored --exclude-standard --directory | sed 's|^|/|'
  } > "$excludes"
  # Seed the run directory from the last mirror (local copy in the guest, ~1 s), so rsync sends only the delta;
  # then publish the synced tree as the new mirror before the command can modify it.
  g "mkdir -p /ci-cache/agents/mirror/$NAME /work/agents/$run/src && flock -s /ci-cache/agents/mirror-$NAME.lock cp -a /ci-cache/agents/mirror/$NAME/. /work/agents/$run/src/ 2>/dev/null || true"
  rsync -az --delete --exclude-from="$excludes" -e "ssh -F $SSH_CONFIG" "$ROOT_DIR/" "crunch:/work/agents/$run/src/"
  rm -f "$excludes"
  g "flock /ci-cache/agents/mirror-$NAME.lock rsync -a --delete /work/agents/$run/src/ /ci-cache/agents/mirror/$NAME/ && touch /ci-cache/agents/mirror/$NAME"
  echo "crunch: synced $NAME to the guest in $((SECONDS - started))s" >&2
}

# The guest half of a run, as one bash program. It reads no stdin: stdin is the ssh channel, and EOF on it
# (this client killed, the network gone) is the signal to stop the run's process group. `timeout` bounds it
# either way, and a `make test*` container also carries its own deadline.
remote_program() {
  cat <<'PROGRAM'
set -uo pipefail
base="/work/agents/$run"
cd "$base/src" || exit 97
mkdir -p "$base/out" /work/agents/slots
exec 3<&0
# Its own session (setsid), so the kill below does not take the watchdog with it before the KILL escalation.
# `timeout` runs its command in a process group of its own, which this shell's group kill does not reach, so the
# run's `timeout` pid is recorded and signalled too (it forwards INT to its group; KILL goes to the whole group).
setsid bash -c 'cat >/dev/null; echo "crunch: client went away; stopping run $2" >&2
  t="$(cat "$3" 2>/dev/null)"
  kill -INT -- "-$1" 2>/dev/null; [ -n "$t" ] && kill -INT "$t" 2>/dev/null
  sleep 25
  kill -KILL -- "-$1" 2>/dev/null; [ -n "$t" ] && kill -KILL -- "-$t" 2>/dev/null' _ "$$" "$run" "$base/timeout.pid" <&3 &
watchdog=$!
trap 'kill "$watchdog" 2>/dev/null' EXIT
waited=0; slot=
while [ -z "$slot" ]; do
  for i in $(seq 1 "$slots"); do
    exec 9>"/work/agents/slots/$i.lock"
    if flock -n 9; then slot=$i; break; fi
  done
  [ -n "$slot" ] && break
  waited=$((waited + 2)); [ $((waited % 30)) -eq 0 ] && echo "crunch: all $slots slots busy, waiting (${waited}s)" >&2
  sleep 2
done
if [ ! -d .git ]; then
  git init -q -b main . && git add -A -f && git -c user.name=crunch -c user.email=crunch@localhost commit -q -m "crunch sync $run"
fi
export CRUNCH_OUT="$base/out" CRUNCH_RUN="$run" CRUNCH_SLOT="$slot"
eval "$extra_env"
# Sizing defaults come after --env so an override moves the container and its xdist workers together. The
# container's memory follows its CPU count when LONGHOUSE_TEST_MEMORY is unset (scripts/qa/test-isolation.py).
export LONGHOUSE_TEST_CPUS="${LONGHOUSE_TEST_CPUS:-8}"
export PYTEST_XDIST_WORKERS="${PYTEST_XDIST_WORKERS:-$LONGHOUSE_TEST_CPUS}"
echo "crunch: run $run in slot $slot of $slots (waited ${waited}s); load $(cut -d' ' -f1-3 /proc/loadavg)" >&2
t0=$SECONDS
bash -c 'echo $$ > "$1"; shift; exec timeout --kill-after=30 "$@"' _ "$base/timeout.pid" "$timeout_s" bash -c "$cmd" 2>&1 | tee "$base/run.log"
code=${PIPESTATUS[0]}
echo "$code" > "$base/exit"
echo "crunch: command exited $code after $((SECONDS - t0))s" >&2
exit "$code"
PROGRAM
}

cmd_run() {
  local outs=() envs=() keep=0
  while (($#)); do
    case "$1" in
      --out) [[ $# -ge 2 && "$2" != /* && "$2" != *..* ]] || die "--out needs a path inside the checkout (no absolute path, no ..)"; outs+=("$2"); shift 2 ;;
      --env) [[ $# -ge 2 && "$2" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || die "--env needs K=V"; envs+=("$2"); shift 2 ;;
      --timeout) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--timeout needs seconds"; TIMEOUT="$2"; shift 2 ;;
      --keep) keep=1; shift ;;
      --) shift; break ;;
      -*) die "unknown option $1" ;;
      *) break ;;
    esac
  done
  (($# > 0)) || die "run needs a command"
  local cmd="$*" started=$SECONDS run dest code=0 extra_env=""
  run="$NAME-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
  dest="$OUT_ROOT/$run"
  local e
  for e in ${envs[@]+"${envs[@]}"}; do extra_env+="export $(printf '%q' "$e"); "; done

  write_ssh_config
  guest_probe
  sync_tree "$run"
  local synced=$SECONDS
  # The guest stops the run when this ssh's stdin reaches EOF (client killed, network gone). A caller with no
  # stdin (a background job, CI) would read as EOF at once, so give ssh a FIFO this shell also holds open for
  # writing: EOF then arrives only when this process dies or ssh ends.
  local stdin_fifo="$OUT_ROOT/stdin-$run"
  mkfifo "$stdin_fifo"
  exec 7<>"$stdin_fifo"
  g "run=$(printf %q "$run") slots=$SLOTS timeout_s=$TIMEOUT cmd=$(printf %q "$cmd") extra_env=$(printf %q "$extra_env"); $(remote_program)" < "$stdin_fifo" || code=$?
  exec 7>&-
  rm -f "$stdin_fifo"
  local ran=$SECONDS

  mkdir -p "$dest"
  rsync -a -e "ssh -F $SSH_CONFIG" "crunch:/work/agents/$run/run.log" "crunch:/work/agents/$run/exit" "crunch:/work/agents/$run/out" "$dest/" 2>/dev/null || true
  local path
  for path in ${outs[@]+"${outs[@]}"}; do
    rsync -a --relative -e "ssh -F $SSH_CONFIG" "crunch:/work/agents/$run/src/./$path" "$dest/" 2>/dev/null \
      || echo "crunch: no $path in the run directory" >&2
  done
  if ((keep)); then
    echo "crunch: kept /work/agents/$run in the guest" >&2
  else
    g "case '$run' in ''|*[!A-Za-z0-9._-]*) exit 1;; esac; rm -rf -- /work/agents/$run" || true
  fi
  echo "crunch: $run exited $code; sync $((synced - started))s, run $((ran - synced))s, total $((SECONDS - started))s; outputs in $dest" >&2
  return "$code"
}

cmd_status() {
  write_ssh_config
  guest_probe
  g 'echo "== load"; cat /proc/loadavg; echo "== memory (GiB)"; free -g | head -2; echo "== disk"; df -h / /work | tail -n +1
     echo "== runs"; for f in /work/agents/slots/*.lock; do [ -e "$f" ] || continue; flock -n "$f" true && echo "slot $(basename "$f" .lock): free" || echo "slot $(basename "$f" .lock): BUSY"; done
     ls -1 /work/agents 2>/dev/null | grep -v slots | sed "s/^/run dir: /"
     echo "== docker"; docker ps --format "{{.Names}}\t{{.Status}}" | head -20; docker images --format "{{.Repository}}:{{.Tag}}\t{{.Size}}" | head -5'
}

case "${1:-}" in
  run) shift; cmd_run "$@" ;;
  status) cmd_status ;;
  ssh) write_ssh_config; exec ssh -F "$SSH_CONFIG" -t crunch ;;
  *) awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
