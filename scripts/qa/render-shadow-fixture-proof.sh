#!/usr/bin/env bash
# Host-normalized transcripts, Phase 3 check on a hosted QA Runtime Host.
#
# Ships every engine transcript fixture through the real shipper to a hosted
# QA target under a throwaway device token, waits for that host's render
# shadow to compare each envelope (the host rebuilds it from stored raw plus
# the capture witness and compares it with the shipped render), prints the
# outcomes for exactly those sessions, then deletes the sessions and revokes
# the token. Exit 0 only when every witnessed envelope matched.
#
#   SMOKE_RUNTIME_TOKEN=<QA access token> scripts/qa/render-shadow-fixture-proof.sh
#
# Never point it at david010: QA_INSTANCE_SUBDOMAIN defaults to hosted-qa-a
# and the script refuses the dogfood tenant.
set -euo pipefail

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
WAIT_SECONDS="${RENDER_SHADOW_PROOF_WAIT_SECONDS:-1200}"
: "${SMOKE_RUNTIME_TOKEN:?Set SMOKE_RUNTIME_TOKEN to the QA host access token}"

SCRATCH="$(mktemp -d /tmp/agents/render-shadow-proof.XXXXXX)"
DEVICE_ID="render-shadow-proof-${RANDOM}${RANDOM}"
TOKEN_ID=""
DEVICE_TOKEN=""
SESSION_IDS=()

cleanup() {
  local status=$?
  for session_id in "${SESSION_IDS[@]}"; do
    curl -sS -o /dev/null --max-time 60 -X DELETE -H "Authorization: Bearer ${SMOKE_RUNTIME_TOKEN}" \
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
for entry in "${FIXTURE_SETS[@]}"; do
  read -r tree provider <<<"$entry"
  source_root="$ROOT_DIR/engine/tests/fixtures/$tree"
  [[ -d "$source_root" ]] || continue
  case_root="$SCRATCH/$(echo "$tree" | tr / _)"
  mkdir -p "$case_root"
  # Copy the tree whole so sidecars sit where the shipper looks.
  cp -R "$source_root/." "$case_root/src"
  while IFS= read -r -d '' transcript; do
    HOME="$case_root/home" "$ENGINE" ship --file "$transcript" --provider "$provider" \
      --url "$API_URL" --token "$DEVICE_TOKEN" --db "$case_root/state-$shipped.db" \
      --machine-name "$DEVICE_ID" --json >"$case_root/ship-$shipped.json" 2>"$case_root/ship-$shipped.err" \
      || { echo "ship failed: $tree/$(basename "$transcript")" >&2; cat "$case_root/ship-$shipped.err" >&2; }
    shipped=$((shipped + 1))
  done < <(find "$case_root/src" -type f \( -name '*.jsonl' -o -name '*.json' \) ! -name '*.expected.*' ! -name '*.meta.json' -print0 | sort -z)
done
echo "shipped ${shipped} fixture transcripts to ${SUBDOMAIN} as ${DEVICE_ID}" >&2

list_sessions() {
  curl -sS --max-time 30 -H "X-Agents-Token: ${DEVICE_TOKEN}" \
    "${API_URL}/api/agents/sessions?limit=200&days_back=3650&include_test=true" \
    | python3 -c 'import json,sys; device=sys.argv[1]; data=json.load(sys.stdin); print("\n".join(s["id"] for s in data.get("sessions", []) if s.get("device_id") == device))' "$DEVICE_ID"
}
mapfile -t SESSION_IDS < <(list_sessions)
echo "${#SESSION_IDS[@]} sessions to compare" >&2
[[ "${#SESSION_IDS[@]}" -gt 0 ]] || { echo "no sessions arrived" >&2; exit 1; }

ids_csv="$(IFS=,; echo "${SESSION_IDS[*]}")"
deadline=$((SECONDS + WAIT_SECONDS))
while :; do
  # The session ids expand here, on this side, by design.
  # shellcheck disable=SC2087
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
    f"select provider, session_id, detail from render_shadow_envelopes where session_id in ({marks}) and outcome in ('mismatch','error') limit 20",
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
bad = sum(c for _p, o, c, _m in d["rows"] if o in ("mismatch", "error"))
matched = sum(c for _p, o, c, _m in d["rows"] if o == "match")
sys.exit(0 if d["compared"] >= d["raw"] and bad == 0 and matched > 0 else 1)
PY
