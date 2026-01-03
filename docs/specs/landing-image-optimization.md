# Web Quality & Performance Guardrails

**Status**: Phase 1 Complete, Phase 2 In Progress
**Date**: 2025-01-02

## Executive Summary

Initial issue: Landing page scenario images were 4.5-5MB each, causing 4-5s cold loads. Fixed with 95% size reduction.

Broader goal: Implement CI guardrails to catch web performance issues before they hit production.

## Problem Analysis

### Root Cause
Production has old, unoptimized images:
| Image | Production Size | Local Size | Reduction |
|-------|-----------------|------------|-----------|
| scenario-health.png | 5,071,569 bytes (4.84 MB) | 225,261 bytes (220 KB) | **95%** |
| scenario-inbox.png | 5,046,436 bytes (4.81 MB) | 270,289 bytes (264 KB) | **95%** |
| scenario-home.png | 4,658,756 bytes (4.44 MB) | 240,819 bytes (235 KB) | **95%** |

Total: **14.1 MB → 719 KB** (95% reduction)

### Secondary Issues
1. **Non-interlaced PNGs** - Images are 800x500 RGB but non-interlaced, meaning they render top-to-bottom instead of progressively
2. **No WebP fallback** - Modern browsers could use even smaller WebP format
3. **No lazy loading** - Images load immediately even though section is below fold
4. **CSS shimmer effect** - Shows skeleton while loading (correct behavior, but visible due to slow load)

### Other Large Images (not in critical path)
These exist in `/public/images/landing/` but aren't loaded via `<img>`:
- hero-orb.png: 4.5MB (unused - hero uses SVG)
- trust-shield.png: 4.8MB (unused)
- integrations-grid.png: 4.9MB (unused)
- og-image.png: 4.6MB (only for social sharing meta tag)

## Decision Log

### Decision: Push existing optimized images first
**Context**: Optimized 220-264KB images are already staged locally
**Choice**: Deploy these immediately before investigating further optimizations
**Rationale**: 95% reduction is massive win, already done
**Revisit if**: Users still report slow loading

### Decision: Skip WebP conversion for now
**Context**: Could reduce further with WebP
**Choice**: PNG optimization sufficient for now
**Rationale**: 220KB per image is fast enough, avoids build complexity
**Revisit if**: Target < 100KB images needed

### Decision: Skip lazy loading for now
**Context**: Scenarios section is ~1 viewport below fold
**Choice**: Let browser handle naturally
**Rationale**: With 220KB images, eager loading is fine
**Revisit if**: More images added to landing page

## Implementation Phases

### Phase 1: Deploy Optimized Images (Easy Win)
**Goal**: Push already-staged optimized images to production

**Steps**:
1. Commit the staged scenario images
2. Push to main (triggers Coolify auto-deploy)
3. Verify production image sizes via curl

**Acceptance Criteria**:
- [x] All three scenario images < 300KB in production (verified: 225KB, 270KB, 240KB)
- [x] Cold load time improved (verified via Playwright)

### Phase 2: Web Quality CI Stack
**Goal**: Comprehensive guardrails against web performance issues

**Recommended 3-Tool Stack** (low-maintenance, ~1.5 hrs/year upkeep):

| Tool | What It Catches | Setup | Maintenance |
|------|-----------------|-------|-------------|
| **Lighthouse CI** | Performance, accessibility, SEO, best practices, Core Web Vitals | 30 min | ~1 hr/year |
| **Size Limit** | Bloated JS bundles, unused dependencies | 15 min | ~30 min/year |
| **ImgBot** | Oversized images (auto-compresses on PR) | 5 min | Zero |

**Would have caught 5MB image issue?**
- Lighthouse CI: Yes (performance score drops 90 → 40)
- ImgBot: Yes (auto-compresses, creates PR with optimized version)
- Size Limit: No (only tracks JS/CSS bundles, not images)

**Why this stack:**
- Free & OSS
- "Set and forget" - minimal config drift
- Catches 80% of web quality issues
- No external services (except ImgBot GitHub App)

**Implementation Steps**:

1. Create `.github/workflows/quality.yml` with Lighthouse CI
2. Create `lighthouserc.json` with performance thresholds
3. Create `.size-limit.js` for bundle budgets
4. Install ImgBot from GitHub Marketplace

**Acceptance Criteria**:
- [ ] Lighthouse CI runs on every PR
- [ ] Performance score threshold: 85 (error), Accessibility: 90 (warn)
- [ ] Bundle size limit: 200KB JS, 50KB CSS
- [ ] ImgBot auto-compresses images on PRs

### Phase 3: Clean Up Unused Images (Optional)
**Goal**: Remove dead weight from repo

**Steps**:
1. Verify hero-orb.png, trust-shield.png, integrations-grid.png are unused
2. Delete from git (saves ~18MB in repo)
3. Update og-image.png if used (social sharing)

**Acceptance Criteria**:
- [ ] No 404s on landing page
- [ ] Repo size reduced

## Test Commands

```bash
# Check production image sizes
curl -sI "https://swarmlet.com/images/landing/scenario-health.png" | grep content-length
curl -sI "https://swarmlet.com/images/landing/scenario-inbox.png" | grep content-length
curl -sI "https://swarmlet.com/images/landing/scenario-home.png" | grep content-length

# Expected: < 300000 bytes each
```
