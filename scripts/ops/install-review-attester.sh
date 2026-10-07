#!/usr/bin/env bash
# Install the review attester: a launchd agent that runs `review_gate.py attest` every two minutes.
#
#   make install-push-gate        (installs this too; once, on the machine that holds the review receipts)
#
# Review receipts live in this clone's git common dir, but the ring promoter runs on a CI runner
# (.github/workflows/promote-rings.yml) so production keeps following dogfood while this Mac sleeps. The
# attester publishes the promotion verdict for what dogfood serves and for origin/main as a GitHub
# commit status (`longhouse/review-gate`, no findings text), queues reviews for any commit that lacks
# one, and posts again whenever the verdict changes: a review lands, a finding is dispositioned,
# production or dogfood moves. Unchanged verdicts post nothing. See review_gate.py "attestations".
#
# Each run fetches origin and runs origin/main's copy of the gate and policy (extracted into
# <git common dir>/review-receipts/attester/, so it never waits for a checkout to be updated and never
# touches one), in a login shell so gh and hatch resolve their credentials as they do for an agent.
# Log: <git common dir>/review-receipts/attest.log.
# Remove: launchctl bootout gui/$(id -u)/ai.longhouse.review-attest && rm ~/Library/LaunchAgents/ai.longhouse.review-attest.plist
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-review-attester: launchd only (macOS); run 'python3 scripts/ops/review_gate.py attest' from cron elsewhere." >&2
  exit 0
fi

label="ai.longhouse.review-attest"
common="$(git rev-parse --path-format=absolute --git-common-dir)"
primary="$(cd "$common/.." && pwd)"
log="$common/review-receipts/attest.log"
cache="$common/review-receipts/attester"
mkdir -p "$cache"
# Both files refresh together or not at all (the previous pair keeps running), then the gate runs.
run="git -C '$primary' fetch --quiet origin; git -C '$primary' show origin/main:scripts/ops/review_gate.py > '$cache/review_gate.py.tmp' && git -C '$primary' show origin/main:scripts/ops/review-policy.toml > '$cache/review-policy.toml.tmp' && mv '$cache/review_gate.py.tmp' '$cache/review_gate.py' && mv '$cache/review-policy.toml.tmp' '$cache/review-policy.toml'; exec python3 '$cache/review_gate.py' --repo '$primary' attest"
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$(dirname "$plist")"
tmp="$(mktemp "$plist.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cat >"$tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>$(xml_escape "$run")</string>
  </array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$(xml_escape "$log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$log")</string>
</dict>
</plist>
PLIST
plutil -lint "$tmp" >/dev/null
mv "$tmp" "$plist"
trap - EXIT
launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "install-review-attester: $label runs every 120 s (log $log)"
