#!/bin/bash
# Disposable search eval on HEL1: one container, one corpus dir, results JSON, teardown.
# Usage: OWNER_EMAIL=... eval_run.sh <name> <image> <data_dir> <port> <wait_rebuild 0|1>
# Optional env: EVAL_CPUS (6), EVAL_EXTRA_ENV (extra `-e K=V` docker flags),
# EVAL_WAIT_POLLS (2160 x 10 s). Expects run_eval.py, run_known_items.py and
# queries.jsonl under $ROOT/eval and known_items.jsonl under $ROOT.
set -uo pipefail
NAME=$1; IMAGE=$2; DATA=$3; PORT=$4; WAIT_REBUILD=$5
: "${OWNER_EMAIL:?set OWNER_EMAIL to the corpus owner}"
ROOT=/var/app-data/search-eval
OUT=$ROOT/results/$NAME; mkdir -p "$OUT"
LOG=$OUT/run.log
C=search-eval-$NAME
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
cleanup() { docker rm -f "$C" >/dev/null 2>&1; log "container removed"; }
trap cleanup EXIT

FK=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
docker rm -f "$C" >/dev/null 2>&1
start=$(date +%s)
docker run -d --name "$C" --memory 10g --cpus ${EVAL_CPUS:-6} -p 127.0.0.1:$PORT:8000 \
  -v "$DATA:/data" \
  -e SINGLE_TENANT=1 -e AUTH_DISABLED=1 -e DATABASE_URL=sqlite:////data/longhouse.db \
  -e LONGHOUSE_EMBED_MODEL_DIR=/opt/longhouse/embedding-model \
  -e FERNET_SECRET="$FK" -e JWT_SECRET=search-eval-throwaway-jwt-0001 \
  -e INTERNAL_API_SECRET=search-eval-throwaway-internal-0001 \
  -e LLM_DISABLED=1 -e AI_TITLES_AND_SUMMARIES_ENABLED=0 -e LONGHOUSE_ALLOW_PUBLIC_NO_AUTH=1 -e OWNER_EMAIL="$OWNER_EMAIL" \
  ${EVAL_EXTRA_ENV:-} "$IMAGE" >> "$LOG" 2>&1
URL=http://127.0.0.1:$PORT
for i in $(seq 1 180); do curl -sf "$URL/api/readyz" >/dev/null && break; sleep 2; done
log "ready after $(( $(date +%s)-start ))s"
COMMIT=$(curl -s "$URL/api/health" | python3 -c "import json,sys;print(json.load(sys.stdin).get('build',{}).get('commit',''))")
log "server commit=$COMMIT extra_env=[${EVAL_EXTRA_ENV:-}]"

peak=0
sample_mem() {
  local cur; cur=$(cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$C").scope/memory.current 2>/dev/null || echo 0)
  (( cur > peak )) && peak=$cur
}

if [ "$WAIT_REBUILD" = 1 ]; then
  # Wait for the search-v2 rebuild to finish: coverage.complete on a session search.
  rb=$(date +%s)
  for i in $(seq 1 ${EVAL_WAIT_POLLS:-2160}); do
    sample_mem
    cov=$(curl -s -m 30 -H "X-Agents-Token: unused" "$URL/api/agents/sessions?query=the&limit=1&days_back=90" | python3 -c "
import json,sys
d=json.load(sys.stdin); c=d.get('coverage') or {}
print(c.get('complete'), c.get('indexed_sessions'), c.get('expected_sessions'))" 2>/dev/null)
    echo "$(date -u +%T) $cov" >> "$OUT/rebuild_progress.txt"
    case "$cov" in True*) break;; esac
    sleep 10
  done
  log "rebuild wait elapsed=$(( $(date +%s)-rb ))s last=[$cov]"
fi

cd $ROOT/eval
# The dense slab loads after startup; its cold queries error until then.
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "X-Agents-Token: unused" "$URL/api/agents/recall?query=search%20index&max_results=1&since_days=30&mode=semantic")
  [ "$code" = 200 ] && break; sleep 10
done
log "dense warmup http=$code after $i polls"
for strategy in lexical dense; do
  python3 run_known_items.py --input $ROOT/known_items.jsonl --strategy $strategy --url "$URL" --days 365 \
    > "$OUT/known_items_$strategy.json" 2>> "$LOG"
  sample_mem
done
for strategy in lexical semantic auto; do
  LONGHOUSE_EVAL_URL="$URL" LONGHOUSE_EVAL_TOKEN=unused python3 run_eval.py --strategy $strategy --json --expected-sha "$COMMIT" \
    > "$OUT/paraphrase_$strategy.json" 2>> "$LOG"
  sample_mem
done
sample_mem
log "peak_memory_bytes=$peak"
sqlite3 "file:$DATA/search.db?mode=ro" "select name, sum(pgsize) from dbstat group by name order by 2 desc limit 12;" > "$OUT/dbstat.txt" 2>> "$LOG"
ls -la "$DATA"/search.db* >> "$OUT/dbstat.txt" 2>&1
log "DONE total_elapsed=$(( $(date +%s)-start ))s"
