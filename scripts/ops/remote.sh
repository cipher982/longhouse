#!/usr/bin/env bash
# Run heavy builds and tests on the `ci` VM instead of this laptop's heavy-build lock. `ci` is the 64-vCPU / 80 GiB
# CI KVM guest on cube (k3s + ARC); these runs use its host Docker, which CI does not, capped below the CI pods. The working tree (uncommitted edits included, .gitignore'd output excluded) is
# mirrored to a per-run directory in the guest, the command runs there, output streams back live, and
# named outputs are copied back to /tmp/agents/remote/<run>/. Same shape as bench.sh.
#
#   scripts/ops/remote.sh run [options] <command...>
#   scripts/ops/remote.sh status     slots in use, load, memory, disk of the guest
#   scripts/ops/remote.sh ssh        a shell in the guest
#
#   scripts/ops/remote.sh run make test                           # backend suite in the guest's Docker
#   scripts/ops/remote.sh run make test-engine-projection-failure # one engine lane
#   scripts/ops/remote.sh run --out artifacts/foo.json 'make foo'
#
# Options for `run`:
#   --out PATH     copy PATH (relative to the checkout root) back after the run (repeatable)
#   --env K=V      set K=V for the command (repeatable)
#   --timeout S    kill the run after S seconds (default $REMOTE_TIMEOUT, else 3600)
#   --keep         leave the run's guest directory in place (debugging; the guest GC removes it after 6 h)
# The command is one shell string (`run make test`, or quote it: `run 'a && b'`). In it, $REMOTE_OUT is a fresh directory whose contents come back to /tmp/agents/remote/<run>/out/.
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
# Rust: there is no `cargo` on the guest's PATH, on purpose. The toolchain is pinned in the test image and the guest
# keeps no second one to drift. `make test-engine` and `make test-engine-single TEST=mod::tests::name` use the image.
# For any other cargo command (a loop, a filter, nextest flags) run it in that image through test-isolation's ad-hoc
# mode; the image carries the dependencies warm, so only the engine crate builds (~30 s):
#   scripts/ops/remote.sh run "python3 scripts/qa/test-isolation.py --target test-engine-single --command bash -c \
#       'python3 scripts/build/generate_build_identity.py && python3 scripts/build/cargo.py exec -- nextest run \
#        --manifest-path engine/Cargo.toml --cargo-profile ci-test --bins -E \"test(pi_print)\"'"
#
# Up to REMOTE_SLOTS runs (default 3) execute at once; a fourth waits for a free slot. Each run has its own
# directory under /work/agents; the container image, Cargo dependencies and uv cache live in the image and
# are shared, so runs are warm. `ci` is reached over Tailscale ssh as user `builds`. Host setup, caps and
# cleanup: ~/git/me/domains/mytech/infrastructure/kube/agent-builds/provision.sh.
#
# Environment:
#   REMOTE_HOST      ssh host name of the build machine (default: ci.coin-castor.ts.net)
#   REMOTE_KEY       private key the build host's `builds` user trusts (default: ~/.ssh/remote_builds_ed25519)
#   REMOTE_SLOTS     concurrent runs (default: 3)
#   REMOTE_TIMEOUT   per-run limit in seconds (default: 3600)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="$(basename "$ROOT_DIR" | tr -c 'A-Za-z0-9._\n-' '_')"
HOST="${REMOTE_HOST:-ci.coin-castor.ts.net}"
KEY="${REMOTE_KEY:-$HOME/.ssh/remote_builds_ed25519}"
SLOTS="${REMOTE_SLOTS:-3}"
TIMEOUT="${REMOTE_TIMEOUT:-3600}"
OUT_ROOT=/tmp/agents/remote
SSH_CONFIG="$OUT_ROOT/ssh_config"

die() { echo "remote: $*" >&2; exit 1; }

# Both reach the guest inside one unquoted command string, and a non-numeric slot count makes its slot loop spin forever.
[[ "$SLOTS" =~ ^[1-9][0-9]*$ ]] || die "REMOTE_SLOTS must be a positive integer (got: $SLOTS)"
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "REMOTE_TIMEOUT must be seconds, digits only (got: $TIMEOUT)"

[[ -f "$KEY" ]] || die "no key at $KEY (the build host trusts ~/.ssh/remote_builds_ed25519; see me kube/agent-builds/provision.sh)"

