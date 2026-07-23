// @generated from openapi.json by scripts/generate/ios_api_models.py
// Do not edit by hand.
//
// The generated event DTOs intentionally reuse the hand-written JSONValue type.
// Decode with JSONDecoder.snakeCase so tool_input_json payload keys are preserved.

import Foundation

struct APISessionCapabilitiesResponse: Codable, Hashable, Sendable {
    let liveControlAvailable: Bool?
    let hostReattachAvailable: Bool?
    let replyToLiveSessionAvailable: Bool?
    let canQueueNextInput: Bool?
    let canSteerActiveTurn: Bool?
    let displayLabel: String?
    let displayDetail: String?
    let displayTone: String?
    let inputMode: String?
    let defaultInputIntent: String?
    let composerEnabled: Bool?
    let composerPlaceholder: String?
    let composerDisabledReason: String?
    let sendDisabledReason: String?
    let controlLabel: String?
    let observeOnly: Bool?
    let searchOnly: Bool?
    let stalenessReason: String?
    let canSendInput: Bool?
    let canInterrupt: Bool?
    let canTerminate: Bool?
    let canTailOutput: Bool?
    let canResume: Bool?
    let turnState: String?
    let canStartTurn: Bool?
    let startTurnBlockedBy: String?
    let canInterruptActiveTurn: Bool?
    let attachImages: Bool?
}

struct APISessionControlResponse: Codable, Hashable, Sendable {
    let sourceRunnerId: Int?
    let sourceRunnerName: String?
    let attachCommand: String?
}

enum APISessionLoopMode: String, Codable, Hashable, Sendable, CaseIterable {
    case assist = "assist"
    case autopilot = "autopilot"
}

enum APIActivityRecency: String, Codable, Hashable, Sendable, CaseIterable {
    case live = "live"
    case recent = "recent"
    case stale = "stale"
    case none = "none"
}

enum APIControlPath: String, Codable, Hashable, Sendable, CaseIterable {
    case managed = "managed"
    case unmanaged = "unmanaged"
}

enum APIHostState: String, Codable, Hashable, Sendable, CaseIterable {
    case online = "online"
    case stale = "stale"
    case offline = "offline"
    case unknown = "unknown"
}

enum APILifecycle: String, Codable, Hashable, Sendable, CaseIterable {
    case `open` = "open"
    case closed = "closed"
    case unknown = "unknown"
}

enum APIPresenceState: String, Codable, Hashable, Sendable, CaseIterable {
    case thinking = "thinking"
    case running = "running"
    case idle = "idle"
    case needsUser = "needs_user"
    case blocked = "blocked"
    case stalled = "stalled"
}

struct APISessionPauseQuestionOptionResponse: Codable, Hashable, Sendable {
    let label: String
    let description: String?
    let value: String?
}

struct APISessionPauseQuestionResponse: Codable, Hashable, Sendable {
    let id: String
    let header: String?
    let question: String
    let multiSelect: Bool?
    let options: [APISessionPauseQuestionOptionResponse]?
}

struct APISessionPauseRequestProjectionResponse: Codable, Hashable, Sendable {
    let id: String
    let sessionId: String
    let runtimeKey: String
    let kind: String
    let status: String
    let provider: String
    let canRespond: Bool
    let title: String?
    let summary: String?
    let toolName: String?
    let questions: [APISessionPauseQuestionResponse]?
    let occurredAt: String?
    let lastSeenAt: String?
    let resolvedAt: String?
    let expiresAt: String?
}

enum APISignalTier: String, Codable, Hashable, Sendable, CaseIterable {
    case none = "none"
    case phaseSignal = "phase_signal"
    case processBinding = "process_binding"
    case transcriptProgress = "transcript_progress"
}

enum APITerminalReason: String, Codable, Hashable, Sendable, CaseIterable {
    case sessionEnded = "session_ended"
    case userClosed = "user_closed"
    case processGone = "process_gone"
    case hostExpired = "host_expired"
    case providerSignal = "provider_signal"
}

enum APITone: String, Codable, Hashable, Sendable, CaseIterable {
    case stalled = "stalled"
    case blocked = "blocked"
    case running = "running"
    case thinking = "thinking"
    case idle = "idle"
    case active = "active"
    case inactive = "inactive"
    case closed = "closed"
}

