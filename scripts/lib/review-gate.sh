#!/usr/bin/env bash
# The review gate ring promotion calls before it changes anything.
#
#   lh_review_gate_promotion TARGET_SHA SERVED_HEALTH_URL
#
# Refuses (non-zero, commits listed on stderr) when a code commit between what
# SERVED_HEALTH_URL reports as served and TARGET_SHA has no completed review
# receipt, or holds an unresolved blocking/material finding. Docs, tests and
# release version bumps are exempt. Policy: scripts/ops/review-policy.toml; the
# gate and the receipt format: scripts/ops/review_gate.py.
lh_review_gate_promotion() {
  local target="${1:?target sha}" served_url="${2:?served health url}"
  git -C "${ROOT:?}" fetch --quiet origin >/dev/null 2>&1 || true
  python3 "$ROOT/scripts/ops/review_gate.py" --repo "$ROOT" promotion --target "$target" --served-url "$served_url"
}
