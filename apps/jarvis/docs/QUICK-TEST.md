# Quick Local Sanity Check

**5-Minute Validation** - Run this before physical device testing

**🆕 Voice/Text Separation Features (Phase 11)**:
- Hands-free mode toggle for continuous voice listening
- Text input now properly mutes voice channel (no VAD activation during typing)
- Ambient noise won't trigger transcription when using text input
- Test 6 & 7 validate these new capabilities

---

## Prerequisites

```bash
# From /Users/davidrose/git/zerg
make jarvis-dev
```

Open: http://localhost:47200

---

## Test 1: Visual State Machine (30 seconds)

1. ✅ **IDLE state visible?**
   - Purple button with steady glow
   - "Tap to speak" label below button

2. ✅ **Click to connect**
   - Button shows pulsing animation
   - "Connecting..." label appears
   - Toast notification: "Connected successfully"
   - Hear chime? (if audio enabled)
   - Feel vibration? (if on mobile)

3. ✅ **READY state visible?**
   - Green glow appears
   - Subtle breathing animation (3s cycle)
   - "Ready to talk" label

---

## Test 2: Push-to-Talk (30 seconds)

1. ✅ **Press and hold button**
   - Button turns pink
   - Scale increases slightly
   - "Listening..." label
   - Microphone icon changes to stop icon

2. ✅ **Speak while holding**
   - "You" bubble appears with "Listening…" placeholder
   - Radial visualizer responds to voice

3. ✅ **Release button**
   - Returns to READY state (green)
   - Your speech appears as finalized text

4. ✅ **Assistant responds**
   - Button turns blue
   - "Assistant is responding" label
   - Response streams in

5. ✅ **Returns to READY**
   - Green glow returns
   - Ready for next turn

---

## Test 3: Keyboard Navigation (1 minute)

1. ✅ **Tab to button**
   - Purple focus ring appears
   - Focus ring visible and clear

2. ✅ **Press Space key**
   - Connects (if IDLE)
   - Or activates PTT (if READY)
   - Hear chime?

3. ✅ **Hold Space while connected**
   - Button turns pink (SPEAKING)
   - "Listening..." appears
   - No page scroll (Space prevented)

4. ✅ **Release Space**
   - Returns to READY (green)
   - Speech sent

5. ✅ **Try Enter key**
   - Same behavior as Space

---

## Test 4: Audio Feedback (30 seconds)

Open browser console (Cmd+Option+J / F12):

```javascript
// Test all sounds
testAudioFeedback()
// Should hear: chime → tick → error tone

// Check preferences
getFeedbackPreferences()
// Should show: { haptics: true, audio: true }

// Disable audio
setAudioFeedback(false)

// Re-enable
setAudioFeedback(true)
```

---

## Test 5: Reduced Motion (30 seconds)

**macOS:**
1. System Preferences → Accessibility → Display → Reduce motion
2. Reload page
3. ✅ All animations disabled?
4. ✅ States still change colors?
5. ✅ Interface still usable?

**Skip if not on macOS** - will test on other platforms during device testing

---

## Test 6: Hands-Free Mode (1 minute)

1. ✅ **Connect to service**
   - Click microphone button
   - Wait for "Ready to talk" status
   - Hands-free toggle should become enabled

2. ✅ **Enable hands-free mode**
   - Toggle "Hands-free" switch ON
   - Status updates to "Voice listening continuously"
   - Button shows green (READY) state

3. ✅ **Speak without button press**
   - Just start talking (no button press needed)
   - Button turns pink when VAD detects speech
   - Transcription appears
   - Assistant responds

4. ✅ **Disable hands-free mode**
   - Toggle "Hands-free" switch OFF
   - Status clears (shows default message)
   - Button returns to normal PTT behavior

---

## Test 7: Ambient Noise While Typing (1 minute)

**Purpose**: Verify that background audio doesn't trigger voice mode when typing text messages

1. ✅ **Connect to service**
   - Click microphone button to establish connection
   - Wait for "Ready to talk"

2. ✅ **Type message with ambient noise**
   - Play background audio/music or have someone talking nearby
   - Type a message in the text input field
   - Press Send or hit Enter

3. ✅ **Verify no voice activation**
   - Message sends successfully
   - No "Listening..." bubble appears
   - No voice transcription triggered by ambient noise
   - Assistant responds to text message

4. ✅ **Confirm voice still works after**
   - Press and hold microphone button
   - Speak a message
   - Verify voice input still works normally

---

## Test 8: Screen Reader Preview (1 minute)

**macOS only** (for quick check):

1. Enable VoiceOver: `Cmd+F5`
2. Navigate to button: `VO+Right arrow`
3. ✅ Announces: "Connect to voice service, button"
4. Activate: `VO+Space`
5. ✅ Announces: "Connecting..., button, busy"
6. ✅ Announces: "Ready to talk" (after connection)
7. Disable VoiceOver: `Cmd+F5`

**Full screen reader testing** will happen in Phase 10 proper

---

## Expected Results

### All Tests Pass ✅
- States transition smoothly
- Keyboard navigation works
- Audio feedback plays
- Focus indicators visible
- Screen reader announces states
- Reduced motion works

### Ready for: Physical device testing

---

## If Something Fails

### Button doesn't appear / stuck loading
```bash
# Check backend is running
curl http://localhost:47300/api/session
# Should return JSON with session token

# Check frontend console for errors
# Open DevTools → Console tab
```

### No audio feedback
```javascript
// Check if disabled
getFeedbackPreferences()

// Re-enable
setAudioFeedback(true)

// Test
testAudioFeedback()
```

### Keyboard navigation not working
- Check browser console for JavaScript errors
- Try clicking with mouse first to ensure connection works
- Verify button has focus (should see focus ring)

### States not changing
- Check browser console for errors
- Look for state transition logs: "Voice button state transition: idle → connecting"

---

## Next Steps After Local Check

1. ✅ **If all tests pass:** Proceed to physical device testing
2. ⚠️ **If issues found:** Fix before device testing
3. 📋 **Document observations:** Note anything that feels off

---

**Time to complete:** ~5 minutes
**Prerequisites:** Jarvis running locally
**Goal:** Verify all 9 phases work together before real device testing
