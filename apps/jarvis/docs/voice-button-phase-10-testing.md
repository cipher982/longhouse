# Phase 10: Voice Button Testing & Validation Plan

**Status**: Ready for Testing
**Created**: 2025-11-14
**Testing Period**: [TBD]

---

## Overview

Phase 10 validates the voice button redesign (Phases 1-9) through systematic testing across devices, users, and assistive technologies. Goal: Ensure the single-button interface is intuitive, accessible, and performs as designed.

---

## 1. Physical Device Testing

### Devices to Test

| Device | OS | Screen Size | Test Focus |
|--------|------|-------------|-----------|
| iPhone | iOS 17+ | ~6" | Thumb reach, haptic feedback, Safari audio |
| Android Phone | Android 13+ | ~6" | Thumb reach, vibration, Chrome audio |
| iPad / Android Tablet | Latest | ~10" | Two-handed use, spacing |
| Desktop | macOS/Windows | Variable | Mouse + keyboard, focus styles |

### Testing Checklist

For each device, test the following flows:

#### **Flow A: First-Time Connection (IDLE → READY)**
- [ ] Device: _____________
- [ ] Button visible and centered on landing page?
- [ ] "Tap to speak" label clearly readable?
- [ ] Button size appropriate for thumb (not too small/large)?
- [ ] Tap button - does it respond immediately?
- [ ] "Connecting..." state visible with animation?
- [ ] Connection establishes successfully?
- [ ] Haptic vibration felt on connection? (mobile only)
- [ ] Audio chime heard? (if enabled)
- [ ] "Ready to talk" state clear?
- [ ] Green glow visible and pleasant?
- [ ] **Time to first interaction:** ______ seconds

#### **Flow B: Push-to-Talk Mode (READY → SPEAKING → READY)**
- [ ] Device: _____________
- [ ] Press and hold button - responds immediately?
- [ ] Button changes to pink with pulse animation?
- [ ] "Listening..." label appears?
- [ ] Can comfortably hold button while speaking?
- [ ] Release button - returns to READY state?
- [ ] Thumb doesn't slip off button during hold?

#### **Flow C: Voice Activity Detection (READY → SPEAKING → READY)**
- [ ] Device: _____________
- [ ] Start speaking - button reacts automatically?
- [ ] Voice tick sound heard when speech detected?
- [ ] Button transitions to SPEAKING state?
- [ ] Stop speaking - returns to READY within 500ms?

#### **Flow D: Full Conversation (READY → RESPONDING → READY)**
- [ ] Device: _____________
- [ ] Speak a question
- [ ] Button transitions to RESPONDING (blue)?
- [ ] "Assistant is responding" label visible?
- [ ] Assistant audio plays clearly?
- [ ] Button returns to READY when response ends?
- [ ] Ready for next turn immediately?

#### **Flow E: Keyboard Navigation (Desktop Only)**
- [ ] Tab to focus button - focus ring visible?
- [ ] Focus ring color matches button state?
- [ ] Press Space/Enter - connects successfully?
- [ ] Hold Space/Enter - activates PTT mode?
- [ ] Release key - ends speaking?
- [ ] No page scroll when pressing Space?

#### **Flow F: Reduced Motion (Accessibility)**
- [ ] Enable "Reduce motion" in OS settings
- [ ] Reload page
- [ ] All animations disabled?
- [ ] State colors still change appropriately?
- [ ] Interface still usable without motion?

### Issues Log

| Device | Issue | Severity | Notes |
|--------|-------|----------|-------|
| | | | |
| | | | |

---

## 2. User Testing Script

### Participant Profile
- **Target:** 5 first-time users who have NOT seen the interface before
- **Mix:** 2 mobile users, 2 desktop users, 1 tablet user
- **Diversity:** Range of ages, technical backgrounds

### Pre-Test Setup
1. **No instructions given** - let user discover interface naturally
2. **Screen recording** - Capture all interactions
3. **Think-aloud protocol** - Ask user to narrate their thoughts
4. **Timing tool** ready - Measure time to first interaction

### Test Script

#### **Task 1: Initial Impression (30 seconds)**
"You've opened this page for the first time. What do you think it does? What would you click first?"

**Observe:**
- [ ] Does user immediately understand it's a voice interface?
- [ ] Is there any confusion about the button's purpose?
- [ ] Does user hesitate or look for other buttons?
- [ ] Time to first click: ______ seconds

