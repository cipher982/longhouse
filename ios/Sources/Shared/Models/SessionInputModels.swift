import Foundation
import SwiftUI

/// Immediate HTTP handoff and durable record ownership are separate facts.
/// `outcome` remains the former; this disposition is the latter.
enum SessionInputDisposition: String, Codable, Sendable {
    case accepted
    case rejected
    case unknown
}

/// Outcome returned from POST /api/sessions/{id}/input.
///
/// - `sent`: Longhouse dispatched the message to the live session immediately.
/// - `queued`: The session was working; the message is durably queued
///   and will auto-send at the next safe turn boundary.
/// - `unknown`: provider handoff may have happened, but Longhouse cannot
///   confirm delivery; the client must retain the same operation identity.
enum SessionInputOutcome: String, Codable, Sendable {
    case sent
    case queued
    case unknown
}

enum SessionInputIntent: String, Codable, Sendable {
    case auto
    case queue
    case steer
}

enum SessionInputStatus: String, Codable, Sendable {
    case queued
    case delivering
    case delivered
    case cancelled
    case failed
}

struct QueuedInputSummary: Codable, Sendable, Identifiable {
    var id: String {
        liveInputId ?? archiveInputId.map(String.init) ?? text
    }

    let archiveInputId: Int?
    let liveInputId: String?
    /// The identity of the operation that created this row. A message another
    /// sender queued (an agent's `continue`, the web, a second phone) carries one
    /// this client never minted, which is how a queue count is split into what
    /// this phone sent and what it did not.
    let clientRequestId: String?
    let text: String
    let intent: SessionInputIntent
    let status: SessionInputStatus
    let lastError: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case archiveInputId = "id"
        case liveInputId
        case clientRequestId
        case text
        case intent
        case status
        case lastError
        case createdAt
    }

    init(
        id: Int?,
        liveInputId: String? = nil,
        clientRequestId: String? = nil,
        text: String,
        intent: SessionInputIntent,
        status: SessionInputStatus,
        lastError: String?,
        createdAt: String?
    ) {
        self.archiveInputId = id
        self.liveInputId = liveInputId
        self.clientRequestId = clientRequestId
        self.text = text
        self.intent = intent
        self.status = status
        self.lastError = lastError
        self.createdAt = createdAt
    }
}

struct SessionInputResponse: Codable, Sendable {
    let outcome: SessionInputOutcome
    let disposition: SessionInputDisposition
    let deliveryStatus: String?
    let inputId: Int?
    let liveInputId: String?
    let clientRequestId: String?
    let turn: ConsoleTurnReceipt?
    let intent: SessionInputIntent
    let queued: [QueuedInputSummary]

    init(
        outcome: SessionInputOutcome,
        disposition: SessionInputDisposition? = nil,
        deliveryStatus: String? = nil,
        inputId: Int?,
        liveInputId: String? = nil,
        clientRequestId: String?,
        turn: ConsoleTurnReceipt? = nil,
        intent: SessionInputIntent,
        queued: [QueuedInputSummary]
    ) {
        self.outcome = outcome
        self.disposition = disposition ?? (outcome == .unknown ? .unknown : .accepted)
        self.deliveryStatus = deliveryStatus
        self.inputId = inputId
        self.liveInputId = liveInputId
        self.clientRequestId = clientRequestId
        self.turn = turn
        self.intent = intent
        self.queued = queued
    }
    private enum CodingKeys: String, CodingKey {
        case outcome
        case disposition
        case deliveryStatus
        case inputId
        case liveInputId
        case clientRequestId
        case turn
        case intent
        case queued
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let outcome = try container.decode(SessionInputOutcome.self, forKey: .outcome)
        self.init(
            outcome: outcome,
            disposition: try container.decodeIfPresent(SessionInputDisposition.self, forKey: .disposition),
            deliveryStatus: try container.decodeIfPresent(String.self, forKey: .deliveryStatus),
            inputId: try container.decodeIfPresent(Int.self, forKey: .inputId),
            liveInputId: try container.decodeIfPresent(String.self, forKey: .liveInputId),
            clientRequestId: try container.decodeIfPresent(String.self, forKey: .clientRequestId),
            turn: try container.decodeIfPresent(ConsoleTurnReceipt.self, forKey: .turn),
            intent: try container.decode(SessionInputIntent.self, forKey: .intent),
            queued: try container.decodeIfPresent([QueuedInputSummary].self, forKey: .queued) ?? []
        )
    }

    var pendingInputCount: Int {
        queued.filter { $0.status == .queued }.count
    }

    /// Queued messages this client did not submit. The queue is shared by every
    /// sender into a session, so a count that includes an agent's parked message
    /// reads as a phantom: one message sent, "two queued", and only one visible.
    func queuedElsewhereCount(excluding ownClientRequestIds: Set<String>) -> Int {
        queued.filter { row in
            row.status == .queued
                && QueuedInputIndicator.isFromAnotherSender(row.clientRequestId, ownClientRequestIds: ownClientRequestIds)
        }.count
    }

    var visibleFailedInputCount: Int {
        queued.filter { row in
            row.status == .failed && !(row.intent == .steer && row.lastError == "turn_ended")
        }.count
    }

