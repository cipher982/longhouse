#!/usr/bin/env bash
# Host-normalized transcripts, Phase 3 check on a hosted QA Runtime Host.
#
# Ships every engine transcript fixture through the real shipper to a hosted
# QA target under a throwaway device token, waits for that host's render
# shadow to compare each envelope (the host rebuilds it from stored raw plus
# the capture witness and compares it with the shipped render), prints the
# outcomes for exactly those sessions, then deletes the sessions and revokes
# the token. Exit 0 only when every fixture that shipped events produced a
# session and every stored envelope of those sessions matched: an envelope the
# host could not rebuild (no witness, raw only, too large) fails the proof.
# Needs bash 4.4 or later (macOS: Homebrew bash).
#
#   SMOKE_RUNTIME_TOKEN=<QA access token> scripts/qa/render-shadow-fixture-proof.sh
#
# Never point it at david010: QA_INSTANCE_SUBDOMAIN defaults to hosted-qa-a
# and the script refuses the dogfood tenant.
set -euo pipefail
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  echo "render-shadow-fixture-proof needs bash 4.4 or later (this is ${BASH_VERSION})" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT_DIR/scripts/lib/hosted-instance.sh"

SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-hosted-qa-a}"
if [[ "$SUBDOMAIN" == "david010" ]]; then
  echo "render-shadow-fixture-proof refuses david010: it carries no test traffic" >&2
  exit 2
fi
API_URL="https://${SUBDOMAIN}.longhouse.ai"
CONTAINER="longhouse-${SUBDOMAIN}"
SSH_TARGET="${RENDER_SHADOW_PROOF_SSH_TARGET:-cube}"
# Longer than the shadow's 30-minute session cooldown, so a session compared
# before its last envelope landed is compared again inside the wait.
WAIT_SECONDS="${RENDER_SHADOW_PROOF_WAIT_SECONDS:-2700}"
: "${SMOKE_RUNTIME_TOKEN:?Set SMOKE_RUNTIME_TOKEN to the QA host access token}"

SCRATCH="$(mktemp -d /tmp/agents/render-shadow-proof.XXXXXX)"
DEVICE_ID="render-shadow-proof-${RANDOM}${RANDOM}"
UUID_MAP="$SCRATCH/uuid-map.json"
TOKEN_ID=""
DEVICE_TOKEN=""
SESSION_IDS=()

# The agents session list does not return sessions from a machine with no
# heartbeat, so read this machine's sessions from the host catalog, the same
# place the outcomes come from.
list_sessions() {
  # The device id expands here, on this side, by design.
  # shellcheck disable=SC2029
  ssh "$SSH_TARGET" "docker exec -i ${CONTAINER} python3 - ${DEVICE_ID}" <<'PY'
import sqlite3, sys
db = sqlite3.connect("file:/data/longhouse-live.db?mode=ro", uri=True)
rows = db.execute(
    "select distinct session_id from raw_objects where machine_id = ? and retired_at is null", (sys.argv[1],)
)
print("\n".join(row[0] for row in rows))
PY
}
cleanup() {
  local status=$?
  # Re-list from the host: a run that stops partway through shipping has
  # sessions it never got to record.
  if [[ -n "$TOKEN_ID" ]]; then
    mapfile -t SESSION_IDS < <(list_sessions || true)
  fi
  for session_id in "${SESSION_IDS[@]}"; do
    [[ -n "$session_id" ]] || continue
    curl -sS --fail -o /dev/null --max-time 60 -X DELETE -H "Authorization: Bearer ${SMOKE_RUNTIME_TOKEN}" \
      "${API_URL}/api/user-data/sessions/${session_id}" || echo "Warning: could not delete session ${session_id}" >&2
  done
  if [[ -n "$TOKEN_ID" ]]; then
    lh_hosted_revoke_device_token "$SMOKE_RUNTIME_TOKEN" "$TOKEN_ID" "$API_URL" >/dev/null 2>&1 \
      || echo "Warning: could not revoke device token ${TOKEN_ID}" >&2
  fi
  rm -r "$SCRATCH"
  exit "$status"
}
trap cleanup EXIT