#### **Task 2: First Connection (Unprompted)**
"Go ahead and try to use it."

**Observe:**
- [ ] Does user click the button without prompting?
- [ ] Any confusion during "Connecting..." state?
- [ ] Does user understand "Ready to talk" means they can speak now?
- [ ] Time from page load to first connection: ______ seconds

#### **Task 3: Voice Interaction**
"Try asking a question or saying something."

**Observe:**
- [ ] Does user understand how to trigger voice input?
- [ ] PTT vs VAD - which mode do they use naturally?
- [ ] Any confusion during speaking or responding states?
- [ ] Does user wait for "Ready" state before next turn?

#### **Task 4: Error Recovery**
"What would you do if the connection fails?"

**Simulate:** Disconnect backend or trigger error state

**Observe:**
- [ ] Does user understand error state (IDLE with error message)?
- [ ] Do they try clicking the button again to reconnect?
- [ ] Is error feedback (haptic/audio) helpful or confusing?

#### **Task 5: Post-Test Interview**
1. "On a scale of 1-5, how intuitive was the button?" ______
2. "What was confusing, if anything?"
3. "Did you notice any sounds or vibrations? Were they helpful?"
4. "Would you prefer two buttons or one?" Why?
5. "Any other feedback?"

### Success Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Time to first interaction | < 3 seconds | _____ |
| Users who click correct button first try | > 95% | _____ |
| Users who successfully connect | > 95% | _____ |
| Users who rate interface 4+ / 5 | > 80% | _____ |
| Confusion points mentioned | < 2 per user | _____ |

---

## 3. Screen Reader Testing Protocol

### Tools Required
- **macOS:** VoiceOver (Cmd+F5)
- **Windows:** NVDA (free download)
- **iOS:** VoiceOver (Settings → Accessibility)
- **Android:** TalkBack (Settings → Accessibility)

### Testing Checklist

#### **VoiceOver (macOS/iOS)**
- [ ] Open page with VoiceOver enabled
- [ ] Navigate to voice button (VO+Right arrow)
- [ ] Button announces: "Connect to voice service, button"
- [ ] Activate button (VO+Space)
- [ ] Connecting state announces: "Connecting..., button, busy"
- [ ] Ready state announces: "Push to talk, button"
- [ ] Status label announced automatically? **[KNOWN ISSUE]**
  - Current: CSS `::after` content not announced
  - Expected: Silent (documented limitation)
- [ ] Speaking state announces: "Speaking - release to send, button, pressed"
- [ ] Responding state announces: "Assistant is responding, button"

#### **NVDA (Windows) / TalkBack (Android)**
- [ ] Test same flow as VoiceOver above
- [ ] Check if aria-live region behavior differs
- [ ] Verify ARIA attributes read correctly

### Known Limitations

**Status Label Announcements:**
- Current: `.voice-status-label` uses CSS `::after` pseudo-content
- Issue: Screen readers cannot announce pseudo-content
- Impact: State changes visible but not announced
- ARIA live region: Configured correctly but receives no text nodes
- Fix required: Wire `setStatusLabel()` to push real text for announcements

**Decision Point:** Should we fix this now or defer?

**Option A: Fix Now (Recommended)**
- Wire `setStatusLabel()` in `setVoiceButtonState()` function
- Push real text to ARIA live region on every state change
- Screen readers announce: "Connecting..." → "Ready to talk" → "Listening..." → etc.
- Effort: ~30 minutes

**Option B: Defer to Post-Launch**
- Accept current limitation
- Visual users have full feedback
- Screen reader users rely on button's aria-label
- Add to backlog for future iteration

### Screen Reader Issues Log

| Tool | Issue | Severity | Notes |
|------|-------|----------|-------|
| | | | |

---

## 4. Metrics Collection Template

### Quantitative Data

**Time Measurements:**
| User | Device | Time to First Click | Time to Connection | Notes |
|------|--------|-------------------|-------------------|-------|
| User 1 | | | | |
| User 2 | | | | |
| User 3 | | | | |
| User 4 | | | | |
| User 5 | | | | |
| **Average** | | | | |

**Error Rates:**
| User | Wrong Button Clicks | Failed Connections | Confusion Points |
|------|-------------------|-------------------|-----------------|
| User 1 | | | |
| User 2 | | | |
| User 3 | | | |
| User 4 | | | |
| User 5 | | | |
| **Total** | | | |

### Qualitative Feedback

