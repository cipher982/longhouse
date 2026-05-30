import SwiftUI
import XCTest
@testable import Longhouse

/// Locks the redesign's "color is signal" contract: the runtime state dot and
/// the capability label are the only color in the session chrome, and degraded
/// states must stay loud. These map server-driven tone strings (from
/// SessionRuntimeDisplay.tone / runtimeCapabilityTone) to the signal vocabulary.
final class RuntimeChromeStyleTests: XCTestCase {

    // MARK: Runtime dot — executing states are live (green)

    func testRunningIsLive() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "running", capabilityTone: "neutral").dot, .live)
    }

    func testThinkingIsLive() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "thinking", capabilityTone: "neutral").dot, .live)
    }

    // MARK: Runtime dot — blocked is attention (orange), never silently quiet

    func testBlockedIsAttention() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "blocked", capabilityTone: "neutral").dot, .attention)
    }

    // MARK: Runtime dot — idle is quiet, terminal/unknown is dormant

    func testIdleIsIdle() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "idle", capabilityTone: "neutral").dot, .idle)
    }

    func testClosedIsDormant() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "closed", capabilityTone: "neutral").dot, .dormant)
    }

    // active = process-observed alive → live (was wrongly falling to dormant).
    func testActiveIsLive() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "active", capabilityTone: "neutral").dot, .live)
    }

    // stalled is a degraded state — must stay loud (attention), not silent grey.
    func testStalledIsAttention() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "stalled", capabilityTone: "neutral").dot, .attention)
    }

    // inactive = quiet/not-observed → idle, not dormant/offline.
    func testInactiveIsIdle() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "inactive", capabilityTone: "neutral").dot, .idle)
    }

    func testUnknownToneIsDormant() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "wat", capabilityTone: "neutral").dot, .dormant)
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "", capabilityTone: "neutral").dot, .dormant)
    }

    // Every tone the server Tone enum can emit must map explicitly (not dormant
    // by accident). Mirrors session_runtime_display.py Tone.
    func testAllServerTonesMapExplicitly() {
        let expected: [String: RuntimeSignal] = [
            "running": .live, "thinking": .live, "active": .live,
            "blocked": .attention, "stalled": .attention,
            "idle": .idle, "inactive": .idle,
            "closed": .dormant,
        ]
        for (tone, signal) in expected {
            XCTAssertEqual(RuntimeChromeStyle(runtimeTone: tone, capabilityTone: "neutral").dot, signal, tone)
        }
    }

    // MARK: Capability — success shows a live dot, warning stays loud

    func testSuccessCapabilityIsLiveWithDot() {
        let s = RuntimeChromeStyle(runtimeTone: "idle", capabilityTone: "success").capability
        XCTAssertEqual(s, .live)
        XCTAssertTrue(s.showsLiveDot)
    }

    func testWarningCapabilityStaysLoud() {
        let s = RuntimeChromeStyle(runtimeTone: "idle", capabilityTone: "warning").capability
        XCTAssertEqual(s, .warning)
        XCTAssertFalse(s.showsLiveDot)
        XCTAssertEqual(s.color, .orange)
    }

    func testNeutralCapabilityIsMonochrome() {
        let s = RuntimeChromeStyle(runtimeTone: "idle", capabilityTone: "neutral").capability
        XCTAssertEqual(s, .neutral)
        XCTAssertFalse(s.showsLiveDot)
        XCTAssertEqual(s.color, .secondary)
    }

    func testUnknownCapabilityFallsBackToNeutral() {
        XCTAssertEqual(RuntimeChromeStyle(runtimeTone: "idle", capabilityTone: "???").capability, .neutral)
    }

    // MARK: Dot color vocabulary — only four signals, distinct

    func testDotColorsAreDistinctSignals() {
        let colors: [Color] = [
            RuntimeSignal.live.color,
            RuntimeSignal.attention.color,
            RuntimeSignal.idle.color,
            RuntimeSignal.dormant.color,
        ]
        XCTAssertEqual(RuntimeSignal.live.color, .green)
        XCTAssertEqual(RuntimeSignal.attention.color, .orange)
        // Live and attention must never collapse to the same hue.
        XCTAssertNotEqual(colors[0], colors[1])
    }

    // MARK: Independence — runtime tone and capability tone don't bleed

    func testRuntimeAndCapabilityAreIndependent() {
        // A blocked session can still be on a live (success) control path:
        // the dot is attention, the capability is live. Mixing must not occur.
        let style = RuntimeChromeStyle(runtimeTone: "blocked", capabilityTone: "success")
        XCTAssertEqual(style.dot, .attention)
        XCTAssertEqual(style.capability, .live)
    }
}