enum APITruthTier: String, Codable, Hashable, Sendable, CaseIterable {
    case none = "none"
    case stale = "stale"
    case fresh = "fresh"
    case managedLocal = "managed-local"
}

struct APISessionRuntimeDisplayResponse: Codable, Hashable, Sendable {
    let truthTier: APITruthTier
    let signalTier: APISignalTier
    let state: APIPresenceState?
    let tone: APITone
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
    let controlPath: APIControlPath
    let activityRecency: APIActivityRecency
    let lifecycle: APILifecycle
    let hostState: APIHostState
    let terminalReason: APITerminalReason?
    let pauseRequest: APISessionPauseRequestProjectionResponse?
}

struct APISessionSharerResponse: Codable, Hashable, Sendable {
    let id: Int
    let displayName: String?
}

struct APISessionActivityFacts: Codable, Hashable, Sendable {
    let state: String
    let rawKind: String?
    let tool: String?
    let source: String?
    let observedAt: String?
    let validUntil: String?
}

struct APISessionActionAvailability: Codable, Hashable, Sendable {
    let state: String
    let reason: String?
}

struct APISessionControlActions: Codable, Hashable, Sendable {
    let startTurn: APISessionActionAvailability?
    let sendInput: APISessionActionAvailability
    let interrupt: APISessionActionAvailability
    let terminate: APISessionActionAvailability
    let reattach: APISessionActionAvailability
    let resume: APISessionActionAvailability
}

struct APISessionControlFacts: Codable, Hashable, Sendable {
    let ownership: String
    let connection: String
    let connectionId: JSONValue?
    let leaseGeneration: String?
    let controlPlane: String?
    let observedAt: String?
    let validUntil: String?
    let actions: APISessionControlActions
}

struct APISessionDispositionFacts: Codable, Hashable, Sendable {
    let state: String
    let closedAt: String?
    let closeReason: String?
}

struct APISessionHostFacts: Codable, Hashable, Sendable {
    let state: String
    let observedAt: String?
}

struct APISessionLaunchFacts: Codable, Hashable, Sendable {
    let state: String
    let errorCode: String?
    let errorMessage: String?
}

struct APISessionPendingInteractionFacts: Codable, Hashable, Sendable {
    let id: String
    let kind: String
    let openedAt: String?
    let resolvedAt: String?
    let providerRequestId: String?
    let canRespond: Bool?
}

struct APISessionPresentationLabel: Codable, Hashable, Sendable {
    let key: String
    let label: String
    let tone: String
    let observedAt: String?
}

struct APISessionPresentation: Codable, Hashable, Sendable {
    let primary: APISessionPresentationLabel?
    let access: APISessionPresentationLabel?
    let transcript: APISessionPresentationLabel?
}

struct APISessionRunFacts: Codable, Hashable, Sendable {
    let id: String?
    let lifecycle: String
    let startedAt: String?
    let endedAt: String?
    let endReason: String?
}

struct APISessionTranscriptFacts: Codable, Hashable, Sendable {
    let convergence: String
    let sourceRevision: Int?
    let durableRevision: Int?
    let renderRevision: Int?
    let lastAppendAt: String?
    let searchable: Bool?
    let liveObservation: Bool?
}

struct APISessionStateFacts: Codable, Hashable, Sendable {
    let stateContractVersion: Int?
    let presentationPolicyVersion: Int?
    let mode: String
    let disposition: APISessionDispositionFacts
    let launch: APISessionLaunchFacts?
    let run: APISessionRunFacts?
    let activity: APISessionActivityFacts
    let control: APISessionControlFacts
    let pendingInteraction: APISessionPendingInteractionFacts?
    let transcript: APISessionTranscriptFacts
    let host: APISessionHostFacts
    let presentation: APISessionPresentation
    let commitSeq: Int?
}

struct APISessionTranscriptPreviewResponse: Codable, Hashable, Sendable {
    let eventId: Int
    let text: String
    let role: String?
    let toolName: String?
    let toolInputJson: JSONValue?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: String?
    let eventOrigin: String
    let timestamp: String?
    let isProvisional: Bool
    let isComplete: Bool?
    let contentCursor: String?
    let isStale: Bool?
    let staleReason: String?
}

struct APITimelineBadgePresentationResponse: Codable, Hashable, Sendable {
    let label: String
    let tone: String
}

