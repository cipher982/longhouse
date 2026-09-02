import Foundation
import Testing
@testable import Longhouse

/// The activity strip draws exactly what the phone received. These lock the
/// two contracts behind it: how a frame becomes a bar, and that the store
/// never grows past its window.
@MainActor
struct ActivityPulseStoreTests {
    @Test
    func classifiesFramesByWhatTheyCarry() {
        #expect(ActivityPulseStore.classify(toolName: "shell", toolCallState: "running", isProvisional: false) == .toolStart)
        #expect(ActivityPulseStore.classify(toolName: "shell", toolCallState: "completed", isProvisional: false) == .toolResult)
        #expect(ActivityPulseStore.classify(toolName: "shell", toolCallState: nil, isProvisional: true) == .toolResult)
        #expect(ActivityPulseStore.classify(toolName: nil, toolCallState: nil, isProvisional: true) == .textDelta)
        #expect(ActivityPulseStore.classify(toolName: nil, toolCallState: nil, isProvisional: false) == .message)
        #expect(ActivityPulseStore.classify(toolName: nil, toolCallState: nil, isProvisional: nil) == .state)
        #expect(ActivityPulseStore.classify(toolName: "", toolCallState: nil, isProvisional: false) == .message)
    }

    @Test
    func framesWithoutPreviewClassifyByWhatWokeTheServer() {
        #expect(ActivityPulseStore.classify(changeKind: "ingest") == .toolResult)
        #expect(ActivityPulseStore.classify(changeKind: "transcript_preview") == .textDelta)
        #expect(ActivityPulseStore.classify(changeKind: "runtime") == .state)
        #expect(ActivityPulseStore.classify(changeKind: nil) == .state)
        #expect(ActivityPulseStore.classify(changeKind: "read_update") == nil)
        #expect(ActivityPulseStore.classify(changeKind: "title_update") == nil)
    }

    @Test
    func pruneKeepsOnlyTheVisibleWindow() {
        let store = ActivityPulseStore()
        let now = Date()
        store.record(.toolStart, at: now.addingTimeInterval(-40))
        store.record(.textDelta, at: now.addingTimeInterval(-20))
        store.record(.message, at: now.addingTimeInterval(-5))
        store.record(.toolResult, at: now)
        #expect(store.pulses.map(\.kind) == [.message, .toolResult])
    }

    @Test
    func resetEmptiesTheStrip() {
        let store = ActivityPulseStore()
        store.record(.message)
        store.reset()
        #expect(store.pulses.isEmpty)
    }

    @Test
    func boundedUnderABurst() {
        let store = ActivityPulseStore()
        let now = Date()
        for i in 0..<1000 {
            store.record(.textDelta, at: now.addingTimeInterval(-Double(i) / 200))
        }
        #expect(store.pulses.count <= 256)
    }

    @Test
    func boundariesAreTallerThanDeltas() {
        #expect(ActivityPulse.Kind.toolStart.height > ActivityPulse.Kind.toolResult.height)
        #expect(ActivityPulse.Kind.toolResult.height > ActivityPulse.Kind.textDelta.height)
        #expect(ActivityPulse.Kind.textDelta.height > ActivityPulse.Kind.state.height)
    }
}

struct RuntimeElapsedTests {
    @Test
    func preciseRegisterCountsSecondsThenMinutes() {
        #expect(RuntimeElapsed.label(seconds: 0, precise: true) == "0s")
        #expect(RuntimeElapsed.label(seconds: 31.9, precise: true) == "31s")
        #expect(RuntimeElapsed.label(seconds: 72, precise: true) == "1m 12s")
        #expect(RuntimeElapsed.label(seconds: 3_725, precise: true) == "1h 02m")
    }

    @Test
    func coarseRegisterNeverShowsSeconds() {
        #expect(RuntimeElapsed.label(seconds: 12, precise: false) == "now")
        #expect(RuntimeElapsed.label(seconds: 240, precise: false) == "4m")
        #expect(RuntimeElapsed.label(seconds: 7_200, precise: false) == "2h")
        #expect(RuntimeElapsed.label(seconds: 200_000, precise: false) == "2d")
    }

    @Test
    func negativeIntervalsClampToZero() {
        #expect(RuntimeElapsed.label(seconds: -5, precise: true) == "0s")
    }
}
