import Foundation

enum SubmittedInputPhase: String, Sendable {
    case submitting
    case working
    case sent
    case queued
    case couldNotConfirm
    case failed
    case needsUserDecision
}

struct SubmittedInput: Identifiable, Sendable {
    let id: String
    let clientRequestId: String
    let text: String
    let intent: String
    var phase: SubmittedInputPhase
    var serverInputId: Int?
    var turnId: String?
    var runId: String?
    var lastError: String?
    let createdAt: Date

    init(
        id: String,
        clientRequestId: String,
        text: String,
        intent: String,
        phase: SubmittedInputPhase,
        serverInputId: Int?,
        turnId: String? = nil,
        runId: String? = nil,
        lastError: String?,
        createdAt: Date
    ) {
        self.id = id
        self.clientRequestId = clientRequestId
        self.text = text
        self.intent = intent
        self.phase = phase
        self.serverInputId = serverInputId
        self.turnId = turnId
        self.runId = runId
        self.lastError = lastError
        self.createdAt = createdAt
    }
}