IFS=$'\t' read -r TOKEN_ID DEVICE_TOKEN <<<"$(lh_hosted_create_device_token "$SMOKE_RUNTIME_TOKEN" "$API_URL" "$DEVICE_ID")"

python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- build --manifest-path "$ROOT_DIR/engine/Cargo.toml" \
  --profile "${CARGO_PROFILE:-release}" --bin longhouse-engine
ENGINE="$(python3 "$ROOT_DIR/scripts/build/cargo.py" artifact --profile "${CARGO_PROFILE:-release}" --bin longhouse-engine)"

# The same fixture trees the engine's rebuild test ships (rebuild.rs fixture_sets).
FIXTURE_SETS=(
  "golden/claude claude" "adversarial/claude claude" "workflows/claude claude"
  "golden/codex codex" "adversarial/codex codex" "codex codex"
  "golden/cursor cursor"
  "golden/antigravity antigravity" "golden/antigravity_legacy_json antigravity"
  "adversarial/antigravity_legacy_json antigravity"
  "golden/pi pi" "golden/omp omp"
)
shipped=0
ship_failures=0
with_events=0
for entry in "${FIXTURE_SETS[@]}"; do
  read -r tree provider <<<"$entry"
  source_root="$ROOT_DIR/engine/tests/fixtures/$tree"
  [[ -d "$source_root" ]] || continue
  case_root="$SCRATCH/$(echo "$tree" | tr / _)"
  mkdir -p "$case_root"
  # Copy the tree whole so sidecars sit where the shipper looks.
  cp -R "$source_root/." "$case_root/src"
  # Fixture ids are fixed in their content, and a session deleted by an
  # earlier run stays tombstoned, so every run maps each UUID to a fresh one,
  # consistently across contents and file names. Host and laptop still read
  # the same bytes, so the comparison is unchanged.
  python3 - "$case_root/src" "$UUID_MAP" <<'PY'
import json, os, re, sys, uuid
root, map_path = sys.argv[1], sys.argv[2]
mapping = json.load(open(map_path)) if os.path.exists(map_path) else {}
pattern = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
def fresh(match):
    key = match.group(0).lower()
    mapping.setdefault(key, str(uuid.uuid4()))
    return mapping[key]
# Media deleted with an earlier run's session can never become present again
# (a deliberate deletion fence), so every inline image gets this run's nonce
# appended. The same image keeps one hash within a run.
import base64
nonce = mapping.setdefault("__media_nonce__", base64.b64encode(os.urandom(4)).decode())
def is_image(node):
    kinds = " ".join(str(node.get(key, "")) for key in ("type", "mimeType", "mime_type", "media_type"))
    return "image" in kinds or node.get("type") == "base64"
def walk(node):
    changed = False
    if isinstance(node, dict):
        if isinstance(node.get("data"), str) and is_image(node):
            try:
                raw = base64.b64decode(node["data"], validate=True)
            except ValueError:
                raw = None
            if raw is not None:
                node["data"] = base64.b64encode(raw + base64.b64decode(nonce)).decode()
                changed = True
        for value in node.values():
            changed |= walk(value)
    elif isinstance(node, list):
        for value in node:
            changed |= walk(value)
    return changed
def unique_media(text):
    lines = []
    for line in text.split("\n"):
        try:
            value = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        lines.append(json.dumps(value, separators=(",", ":")) if walk(value) else line)
    return "\n".join(lines)
for directory, _dirs, files in os.walk(root, topdown=False):
    for name in files:
        path = os.path.join(directory, name)
        try:
            text = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            continue
        open(path, "w", encoding="utf-8").write(unique_media(pattern.sub(fresh, text)))
        renamed = pattern.sub(fresh, name)
        if renamed != name:
            os.rename(path, os.path.join(directory, renamed))