**Common Themes:**
- [ ] Positive: ________________________________
- [ ] Negative: ________________________________
- [ ] Confusion: ________________________________
- [ ] Suggestions: ______________________________

---

## 5. Testing Observations Log

### Session 1: [Date] - [Device] - [User Type]
**Setup:** ___________________________
**Duration:** _____ minutes

**Observations:**
-
-
-

**Issues Found:**
-
-

**Action Items:**
- [ ]
- [ ]

---

### Session 2: [Date] - [Device] - [User Type]
**Setup:** ___________________________
**Duration:** _____ minutes

**Observations:**
-
-

**Issues Found:**
-
-

**Action Items:**
- [ ]
- [ ]

---

## 6. Post-Testing Iteration

### Critical Issues (Block Launch)
- [ ] Issue: _______________
  - Severity: High
  - Fix: _______________
  - ETA: _______________

### Medium Issues (Fix Before Wide Release)
- [ ] Issue: _______________
  - Severity: Medium
  - Fix: _______________
  - ETA: _______________

### Minor Issues (Backlog)
- [ ] Issue: _______________
  - Severity: Low
  - Fix: _______________
  - Deferred to: _______________

---

## 7. Go/No-Go Decision

### Launch Criteria

| Criterion | Target | Actual | Pass? |
|-----------|--------|--------|-------|
| Time to first interaction | < 3s | _____ | ☐ |
| Task completion rate | > 95% | _____ | ☐ |
| User satisfaction (4+/5) | > 80% | _____ | ☐ |
| Critical bugs found | 0 | _____ | ☐ |
| Accessibility (WCAG AA) | Pass | _____ | ☐ |

### Decision
- [ ] **GO** - Launch to production
- [ ] **NO-GO** - Additional iteration required
- [ ] **SOFT LAUNCH** - Limited rollout with monitoring

**Decision Maker:** _______________
**Date:** _______________
**Notes:**
-
-

---

## 8. Quick Reference: How to Test

### Test Audio Feedback
```javascript
// Open browser DevTools console
testAudioFeedback()  // Plays all 3 sounds sequentially
```

### Test Haptic Feedback
1. Open on mobile device
2. Click connect button
3. Should feel 50ms vibration on success

### Disable Feedback for Testing
```javascript
setAudioFeedback(false)   // Silence sounds
setHapticFeedback(false)  // Disable vibration
```

### Check Current Preferences
```javascript
getFeedbackPreferences()  // Returns { haptics: true, audio: true }
```

### Test Reduced Motion
**macOS:**
1. System Preferences → Accessibility → Display
2. Check "Reduce motion"
3. Reload page - animations should be disabled

**iOS:**
1. Settings → Accessibility → Motion
2. Enable "Reduce Motion"
3. Reload page

**Windows:**
1. Settings → Ease of Access → Display
2. Turn on "Show animations"
3. Reload page

---

## 9. Testing Timeline (Suggested)

### Week 1: Physical Device Testing
- [ ] Day 1-2: iPhone testing (2 devices, iOS 17+)
- [ ] Day 2-3: Android testing (2 devices, Android 13+)
- [ ] Day 3-4: Tablet testing (iPad + Android tablet)
- [ ] Day 4-5: Desktop testing (macOS + Windows)

### Week 2: User Testing
- [ ] Day 1: Recruit 5 participants
- [ ] Day 2-4: Conduct 5 user sessions (1-2 per day)
- [ ] Day 5: Analyze results, compile metrics

### Week 3: Accessibility Testing
- [ ] Day 1-2: VoiceOver testing (macOS + iOS)
- [ ] Day 3: NVDA testing (Windows)
- [ ] Day 4: TalkBack testing (Android)
- [ ] Day 5: Document issues, decide on setStatusLabel() fix

### Week 4: Iteration & Decision
- [ ] Day 1-3: Fix critical issues
- [ ] Day 4: Retest fixes
- [ ] Day 5: Go/No-Go decision

---

## Appendix: Testing URLs

**Local Development:**
- Frontend: http://localhost:47200
- Backend: http://localhost:47300

**Production:**
- Frontend: https://swarmlet.com
- Backend: https://api.swarmlet.com

**Test Parameters:**
- `?autoconnect=1` - Auto-connect on load (skip IDLE state)
- localStorage key: `jarvis.autoconnect` = 'true'

---

**Document Version**: 1.0
**Owner**: [Your Name]
**Next Review**: After each testing phase completes
