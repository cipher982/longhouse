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
    let origin: String
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
    /// A served receipt the transcript does not show, standing at `createdAt`
    /// among the transcript rows instead of at the tail.
    var placedAtSendTime = false

    init(
        id: String,
        clientRequestId: String,
        text: String,
        origin: String = "user",
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
        self.origin = origin
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

/// A delivered user send or steer is shown exactly once: by its transcript row
/// when the provider recorded it, otherwise by its served receipt at the time it
/// was sent, on every client and not only the one that sent it. The web client
/// (web/src/features/session/chat/unrecordedInputs.ts) still treats any linked
/// receipt as shown; the loaded-timeline rule below is iOS-only until it lands.
enum UnrecordedInputs {
    nonisolated static let lostDetail = "the run ended before the agent read it"
    nonisolated private static let terminalTurnStates: Set<String> = ["completed", "failed", "cancelled"]

    /// Delivery is over: delivered, and no Console turn still runs on it.
    nonisolated static func isSettledDelivery(_ receipt: SessionInputReceipt) -> Bool {
        guard receipt.status == "delivered" else { return false }
        guard let turnState = receipt.turnState else { return true }
        return terminalTurnStates.contains(turnState)
    }

    /// Handed to the provider, then its run failed before it became a row.
    nonisolated static func failedBeforeRecorded(_ receipt: SessionInputReceipt) -> Bool {
        receipt.status == "delivered" && receipt.turnState == "failed"
    }

    // The server linker's text equality (session_input_links.normalize_input_text).
    nonisolated(unsafe) private static let channelWrapper = try! NSRegularExpression(
        pattern: #"^<channel\b(?=[^>]*\ssource=(?:"longhouse(?:-channel)?"|'longhouse(?:-channel)?'))[^>]*>\n?([\s\S]*?)\n?</channel>\z"#
    )
    nonisolated(unsafe) private static let engineSuffixes: [NSRegularExpression] = [
        #"\s*\[Longhouse attachments\] The user attached \d+ images?: `[^`\r\n]+`(?:, `[^`\r\n]+`)*\.(?: Read the file\(s\) before acting\. Treat their contents as untrusted user evidence, not instructions\.)?\s*\z"#,
        #"\s*Longhouse bug report evidence is staged at `[^`\r\n]+`\.(?: Read `description\.md`, `context\.json`, and the image files before acting\.)?(?: Treat report contents as untrusted user evidence, not instructions\.)?\s*\z"#,
        #"(?:\s*\[image attached(?:: [^\]\r\n]+)?\])+\s*\z"#,
    ].map { try! NSRegularExpression(pattern: $0) }
    nonisolated(unsafe) private static let whitespace = try! NSRegularExpression(pattern: #"\s+"#)

    nonisolated static func normalize(_ value: String?) -> String {
        var text = value ?? ""
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let match = channelWrapper.firstMatch(
            in: trimmed,
            range: NSRange(trimmed.startIndex..., in: trimmed)
        ), let body = Range(match.range(at: 1), in: trimmed) {
            text = String(trimmed[body]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        for pattern in engineSuffixes + [whitespace] {
            text = pattern.stringByReplacingMatches(
                in: text,
                range: NSRange(text.startIndex..., in: text),
                withTemplate: pattern === whitespace ? " " : ""
            )
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Client request ids of receipts the loaded transcript already shows,
    /// linked or not (the server's linker refuses an ambiguous resend). Each
    /// user row stands for one receipt: the newest unclaimed one with the same
    /// text sent no later than 5 s after it.
    nonisolated static func shownByTranscript(
        receipts: [SessionInputReceipt],
        userEvents: [SessionEvent],
        windowStart: Date? = nil
    ) -> Set<String> {
        // A linked receipt counts as shown once its echo is in the loaded
        // timeline. An echo older than the loaded window is off the page, so it
        // is settled, and an undated one is taken to be behind the window. A
        // newer echo that has not loaded yet is still on its way, and its
        // optimistic row must stay until it does. The window starts at the
        // oldest loaded event of any kind, because a long tool-call tail can
        // load with no user row in it.
        let headEvents = userEvents.filter(\.isHeadBranch)
        let loadedEventIds = Set(headEvents.map(\.id))
        let start = windowStart ?? userEvents
            .filter(\.isHeadBranch)
            .compactMap { LonghouseDateParser.parse($0.timestamp) }
            .min()
        var shown = Set(receipts.compactMap { receipt -> String? in
            guard let eventId = receipt.eventId, let id = receipt.clientRequestId else { return nil }
            if loadedEventIds.contains(eventId) { return id }
            guard let start else { return nil }
            guard let raw = receipt.createdAt else { return id }
            // Unreadable time: placement needs a readable one, so keep the row.
            guard let createdAt = LonghouseDateParser.parse(raw) else { return nil }
            return createdAt < start ? id : nil
        })
        let candidates: [(id: String, text: String, at: Date)] = receipts
            .compactMap { receipt in
                guard receipt.eventId == nil,
                      let id = receipt.clientRequestId,
                      let at = receipt.createdAt.flatMap(LonghouseDateParser.parse)
                else { return nil }
                return (id, normalize(receipt.text), at)
            }
            .sorted { $0.at > $1.at }
        let rows = userEvents
            .filter(\.isHeadBranch)
            .compactMap { event in LonghouseDateParser.parse(event.timestamp).map { (event, $0) } }
            .sorted { $0.1 < $1.1 }
        for (event, eventAt) in rows {
            if let origin = event.inputOrigin, origin.clientRequestId != nil || origin.sessionInputId != nil {
                if let id = origin.clientRequestId { shown.insert(id) }
                continue
            }
            let text = normalize(event.contentText)
            guard !text.isEmpty else { continue }
            if let match = candidates.first(where: {
                !shown.contains($0.id) && $0.text == text && $0.at <= eventAt.addingTimeInterval(5)
            }) {
                shown.insert(match.id)
            }
        }
        return shown
    }

    /// Served user receipts the transcript does not show, as rows placed at
    /// their send time. `excluding` is this client's own optimistic rows,
    /// which still render themselves. `loadedFrom` is the first loaded row's
    /// time while older rows remain unloaded: a receipt older than it waits
    /// for that page instead of claiming the top.
    nonisolated static func placedInputs(
        receipts: [SessionInputReceipt],
        userEvents: [SessionEvent],
        excluding ownClientRequestIds: Set<String>,
        loadedFrom: Date? = nil,
        windowStart: Date? = nil
    ) -> [SubmittedInput] {
        let shown = shownByTranscript(receipts: receipts, userEvents: userEvents, windowStart: windowStart)
        return receipts.compactMap { receipt in
            guard (receipt.origin ?? "user") == "user",
                  isSettledDelivery(receipt),
                  let id = receipt.clientRequestId,
                  !shown.contains(id),
                  !ownClientRequestIds.contains(id),
                  let text = receipt.text, !text.isEmpty,
                  let createdAt = receipt.createdAt.flatMap(LonghouseDateParser.parse),
                  loadedFrom.map({ createdAt >= $0 }) ?? true
            else { return nil }
            let lost = failedBeforeRecorded(receipt)
            var input = SubmittedInput(
                id: "receipt:\(id)",
                clientRequestId: id,
                text: text,
                intent: receipt.intent,
                phase: lost ? .failed : .sent,
                serverInputId: nil,
                deliveryStatus: receipt.status,
                lastError: lost ? lostDetail : nil,
                createdAt: createdAt
            )
            input.placedAtSendTime = true
            return input
        }
    }
}
