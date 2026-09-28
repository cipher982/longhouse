import Foundation

enum SubmittedInputPhase: String, Sendable, Hashable {
    case submitting
    case working
    case sent
    case queued
    case couldNotConfirm
    case failed
    case needsUserDecision
}

/// Bounded metadata for an optimistic input row. Payload bytes remain in
/// PendingInputStore until terminal delivery confirmation; after that, only a
/// lightweight sent summary remains until the exact transcript echo arrives.
struct SubmittedInputAttachmentSummary: Encodable, Equatable, Sendable {
    let filename: String
    let mimeType: String
    let byteSize: Int

    init(filename: String, mimeType: String, byteSize: Int) {
        self.filename = String(filename.prefix(128))
        self.mimeType = String(mimeType.prefix(80))
        self.byteSize = max(0, byteSize)
    }

    init(_ attachment: ComposerAttachment) {
        self.init(filename: attachment.filename, mimeType: attachment.mimeType, byteSize: attachment.byteSize)
    }

    init(_ attachment: PendingInputIntent.Attachment) {
        self.init(filename: attachment.filename, mimeType: attachment.mimeType, byteSize: attachment.data.count)
    }

    init(_ summary: PendingInputIntent.AttachmentSummary) {
        self.init(filename: summary.filename, mimeType: summary.mimeType, byteSize: summary.byteSize)
    }
}

enum SessionInputSendResult: Sendable, Equatable {
    case persistenceFailed
    case accepted
    case queued
    case unknown
    case rejected

    var shouldRestoreComposer: Bool { self == .persistenceFailed }

    var isSuccessfulHandoff: Bool {
        switch self {
        case .accepted, .queued: return true
        case .persistenceFailed, .unknown, .rejected: return false
        }
    }
}

struct SubmittedInput: Identifiable, Sendable {
    let id: String
    let clientRequestId: String
    let text: String
    let intent: String
    let attachmentSummaries: [SubmittedInputAttachmentSummary]
    var phase: SubmittedInputPhase
    var serverInputId: Int?
    var liveInputId: String?
    var turnId: String?
    var runId: String?
    var deliveryStatus: String?
    var lastError: String?
    let createdAt: Date

    init(
        id: String,
        clientRequestId: String,
        text: String,
        intent: String,
        attachmentSummaries: [SubmittedInputAttachmentSummary] = [],
        phase: SubmittedInputPhase,
        serverInputId: Int?,
        liveInputId: String? = nil,
        turnId: String? = nil,
        runId: String? = nil,
        deliveryStatus: String? = nil,
        lastError: String?,
        createdAt: Date
    ) {
        self.id = id
        self.clientRequestId = clientRequestId
        self.text = text
        self.intent = intent
        self.attachmentSummaries = attachmentSummaries
        self.phase = phase
        self.serverInputId = serverInputId
        self.liveInputId = liveInputId
        self.turnId = turnId
        self.runId = runId
        self.deliveryStatus = deliveryStatus
        self.lastError = lastError
        self.createdAt = createdAt
    }
}

struct TurnEndedInput: Equatable, Sendable {
    let clientRequestId: String
    let text: String
}