struct APITimelineStatusPresentationResponse: Codable, Hashable, Sendable {
    let label: String
    let tone: String
    let seenAt: String?
    let seenAtPrefix: String
}

struct APITimelineCardPresentationResponse: Codable, Hashable, Sendable {
    let ownership: APITimelineBadgePresentationResponse
    let status: APITimelineStatusPresentationResponse
    let borderTone: String?
}

struct APISessionResponse: Codable, Hashable, Sendable {
    let id: String
    let originKind: String?
    let provider: String
    let project: String?
    let deviceId: String?
    let environment: String?
    let cwd: String?
    let gitRepo: String?
    let gitBranch: String?
    let startedAt: String
    let endedAt: String?
    let userMessages: Int
    let assistantMessages: Int
    let toolCalls: Int
    let lastActivityAt: String?
    let timelineAnchorAt: String?
    let runtimePhase: String?
    let phaseStartedAt: String?
    let lastProgressAt: String?
    let runtimeSource: String?
    let terminalState: String?
    let runtimeVersion: Int?
    let status: String?
    let presenceState: String?
    let presenceTool: String?
    let presenceUpdatedAt: String?
    let lastLiveAt: String?
    let displayPhase: String?
    let activeTool: String?
    let confidence: String?
    let summary: String?
    let summaryTitle: String?
    let anchorTitle: String?
    let timelineTitle: String?
    let titleState: String?
    let titleSource: String?
    let summaryStatus: String?
    let firstUserMessage: String?
    let matchEventId: Int?
    let matchSnippet: String?
    let matchRole: String?
    let matchScore: Double?
    let threadRootSessionId: String
    let threadHeadSessionId: String
    let threadContinuationCount: Int
    let continuedFromSessionId: String?
    let continuationKind: String?
    let originLabel: String?
    let homeLabel: String?
    let branchedFromEventId: Int?
    let isWritableHead: Bool?
    let isSidechain: Bool?
    let control: APISessionControlResponse?
    let capabilities: APISessionCapabilitiesResponse
    let sessionState: APISessionStateFacts
    let runtimeDisplay: APISessionRuntimeDisplayResponse
    let transcriptPreview: APISessionTranscriptPreviewResponse?
    let timelineCard: APITimelineCardPresentationResponse
    let loopMode: APISessionLoopMode?
    let userState: String?
    let userHiddenFromTimeline: Bool?
    let launchState: String?
    let executionLifetime: String?
    let launchErrorCode: String?
    let launchErrorMessage: String?
    let sharer: APISessionSharerResponse?
}

struct APITimelineSessionCardResponse: Codable, Hashable, Sendable {
    let threadId: String
    let timelineAnchorAt: String?
    let head: APISessionResponse
    let detail: APISessionResponse
    let root: APISessionResponse
    let continuationCount: Int
    let startedOriginLabel: String?
    let headOriginLabel: String?
}

struct APITimelineSessionsListResponse: Codable, Hashable, Sendable {
    let sessions: [APITimelineSessionCardResponse]
    let total: Int
    let hasRealSessions: Bool?
}

struct APISessionThreadResponse: Codable, Hashable, Sendable {
    let rootSessionId: String
    let headSessionId: String
    let sessions: [APISessionResponse]
}

struct APIEventMediaRefResponse: Codable, Hashable, Sendable {
    let sha256: String
    let mediaState: String
    let mimeType: String?
    let byteSize: Int?
    let blobUrl: String
    let thumbUrl: String?
    let sourcePath: String?
    let sourceOffset: Int?
    let jsonPointer: String?
    let originalKind: String
}

struct APIInputOriginResponse: Codable, Hashable, Sendable {
    let authoredVia: String
    let sessionInputId: Int?
    let clientRequestId: String?
}

enum APIToolCallState: String, Codable, Hashable, Sendable, CaseIterable {
    case running = "running"
    case completed = "completed"
    case dropped = "dropped"
}

struct APIEventResponse: Codable, Hashable, Sendable {
    let id: JSONValue
    let role: String
    let contentText: String?
    let rawContentText: String?
    let inputOrigin: APIInputOriginResponse?
    let toolName: String?
    let toolInputJson: JSONValue?
    let toolOutputText: String?
    let toolOutputTruncated: Bool?
    let toolOutputOriginalChars: Int?
    let toolCallId: String?
    let timestamp: String
    let inActiveContext: Bool?
    let branchId: Int?
    let isHeadBranch: Bool?
    let eventOrigin: String?
    let provisionalState: String?
    let provisionalCursor: String?
    let provisionalComplete: Bool?
    let reconciledEventId: Int?
    let toolCallState: APIToolCallState?
    let mediaRefs: [APIEventMediaRefResponse]?
}

