#!/usr/bin/env bash
# check-email-routing.sh — Verify all mailto: links on the marketing site
# have active Cloudflare email forwarding rules (not a catch-all drop).
#
# Usage:
#   CF_API_TOKEN=xxx ./scripts/check-email-routing.sh
#   CF_API_TOKEN=xxx MARKETING_URL=https://longhouse.ai ./scripts/check-email-routing.sh
#
# Exit: 0 = all addresses routed, 1 = one or more addresses unrouted

set -euo pipefail

MARKETING_URL="${MARKETING_URL:-https://longhouse.ai}"
CF_ZONE_ID="${CF_ZONE_ID:-1c0f5f8ea503262546e94522090ba832}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

# --- Resolve CF token ---
if [[ -z "${CF_API_TOKEN:-}" ]]; then
    # Try macOS Keychain when running locally
    CF_API_TOKEN=$(security find-generic-password -a cloudflare -s cloudflare-api-token -w 2>/dev/null || true)
fi
if [[ -z "${CF_API_TOKEN:-}" ]]; then
    echo "Error: CF_API_TOKEN not set and not found in Keychain" >&2
    exit 1
fi

echo ""
echo "--- Email Routing Check ---"
echo "  Site: $MARKETING_URL"
echo ""

# --- Scrape mailto: addresses from the live site ---
# The marketing site is a React SPA — mailto links live in JS bundles, not raw HTML.
html=$(curl -s --max-time 15 "$MARKETING_URL")

# Collect all linked JS bundles
content="$html"
while IFS= read -r js_path; do
    [[ -z "$js_path" ]] && continue
    content+=$(curl -s --max-time 15 "${MARKETING_URL%/}${js_path}" || true)
done < <(echo "$html" | grep -oE 'src="/assets/[^"]+\.js"' | sed 's/src="//;s/"//' | sort -u)

# Extract mailto: addresses (strip query strings and trailing punctuation)
mailto_addresses=$(echo "$content" | grep -oE 'mailto:[^\\?"]+' | sed 's/mailto://' | sort -u)

if [[ -z "$mailto_addresses" ]]; then
    echo "  No mailto: links found on $MARKETING_URL"
    exit 0
fi

# --- Fetch all email routing rules for the zone ---
rules_json=$(curl -s --max-time 15 \
    "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/email/routing/rules" \
    -H "Authorization: Bearer $CF_API_TOKEN")

if ! echo "$rules_json" | jq -e '.success == true' > /dev/null 2>&1; then
    echo "Error: Cloudflare API call failed" >&2
    echo "$rules_json" >&2
    exit 1
fi

FAILED=0

while IFS= read -r address; do
    [[ -z "$address" ]] && continue

    # Find an enabled rule that literal-matches this address with a 'forward' action
    match=$(echo "$rules_json" | jq -r --arg addr "${address,,}" '
        .result[]
        | select(.enabled == true)
        | select(.matchers[] | .type == "literal" and (.value | ascii_downcase) == $addr)
        | .actions[]
        | select(.type == "forward")
        | .value | join(", ")
    ' 2>/dev/null | head -1)

    if [[ -n "$match" ]]; then
        pass "$address → $match"
    else
        fail "$address — no active forwarding rule (will be dropped)"
        FAILED=$((FAILED + 1))
    fi
done <<< "$mailto_addresses"

echo ""
if [[ $FAILED -gt 0 ]]; then
    echo -e "  ${RED}$FAILED address(es) unrouted — add Cloudflare forwarding rules${NC}"
    exit 1
else
    echo -e "  ${GREEN}All mailto: addresses have active forwarding rules${NC}"
fi
