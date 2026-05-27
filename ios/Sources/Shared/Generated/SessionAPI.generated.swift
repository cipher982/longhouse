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
    let attachImages: Bool?
    let canContinue: Bool?
    let continueTargets: [[String: JSONValue]]?
}

struct APISessionControlResponse: Codable, Hashable, Sendable {
    let sourceRunnerId: Int?
    let sourceRunnerName: String?
    let attachCommand: String?
}

typealias APISessionLoopMode = String

struct APISessionRuntimeDisplayResponse: Codable, Hashable, Sendable {
    let truthTier: String
    let signalTier: String?
    let state: String?
    let tone: String
    let headline: String
    let detail: String?
    let phaseLabel: String
    let compactToolLabel: String?
    let isLive: Bool?
    let isExecuting: Bool?
    let needsAttention: Bool?
    let isIdle: Bool?
    let isStalled: Bool?
    let isManagedLocalTruth: Bool?
    let hasSignal: Bool?
    let controlPath: String?
    let activityRecency: String?
    let lifecycle: String?
    let hostState: String?
    let terminalReason: String?
}

struct APISessionTranscriptPreviewResponse: Codable, Hashable, Sendable {
    let eventId: Int
    let text: String
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
    let runtimeDisplay: APISessionRuntimeDisplayResponse
    let transcriptPreview: APISessionTranscriptPreviewResponse?
    let timelineCard: APITimelineCardPresentationResponse
    let loopMode: APISessionLoopMode?
    let userState: String?
    let launchState: String?
    let launchErrorCode: String?
    let launchErrorMessage: String?
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

struct APIInputOriginResponse: Codable, Hashable, Sendable {
    let authoredVia: String
    let sessionInputId: Int?
    let clientRequestId: String?
}

struct APIEventResponse: Codable, Hashable, Sendable {
    let id: Int
    let role: String
    let contentText: String?
    let rawContentText: String?
    let inputOrigin: APIInputOriginResponse?
    let toolName: String?
    let toolInputJson: [String: JSONValue]?
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
}

struct APISessionProjectionItemResponse: Codable, Hashable, Sendable {
    let kind: String
    let sessionId: String
    let timestamp: String
    let event: APIEventResponse?
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
}

struct APISessionWorkspaceResponse: Codable, Hashable, Sendable {
    let session: APISessionResponse
    let thread: APISessionThreadResponse
    let projection: APISessionProjectionResponse
}

struct APIQueuedInputSummary: Codable, Hashable, Sendable {
    let id: Int
    let text: String
    let intent: String
    let status: String
    let lastError: String?
    let createdAt: String?
}

struct APISessionInputResponse: Codable, Hashable, Sendable {
    let outcome: String
    let inputId: Int
    let clientRequestId: String?
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
