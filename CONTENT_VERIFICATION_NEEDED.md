# Content Verification Needed

**Created:** 2025-12-02
**Status:** Awaiting user input
**Purpose:** Track unverified claims in landing/info pages that need verification before production

---

## Summary

The following pages contain claims that were written by AI and need verification from the product owner before going live. **No fabricated data should ship to production.**

---

## 1. CHANGELOG PAGE - 100% FABRICATED

The entire version history was invented. This page needs to be either:
- Replaced with actual release history
- Changed to a placeholder ("Coming soon")
- Removed entirely

**Fabricated content:**
- Version numbers (0.1.0, 0.2.0, 0.3.0, 0.4.0)
- All dates (September - December 2024)
- All feature descriptions for each release
- "Mailing list" mention

**File:** `apps/zerg/frontend-web/src/pages/ChangelogPage.tsx`

---

## 2. SECURITY PAGE - Unverified Technical Claims

| Claim | Location | Question |
|-------|----------|----------|
| "AES-256 encryption" for data at rest | Line 40 | Is this actually implemented? |
| "TLS 1.3" for connections | Line 47 | What TLS version is actually used? |
| API keys "encrypted using AES-256" | Line 96 | How are credentials actually stored? |
| "Stored in separate, access-controlled database" | Line 97 | Is this accurate? |
| "Never written to logs" | Line 98 | Has this been verified? |
| "Session tokens short-lived and refreshed" | Line 74 | What's the actual token lifetime? |
| "30 days" deletion timeframe | Line 141 | Is this the actual policy? |
| Data stored "in United States" | Line 124 | Where is data actually hosted? |
| "Cloud providers with SOC 2 Type II" | Line 125 | Which provider? Do they have SOC 2? |
| Compliance roadmap "Done" items | Lines 148-166 | Are these actually done? |
| "Penetration Testing - In Progress" | Line 170 | Is this actually happening? |
| "SOC 2 Type I - Planned" | Line 177 | Is this actually planned? |
| "GDPR Compliance - Planned" | Line 184 | Is this actually planned? |
| `security@swarmlet.ai` email | Lines 196, 209 | Does this email exist? |
| `privacy@swarmlet.ai` email | Line 213 | Does this email exist? |

**File:** `apps/zerg/frontend-web/src/pages/SecurityPage.tsx`

---

## 3. PRIVACY PAGE - Unverified Policy Details

| Claim | Location | Question |
|-------|----------|----------|
| Usage data collected (list) | Lines 46-51 | What do you actually collect? |
| "30 days" deletion timeframe | Line 94 | What's the actual policy? |
| Cookie usage description | Lines 107-110 | What cookies are actually used? |
| Third-party services (Google, cloud, analytics) | Lines 114-119 | Is this a complete list? |
| `privacy@swarmlet.ai` email | Line 142 | Does this email exist? |

**File:** `apps/zerg/frontend-web/src/pages/PrivacyPage.tsx`

---

## 4. DOCS PAGE - Unverified Features/API

| Claim | Location | Question |
|-------|----------|----------|
| "under 5 minutes" setup time | Line 36 | Is this accurate? |
| LLM providers: OpenAI, Anthropic, "other" | Lines 69-71 | What's actually supported? |
| Node types (Triggers, Actions, Conditions, AI) | Lines 97-100 | Do these exist as described? |
| Integrations list | Lines 120-123 | Which are actually available vs aspirational? |
| API Base URL `https://api.swarmlet.ai/v1` | Line 151 | Does this URL exist? |
| API endpoints listed | Lines 157-161 | Do these endpoints exist? |
| GitHub `https://github.com/swarmlet` | Line 175 | Is this the correct URL? |
| `hello@swarmlet.ai` email | Line 174 | Does this email exist? |

**File:** `apps/zerg/frontend-web/src/pages/DocsPage.tsx`

---

## 5. PRICING PAGE - Minor Verification Needed

| Claim | Location | Question |
|-------|----------|----------|
| "Unlimited agents" | Line 51 | Is this true for free tier? |
| "All integrations" | Line 53 | Are all integrations available in free? |
| "Community support" | Line 55 | Does this exist? |
| "Early adopter perks" | Line 56 | What perks? Or remove? |
| "Will always have a free tier" | Lines 119-121 | Is this the actual commitment? |
| `hello@swarmlet.ai` | Line 102 | Does this email exist? |

**File:** `apps/zerg/frontend-web/src/pages/PricingPage.tsx`

---

## Questions to Answer

### Email Addresses
Which of these exist and should be used?
- [ ] `hello@swarmlet.ai`
- [ ] `security@swarmlet.ai`
- [ ] `privacy@swarmlet.ai`

### Changelog Decision
- [ ] a) Replace with actual version history (provide details)
- [ ] b) Change to "Coming soon" placeholder
- [ ] c) Remove the page entirely

### Security Technical Details
- Encryption for data at rest: _________________
- Encryption for stored credentials: _________________
- Data hosting location (provider, region): _________________
- Actual token lifetime: _________________
- Actual account deletion timeframe: _________________

### Compliance Roadmap Status
- [ ] HTTPS/TLS - Done
- [ ] OAuth 2.0 - Done
- [ ] Encrypted credentials - Done
- [ ] Penetration testing - In progress
- [ ] SOC 2 - Planned
- [ ] GDPR - Planned

### Integrations
Available today:
- _________________

Aspirational/coming soon:
- _________________

### API
- Is there a public API? _________________
- Base URL: _________________

### GitHub
- Correct org/repo URL: _________________

---

## Action Items After Verification

1. Update ChangelogPage based on decision
2. Update SecurityPage with verified technical details
3. Update PrivacyPage with accurate policy
4. Update DocsPage with verified features/API
5. Update PricingPage if needed
6. Commit all changes with "fix: update content with verified information"
