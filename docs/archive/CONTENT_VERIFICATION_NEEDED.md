# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---

# Content Verification Status

**Created:** 2025-12-02
**Last Updated:** 2025-12-02
**Status:** Most fabricated content removed; minor items remain

---

## Summary

The info pages have been stripped of unverified claims. Most pages now contain only generic, factual information. Below are the remaining items that may need verification.

---

## Changes Made

### Changelog Page

- **Fixed:** Replaced fabricated version history with "coming soon" placeholder
- Links to actual GitHub repo (cipher982/zerg)

### Security Page

- **Fixed:** Removed all specific technical claims (AES-256, TLS 1.3, SOC 2, pentest roadmap, etc.)
- Now states only verifiable facts: HTTPS, OAuth 2.0, account deletion, encrypted integration credentials

### Privacy Page

- **Fixed:** Removed specific retention periods, data location, children's privacy, analytics details
- Now states only generic facts about data collection

### Docs Page

- **Fixed:** Removed fabricated API reference (endpoints, base URL)
- Now just a quick start guide

### Pricing Page

- **Fixed:** Removed "bring your own API keys" claims, Pro/Enterprise tiers, guarantees
- Now just shows free beta tier

### Domain Alignment

- **Fixed:** Changed all `swarmlet.ai` references to `swarmlet.com` (matches public/config.js)

### LLM Claims

- **Fixed:** Changed from "OpenAI, Anthropic, Gemini, etc." to just "OpenAI" (reflects actual backend)
- Removed "bring your own API keys" - backend uses platform-configured keys, not user-uploaded

---

## Remaining Items to Verify

### Email Address

- [x] `swarmlet@drose.io` - Updated all pages to use this

### Discord

- [x] `https://discord.gg/h2CWBUrj` - Added to all contact sections

### GitHub URL

- [x] `https://github.com/cipher982/zerg` - Confirmed and updated everywhere

### Integration Credential Encryption

- [x] Verified: Connector credentials use Fernet encryption (agent_connectors.py:170-200)

---

## Files Modified

- `apps/zerg/frontend-web/src/pages/ChangelogPage.tsx`
- `apps/zerg/frontend-web/src/pages/SecurityPage.tsx`
- `apps/zerg/frontend-web/src/pages/PrivacyPage.tsx`
- `apps/zerg/frontend-web/src/pages/DocsPage.tsx`
- `apps/zerg/frontend-web/src/pages/PricingPage.tsx`
- `apps/zerg/frontend-web/src/components/landing/TrustSection.tsx`
- `apps/zerg/frontend-web/src/components/landing/NerdSection.tsx`
- `apps/zerg/frontend-web/src/components/landing/FooterCTA.tsx`