write_ssh_config() {
  mkdir -p "$OUT_ROOT" && chmod 700 "$OUT_ROOT"
  # One config per invocation, under the alias `remote-build` so the rsync/ssh lines below stay host-agnostic.
  # The build host's key is pinned on first use in its own file, never mixed into known_hosts.
  cat > "$SSH_CONFIG.$$" <<EOF
Host remote-build
  HostName $HOST
  User builds
  IdentityFile $KEY
  IdentitiesOnly yes
  BatchMode yes
  HostKeyAlias remote-build-$HOST
  UserKnownHostsFile $HOME/.ssh/known_hosts_remote_build
  StrictHostKeyChecking accept-new
  ServerAliveInterval 20
  ServerAliveCountMax 6
  ControlMaster auto
  ControlPath $OUT_ROOT/cm-%C
  ControlPersist 5m
EOF
  mv "$SSH_CONFIG.$$" "$SSH_CONFIG"
}

g() { ssh -F "$SSH_CONFIG" remote-build "$@"; }

guest_probe() {
  if ! g true 2>/dev/null; then
    echo "remote: cannot reach $HOST as user builds. Check 'tailscale ping ${HOST%%.*}', then 'ssh drose@$HOST true'" >&2
    echo "        (`ci` is a VM on cube: 'ssh cube sudo virsh -c qemu:///system domstate ci' must say running)." >&2
    echo "        Setup: ~/git/me/domains/mytech/infrastructure/kube/agent-builds/provision.sh" >&2
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
  rsync -az --delete --exclude-from="$excludes" -e "ssh -F $SSH_CONFIG" "$ROOT_DIR/" "remote-build:/work/agents/$run/src/"
  rm -f "$excludes"
  g "flock /ci-cache/agents/mirror-$NAME.lock rsync -a --delete /work/agents/$run/src/ /ci-cache/agents/mirror/$NAME/ && touch /ci-cache/agents/mirror/$NAME"
  echo "remote: synced $NAME to the guest in $((SECONDS - started))s" >&2
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
setsid bash -c 'cat >/dev/null; echo "remote: client went away; stopping run $2" >&2
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
  waited=$((waited + 2)); [ $((waited % 30)) -eq 0 ] && echo "remote: all $slots slots busy, waiting (${waited}s)" >&2
  sleep 2
done
if [ ! -d .git ]; then
  git init -q -b main . && git add -A -f && git -c user.name=remote -c user.email=remote@localhost commit -q -m "remote sync $run"
fi
export REMOTE_OUT="$base/out" REMOTE_RUN="$run" REMOTE_SLOT="$slot"
eval "$extra_env"
# Sizing defaults come after --env so an override moves the container and its xdist workers together. The
# container's memory follows its CPU count when LONGHOUSE_TEST_MEMORY is unset (scripts/qa/test-isolation.py).
export LONGHOUSE_TEST_CPUS="${LONGHOUSE_TEST_CPUS:-8}"
export PYTEST_XDIST_WORKERS="${PYTEST_XDIST_WORKERS:-$LONGHOUSE_TEST_CPUS}"
echo "remote: run $run in slot $slot of $slots (waited ${waited}s); load $(cut -d' ' -f1-3 /proc/loadavg)" >&2
t0=$SECONDS
bash -c 'echo $$ > "$1"; shift; exec timeout --kill-after=30 "$@"' _ "$base/timeout.pid" "$timeout_s" bash -c "$cmd" 2>&1 | tee "$base/run.log"
code=${PIPESTATUS[0]}
echo "$code" > "$base/exit"
echo "remote: command exited $code after $((SECONDS - t0))s" >&2
exit "$code"
PROGRAM
}

cmd_run() {
  local outs=() envs=() keep=0 dotdot='(^|/)\.\.(/|$)'
  while (($#)); do
    case "$1" in
      --out) [[ $# -ge 2 && -n "$2" && "$2" != /* && ! "$2" =~ $dotdot ]] || die "--out needs a path inside the checkout (not empty, not absolute, no .. component)"; outs+=("$2"); shift 2 ;;
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
  rsync -a -e "ssh -F $SSH_CONFIG" "remote-build:/work/agents/$run/run.log" "remote-build:/work/agents/$run/exit" "remote-build:/work/agents/$run/out" "$dest/" 2>/dev/null || true
  local path
  for path in ${outs[@]+"${outs[@]}"}; do
    rsync -a --relative -e "ssh -F $SSH_CONFIG" "remote-build:/work/agents/$run/src/./$path" "$dest/" 2>/dev/null \
      || echo "remote: no $path in the run directory" >&2
  done
  if ((keep)); then
    echo "remote: kept /work/agents/$run in the guest" >&2
  else
    g "case '$run' in ''|*[!A-Za-z0-9._-]*) exit 1;; esac; rm -rf -- /work/agents/$run" || true
  fi
  echo "remote: $run exited $code; sync $((synced - started))s, run $((ran - synced))s, total $((SECONDS - started))s; outputs in $dest" >&2
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
  ssh) write_ssh_config; exec ssh -F "$SSH_CONFIG" -t remote-build ;;
  *) awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