    /// The failures this client's user should be told about: the ones this
    /// client sent. The recent window holds every sender's rows, and another
    /// sender's message that expired unread (an agent's `continue` to a turn that
    /// never ended) is that sender's failure, not "a queued message failed to send"
    /// in this person's composer.
    func visibleFailedInputCount(ownClientRequestIds: Set<String>) -> Int {
        queued.filter { row in
            row.status == .failed
                && !(row.intent == .steer && row.lastError == "turn_ended")
                && !QueuedInputIndicator.isFromAnotherSender(row.clientRequestId, ownClientRequestIds: ownClientRequestIds)
        }.count
    }
}

typealias ConsoleTurnReceipt = APIConsoleTurnReceiptResponse

enum SessionInputAuthoredVia: Codable, Hashable, Sendable {
    case longhouse
    case terminal
    case unknown(String)

    init(serverValue: String) {
        switch serverValue {
        case "longhouse":
            self = .longhouse
        case "terminal":
            self = .terminal
        default:
            self = .unknown(serverValue)
        }
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self.init(serverValue: value)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .longhouse:
            try container.encode("longhouse")
        case .terminal:
            try container.encode("terminal")
        case .unknown(let value):
            try container.encode(value)
        }
    }
}


struct SessionInputReceipt: Codable, Hashable, Sendable {
    let clientRequestId: String?
    let intent: String
    let status: String
    let createdAt: String?
    /// The durable user event this send became, once ingest linked it.
    let eventId: String?
}

/// The composer's "N messages queued" line, read from the served receipts.
///
/// The queue is shared by every sender into a session. Counting it once at send
/// time left the line stale after the queue drained, and counting an agent's
/// parked message beside one bubble read as a phantom ("two queued, I sent one").
enum QueuedInputIndicator {
    static func counts(
        receipts: [SessionInputReceipt],
        ownClientRequestIds: Set<String>
    ) -> (total: Int, elsewhere: Int) {
        let queued = receipts.filter { $0.status == SessionInputStatus.queued.rawValue }
        let elsewhere = queued.filter {
            isFromAnotherSender($0.clientRequestId, ownClientRequestIds: ownClientRequestIds)
        }
        return (queued.count, elsewhere.count)
    }

    /// This app mints every id it sends as "ios-<uuid>", so another phone of
    /// the same person is not another sender; an agent's `continue`, the web and
    /// directed input each mint ids of their own.
    static func isFromAnotherSender(_ clientRequestId: String?, ownClientRequestIds: Set<String>) -> Bool {
        guard let id = clientRequestId else { return true }
        return !(ownClientRequestIds.contains(id) || id.hasPrefix("ios-"))
    }
}

enum SessionInputReceiptDisposition: String, Codable, Sendable {
    case accepted
    case rejected
    case couldNotConfirm
}

/// Structured input errors carry the same operation authority as success
/// responses so the client can distinguish rejection from unknown delivery.
struct SessionInputReceiptState: Codable, Sendable, Equatable {
    let clientRequestId: String
    let intent: String?
    let status: String?
    let disposition: SessionInputReceiptDisposition
    let deliveryStatus: String?
    let inputId: Int?
    let liveInputId: String?
    let turn: ConsoleTurnReceipt?
    let eventId: String?
    let error: String?

    init(
        clientRequestId: String,
        intent: String? = nil,
        status: String? = nil,
        disposition: SessionInputReceiptDisposition,
        deliveryStatus: String? = nil,
        inputId: Int? = nil,
        liveInputId: String? = nil,
        turn: ConsoleTurnReceipt? = nil,
        eventId: String? = nil,
        error: String? = nil
    ) {
        self.clientRequestId = clientRequestId
        self.intent = intent
        self.status = status
        self.disposition = disposition
        self.deliveryStatus = deliveryStatus
        self.inputId = inputId
        self.liveInputId = liveInputId
        self.turn = turn
        self.eventId = eventId
        self.error = error
    }
    private enum CodingKeys: String, CodingKey {
        case clientRequestId
        case intent
        case status
        case disposition
        case deliveryStatus
        case inputId
        case liveInputId
        case turn
        case eventId
        case error
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let status = try container.decodeIfPresent(String.self, forKey: .status)
        let error = try container.decodeIfPresent(String.self, forKey: .error)
        self.init(
            clientRequestId: try container.decode(String.self, forKey: .clientRequestId),
            intent: try container.decodeIfPresent(String.self, forKey: .intent),
            status: status,
            // Legacy receipt rows predate explicit disposition. Their
            // existence proves server-record ownership; status is only the
            // terminal/working delivery state.
            disposition: try container.decodeIfPresent(SessionInputReceiptDisposition.self, forKey: .disposition)
                ?? .accepted,
            deliveryStatus: try container.decodeIfPresent(String.self, forKey: .deliveryStatus)
                ?? status,
            inputId: try container.decodeIfPresent(Int.self, forKey: .inputId),
            liveInputId: try container.decodeIfPresent(String.self, forKey: .liveInputId),
            turn: try container.decodeIfPresent(ConsoleTurnReceipt.self, forKey: .turn),
            eventId: try container.decodeIfPresent(String.self, forKey: .eventId),
            error: error
        )
    }
}

struct SessionInputOrigin: Codable, Hashable, Sendable {
    let authoredVia: SessionInputAuthoredVia
    let sessionInputId: Int?
    let clientRequestId: String?
}