struct APITranscriptActionResponse: Codable, Hashable, Sendable {
    let id: String
    let kind: String
    let provider: String?
    let source: String?
    let providerReason: String?
    let eventId: Int?
}

struct APISessionProjectionItemResponse: Codable, Hashable, Sendable {
    let kind: String
    let sessionId: String
    let timestamp: String
    let event: APIEventResponse?
    let action: APITranscriptActionResponse?
    let continuedFromSessionId: String?
    let continuationKind: String?
    let originLabel: String?
    let parentOriginLabel: String?
    let parentContinuationKind: String?
    let branchedFromEventId: Int?
}

struct APISessionProjectionResponse: Codable, Hashable, Sendable {
    let rootSessionId: String
    let focusSessionId: String
    let headSessionId: String
    let pathSessionIds: [String]
    let items: [APISessionProjectionItemResponse]
    let total: Int
    let pageOffset: Int?
    let branchMode: String?
    let abandonedEvents: Int?
    let generationId: String?
    let nextCursor: String?
    let hasMore: Bool?
}

struct APISessionWorkspaceRevisionResponse: Codable, Hashable, Sendable {
    let latestEventId: Int?
    let latestSessionUpdatedAt: String?
    let latestRuntimeSignalAt: String?
    let runtimeVersionSum: Int?
    let pauseRequestCount: Int?
    let pauseRequestFingerprint: String?
    let managedControlCount: Int?
    let managedControlFingerprint: String?
    let livePreviewUpdatedAt: String?
    let threadSessionCount: Int?
    let fingerprint: String
}

struct APISessionWorkspaceResponse: Codable, Hashable, Sendable {
    let session: APISessionResponse
    let thread: APISessionThreadResponse
    let projection: APISessionProjectionResponse
    let workspaceRevision: APISessionWorkspaceRevisionResponse
    let controlOnly: Bool?
}

struct APIConsoleTurnReceiptResponse: Codable, Hashable, Sendable {
    let turnId: String
    let runId: String?
    let state: String
}

struct APIQueuedInputSummary: Codable, Hashable, Sendable {
    let id: Int?
    let liveInputId: String?
    let text: String
    let intent: String
    let status: String
    let lastError: String?
    let createdAt: String?
}

struct APISessionInputResponse: Codable, Hashable, Sendable {
    let outcome: String
    let inputId: Int?
    let liveInputId: String?
    let clientRequestId: String?
    let turn: APIConsoleTurnReceiptResponse?
    let intent: String
    let queued: [APIQueuedInputSummary]?
}

struct APISessionDraftReplyResponse: Codable, Hashable, Sendable {
    let draftText: String
    let model: String
    let generatedAt: String
    let basedOnEventIds: [Int]
}

struct APISessionLoopModeResponse: Codable, Hashable, Sendable {
    let sessionId: String
    let loopMode: APISessionLoopMode
}

struct APISessionTurnTimingResponse: Codable, Hashable, Sendable {
    let submitToSendMs: Int?
    let submitToActiveMs: Int?
    let submitToTerminalMs: Int?
    let activeToTerminalMs: Int?
    let terminalToDurableMs: Int?
    let totalTurnTimeMs: Int?
}

struct APISessionTurnResponse: Codable, Hashable, Sendable {
    let id: Int
    let sessionId: String
    let requestId: String?
    let sessionInputId: Int?
    let state: String
    let terminalPhase: String?
    let errorCode: String?
    let userEventId: Int?
    let durableAssistantEventId: Int?
    let baselineEventId: Int?
    let baselineObservationCursor: Int?
    let userSubmittedAt: String
    let sendAcceptedAt: String?
    let activePhaseObservedAt: String?
    let terminalAt: String?
    let durableAt: String?
    let createdAt: String?
    let updatedAt: String?
    let timing: APISessionTurnTimingResponse
}

struct APISessionTurnsListResponse: Codable, Hashable, Sendable {
    let turns: [APISessionTurnResponse]
    let total: Int
}
