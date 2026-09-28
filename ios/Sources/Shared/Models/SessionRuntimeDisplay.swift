import Foundation
import SwiftUI

extension SessionRuntimeDisplay {
    /// Synthetic placeholder for SwiftUI previews and widget snapshot fixtures.
    /// Production runtimeDisplay always comes from the server projection.
    static func widgetPlaceholder(
        state: String?,
        phase: String,
        tone: String,
        lifecycle: String = "open"
    ) -> SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: "none",
            signalTier: "none",
            state: state,
            tone: tone,
            headline: phase,
            detail: nil,
            phaseLabel: phase,
            compactToolLabel: nil,
            isLive: false,
            isExecuting: false,
            needsAttention: false,
            isIdle: lifecycle == "closed",
            isStalled: false,
            isManagedLocalTruth: false,
            hasSignal: false,
            controlPath: "unmanaged",
            activityRecency: "none",
            lifecycle: lifecycle,
            hostState: "unknown",
            terminalReason: nil,
            pauseRequest: nil
        )
    }
}

struct SessionRuntimeDisplay: Codable, Hashable, Sendable {
    let truthTier: String
    let signalTier: String
    let state: String?
    let tone: String
    let headline: String
    let detail: String?
    let phaseLabel: String
    let compactToolLabel: String?
    let isLive: Bool
    let isExecuting: Bool
    let needsAttention: Bool
    let isIdle: Bool
    let isStalled: Bool
    let isManagedLocalTruth: Bool
    let hasSignal: Bool
    let controlPath: String
    let activityRecency: String
    let lifecycle: String
    let hostState: String
    let terminalReason: String?
    let pauseRequest: SessionPauseRequest?

    init(
        truthTier: String,
        signalTier: String,
        state: String?,
        tone: String,
        headline: String,
        detail: String?,
        phaseLabel: String,
        compactToolLabel: String?,
        isLive: Bool,
        isExecuting: Bool,
        needsAttention: Bool,
        isIdle: Bool,
        isStalled: Bool,
        isManagedLocalTruth: Bool,
        hasSignal: Bool,
        controlPath: String,
        activityRecency: String,
        lifecycle: String,
        hostState: String,
        terminalReason: String?,
        pauseRequest: SessionPauseRequest? = nil
    ) {
        self.truthTier = truthTier
        self.signalTier = signalTier
        self.state = state
        self.tone = tone
        self.headline = headline
        self.detail = detail
        self.phaseLabel = phaseLabel
        self.compactToolLabel = compactToolLabel
        self.isLive = isLive
        self.isExecuting = isExecuting
        self.needsAttention = needsAttention
        self.isIdle = isIdle
        self.isStalled = isStalled
        self.isManagedLocalTruth = isManagedLocalTruth
        self.hasSignal = hasSignal
        self.controlPath = controlPath
        self.activityRecency = activityRecency
        self.lifecycle = lifecycle
        self.hostState = hostState
        self.terminalReason = terminalReason
        self.pauseRequest = pauseRequest
    }
}