json.dump(mapping, open(map_path, "w"))
PY
  # A copy is brand new, and the shipper holds a transcript modified in the
  # last few seconds (Antigravity's binding race), so backdate the copies.
  find "$case_root/src" -type f -exec touch -t 202601010000 {} +
  while IFS= read -r -d '' transcript; do
    # A fresh session id per run: fixtures without a UUID would otherwise land
    # on an earlier run's deleted session and ship nothing.
    HOME="$case_root/home" "$ENGINE" ship --file "$transcript" --provider "$provider" \
      --session-id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
      --url "$API_URL" --token "$DEVICE_TOKEN" --db "$case_root/state-$shipped.db" \
      --machine-name "$DEVICE_ID" --json >"$case_root/ship-$shipped.json" 2>"$case_root/ship-$shipped.err" \
      || { echo "ship failed: $tree/$(basename "$transcript")" >&2; cat "$case_root/ship-$shipped.err" >&2; ship_failures=$((ship_failures + 1)); }
    # The JSON result is the last object on stdout, after any progress lines.
    events="$(python3 -c '
import json, sys
text = open(sys.argv[1]).read()
result = json.loads(text[text.index("{"):])
print(f"  {sys.argv[2]:60} events={result.get(\"events_shipped\")} status={result.get(\"status\")}", file=sys.stderr)
print(result.get("events_shipped") or 0)
' "$case_root/ship-$shipped.json" "$tree/$(basename "$transcript")" || echo "?")"
    if [[ "$events" == "?" ]]; then
      echo "  $tree/$(basename "$transcript"): no ship result" >&2
      ship_failures=$((ship_failures + 1))
    elif [[ "$events" -gt 0 ]]; then
      with_events=$((with_events + 1))
    fi
    shipped=$((shipped + 1))
  done < <(find "$case_root/src" -type f \( -name '*.jsonl' -o -name '*.json' \) ! -name '*.expected.*' ! -name '*.meta.json' -print0 | sort -z)
done
echo "shipped ${shipped} fixture transcripts to ${SUBDOMAIN} as ${DEVICE_ID}" >&2
[[ "$ship_failures" -eq 0 ]] || { echo "${ship_failures} fixture(s) failed to ship" >&2; exit 1; }

mapfile -t SESSION_IDS < <(list_sessions)
echo "${#SESSION_IDS[@]} sessions to compare (${with_events} fixtures shipped events)" >&2
[[ "${#SESSION_IDS[@]}" -ge "$with_events" && "$with_events" -gt 0 ]] \
  || { echo "expected at least ${with_events} sessions, found ${#SESSION_IDS[@]}" >&2; exit 1; }

ids_csv="$(IFS=,; echo "${SESSION_IDS[*]}")"
deadline=$((SECONDS + WAIT_SECONDS))
while :; do
  # The session ids expand here, on this side, by design.
  # shellcheck disable=SC2087,SC2029
  report="$(ssh "$SSH_TARGET" "docker exec -i ${CONTAINER} python3 -" <<PY
import json, sqlite3
ids = "${ids_csv}".split(",")
db = sqlite3.connect("file:/data/longhouse-live.db?mode=ro", uri=True)
marks = ",".join("?" * len(ids))
raw = db.execute(f"select count(*) from raw_objects where retired_at is null and session_id in ({marks})", ids).fetchone()[0]
rows = db.execute(
    f"select provider, outcome, count(*), max(elapsed_us) from render_shadow_envelopes where session_id in ({marks}) group by provider, outcome",
    ids,
).fetchall()
diffs = db.execute(
    f"select provider, session_id, outcome || ': ' || coalesce(detail, '') from render_shadow_envelopes where session_id in ({marks}) and outcome != 'match' limit 20",
    ids,
).fetchall()
print(json.dumps({"raw": raw, "compared": sum(r[2] for r in rows), "rows": rows, "diffs": diffs}))
PY
)"
  compared="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["compared"] >= d["raw"])' "$report")"
  [[ "$compared" == "True" || $SECONDS -ge $deadline ]] && break
  sleep 20
done
python3 - "$report" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"envelopes stored: {d['raw']}, compared: {d['compared']}")
for provider, outcome, count, max_us in sorted(d["rows"]):
    print(f"  {provider:12} {outcome:10} {count:5}  max {max_us or 0} us")
for provider, session, detail in d["diffs"]:
    print(f"  DIFF {provider} {session}: {detail}")
# Every stored envelope matched: anything else, a no_witness or raw_only
# included, means the host could not prove that envelope.
matched = sum(c for _p, o, c, _m in d["rows"] if o == "match")
sys.exit(0 if d["raw"] > 0 and matched == d["raw"] == d["compared"] else 1)
PY
