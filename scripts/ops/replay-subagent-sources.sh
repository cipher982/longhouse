#!/usr/bin/env bash
#
# Replay in-harness subagent transcripts so already-shipped workers pick up the
# lineage the storage-v2 contract used to drop.
#
# A parser revision bump does NOT re-ship an exhausted file source: `observe_file`
# returns at EOF, and automatic revision replay exists only for Cursor and
# Antigravity (engine/src/storage_v2_shipper.rs). `--replay` mints a replacement
# source epoch, which is the only mechanism that makes the host treat the same
# bytes as new material. Events deduplicate by hash, so history is refreshed
# rather than duplicated.
#
# Without this sweep the fix applies only to workers produced after the deploy,
# and the ones already sitting in the timeline stay there.
#
# Usage:
#   scripts/ops/replay-subagent-sources.sh [--dry-run] [--limit N] [--root DIR]
set -euo pipefail

DRY_RUN="false"
LIMIT=0
ROOT="${HOME}/.claude/projects"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! command -v longhouse-engine >/dev/null 2>&1; then
  echo "longhouse-engine is not on PATH; run 'make dogfood-refresh' first." >&2
  exit 1
fi

# Every Claude subagent transcript: Task/Agent children directly under
# `subagents/`, and workflow children under `subagents/workflows/<run>/`.
mapfile -t SOURCES < <(find "$ROOT" -path '*/subagents/*' -name 'agent-*.jsonl' | sort)

if [[ "$LIMIT" -gt 0 && "${#SOURCES[@]}" -gt "$LIMIT" ]]; then
  echo "Limiting to $LIMIT of ${#SOURCES[@]} sources"
  SOURCES=("${SOURCES[@]:0:$LIMIT}")
fi

echo "Replaying ${#SOURCES[@]} subagent sources (dry_run=$DRY_RUN)"

replayed=0
failed=0
for source in "${SOURCES[@]}"; do
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "would replay: $source"
    replayed=$((replayed + 1))
    continue
  fi
  if longhouse-engine ship --file "$source" --provider claude --replay --json >/dev/null 2>&1; then
    replayed=$((replayed + 1))
  else
    failed=$((failed + 1))
    echo "replay failed: $source" >&2
  fi
  # A silent cap would read as "covered everything"; report both counts.
  if (( (replayed + failed) % 100 == 0 )); then
    echo "progress: $((replayed + failed))/${#SOURCES[@]} (failed=$failed)"
  fi
done

echo "replayed=$replayed failed=$failed total=${#SOURCES[@]}"
[[ "$failed" -eq 0 ]]
