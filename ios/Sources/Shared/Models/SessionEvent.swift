import Foundation
import SwiftUI

/// The provider's own accounting for the turn that ended on an event:
/// "Worked for 2m 9s · Turn finished 9:15 AM". Served on the anchor event only.
struct SessionTurnEnd: Codable, Hashable, Sendable {
    let durationMs: Int
    let endedAt: String
    let messageCount: Int?
    /// "completed" or "aborted": Codex reports a stopped turn's duration too.
    var outcome: String? = nil
}


typealias SessionEventMediaRef = APIEventMediaRefResponse

struct SessionEvent: Codable, Identifiable, Sendable {
    /// Durable identity, not chronology. Storage-v2 IDs are opaque strings;
    /// legacy integer IDs decode to their decimal representation.
    let id: String
    /// Opaque generation-qualified paging cursor supplied by storage-v2.
    let cursor: String?
    /// Explicit durable ordering value when the API supplies one.
    let orderTimeUs: Int64?
    let threadId: String?
    let branchKind: String?
    let role: String
    let contentText: String?
    /// Provider-owned status/control records rendered outside the conversation.
    let interactionKind: String?
    let toolName: String?
    let toolInputJSON: [String: JSONValue]?
    /// Lossless provider value. Most tools use an object; Codex custom tools may use a string.
    let toolInputValue: JSONValue?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: ToolCallState?
    let toolPresentation: ToolPresentation?
    let timestamp: String
    let inActiveContext: Bool
    let isHeadBranch: Bool
    let inputOrigin: SessionInputOrigin?
    let eventOrigin: String?
    let mediaRefs: [SessionEventMediaRef]
    /// Set on the event a provider-reported turn ended on.
    let turnEnd: SessionTurnEnd?

    init(
        id: String,
        role: String,
        contentText: String?,
        interactionKind: String? = nil,
        toolName: String?,
        toolInputJSON: [String: JSONValue]?,
        toolInputValue: JSONValue? = nil,
        toolOutputText: String?,
        toolCallId: String?,
        toolCallState: ToolCallState?,
        toolPresentation: ToolPresentation? = nil,
        timestamp: String,
        inActiveContext: Bool,
        isHeadBranch: Bool,
        inputOrigin: SessionInputOrigin?,
        eventOrigin: String? = nil,
        mediaRefs: [SessionEventMediaRef] = [],
        turnEnd: SessionTurnEnd? = nil,
        cursor: String? = nil,
        orderTimeUs: Int64? = nil,
        threadId: String? = nil,
        branchKind: String? = nil
    ) {
        self.id = id
        self.cursor = cursor
        self.orderTimeUs = orderTimeUs
        self.threadId = threadId
        self.branchKind = branchKind
        self.role = role
        self.contentText = contentText
        self.interactionKind = interactionKind
        self.toolName = toolName
        self.toolInputJSON = toolInputJSON
        self.toolInputValue = toolInputValue ?? toolInputJSON.map(JSONValue.object)
        self.toolOutputText = toolOutputText
        self.toolCallId = toolCallId
        self.toolCallState = toolCallState
        self.toolPresentation = toolPresentation
        self.timestamp = timestamp
        self.inActiveContext = inActiveContext
        self.isHeadBranch = isHeadBranch
        self.inputOrigin = inputOrigin
        self.eventOrigin = eventOrigin
        self.mediaRefs = mediaRefs
        self.turnEnd = turnEnd
    }

    /// Source-compatibility convenience while fixtures and the legacy API
    /// still expose integer identities. Callers still observe `id` as String.
    init(
        id: Int,
        role: String,
        contentText: String?,
        interactionKind: String? = nil,
        toolName: String?,
        toolInputJSON: [String: JSONValue]?,
        toolInputValue: JSONValue? = nil,
        toolOutputText: String?,
        toolCallId: String?,
        toolCallState: ToolCallState?,
        toolPresentation: ToolPresentation? = nil,
        timestamp: String,
        inActiveContext: Bool,
        isHeadBranch: Bool,
        inputOrigin: SessionInputOrigin?,
        eventOrigin: String? = nil,
        mediaRefs: [SessionEventMediaRef] = [],
        turnEnd: SessionTurnEnd? = nil,
        cursor: String? = nil,
        orderTimeUs: Int64? = nil,
        threadId: String? = nil,
        branchKind: String? = nil
    ) {
        self.init(
            id: String(id),
            role: role,
            contentText: contentText,
            interactionKind: interactionKind,
            toolName: toolName,
            toolInputJSON: toolInputJSON,
            toolInputValue: toolInputValue,
            toolOutputText: toolOutputText,
            toolCallId: toolCallId,
            toolCallState: toolCallState,
            toolPresentation: toolPresentation,
            timestamp: timestamp,
            inActiveContext: inActiveContext,
            isHeadBranch: isHeadBranch,
            inputOrigin: inputOrigin,
            eventOrigin: eventOrigin,
            mediaRefs: mediaRefs,
            turnEnd: turnEnd,
            cursor: cursor,
            orderTimeUs: orderTimeUs,
            threadId: threadId,
            branchKind: branchKind
        )
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let stringId = (try? container.decode(String.self, forKey: .id))
            ?? (try? container.decode(String.self, forKey: .eventId)) {
            id = stringId
        } else if let integerId = (try? container.decode(Int.self, forKey: .id))
            ?? (try? container.decode(Int.self, forKey: .eventId)) {
            id = String(integerId)
        } else {
            throw DecodingError.keyNotFound(
                CodingKeys.id,
                .init(codingPath: decoder.codingPath, debugDescription: "Expected id or event_id")
            )
        }
        cursor = try container.decodeIfPresent(String.self, forKey: .cursor)
        orderTimeUs = try container.decodeIfPresent(Int64.self, forKey: .orderTimeUs)
        threadId = try container.decodeIfPresent(String.self, forKey: .threadId)
        branchKind = try container.decodeIfPresent(String.self, forKey: .branchKind)
        role = try container.decode(String.self, forKey: .role)
        contentText = try container.decodeIfPresent(String.self, forKey: .contentText)
        interactionKind = try container.decodeIfPresent(String.self, forKey: .interactionKind)
        toolName = try container.decodeIfPresent(String.self, forKey: .toolName)
        toolInputValue = try container.decodeIfPresent(JSONValue.self, forKey: .toolInputJSON)
        toolInputJSON = toolInputValue?.objectValue
        toolOutputText = try container.decodeIfPresent(String.self, forKey: .toolOutputText)
        toolCallId = try container.decodeIfPresent(String.self, forKey: .toolCallId)
        toolCallState = try container.decodeIfPresent(ToolCallState.self, forKey: .toolCallState)
        toolPresentation = try container.decodeIfPresent(ToolPresentation.self, forKey: .toolPresentation)
        timestamp = try container.decode(String.self, forKey: .timestamp)
        inActiveContext = try container.decodeIfPresent(Bool.self, forKey: .inActiveContext)
            ?? (branchKind == nil || branchKind == "head")
        isHeadBranch = try container.decodeIfPresent(Bool.self, forKey: .isHeadBranch)
            ?? (branchKind == nil || branchKind == "head")
        inputOrigin = try container.decodeIfPresent(SessionInputOrigin.self, forKey: .inputOrigin)
        turnEnd = try container.decodeIfPresent(SessionTurnEnd.self, forKey: .turnEnd)
        eventOrigin = try container.decodeIfPresent(String.self, forKey: .eventOrigin)
        mediaRefs = try container.decodeIfPresent([SessionEventMediaRef].self, forKey: .mediaRefs) ?? []
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encodeIfPresent(cursor, forKey: .cursor)
        try container.encodeIfPresent(orderTimeUs, forKey: .orderTimeUs)
        try container.encodeIfPresent(threadId, forKey: .threadId)
        try container.encodeIfPresent(branchKind, forKey: .branchKind)
        try container.encode(role, forKey: .role)
        try container.encodeIfPresent(contentText, forKey: .contentText)
        try container.encodeIfPresent(interactionKind, forKey: .interactionKind)
        try container.encodeIfPresent(toolName, forKey: .toolName)
        try container.encodeIfPresent(toolInputValue, forKey: .toolInputJSON)
        try container.encodeIfPresent(toolOutputText, forKey: .toolOutputText)
        try container.encodeIfPresent(toolCallId, forKey: .toolCallId)
        try container.encodeIfPresent(toolCallState, forKey: .toolCallState)
        try container.encodeIfPresent(toolPresentation, forKey: .toolPresentation)
        try container.encode(timestamp, forKey: .timestamp)
        try container.encode(inActiveContext, forKey: .inActiveContext)
        try container.encode(isHeadBranch, forKey: .isHeadBranch)
        try container.encodeIfPresent(inputOrigin, forKey: .inputOrigin)
        try container.encodeIfPresent(eventOrigin, forKey: .eventOrigin)
        try container.encode(mediaRefs, forKey: .mediaRefs)
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case eventId
        case cursor
        case orderTimeUs
        case threadId
        case branchKind
        case role
        case contentText
        case interactionKind
        case toolName
        case toolInputJSON = "toolInputJson"
        case toolOutputText
        case toolCallId
        case toolCallState
        case toolPresentation
        case timestamp
        case inActiveContext
        case isHeadBranch
        case inputOrigin
        case eventOrigin
        case mediaRefs
        case turnEnd
    }

    var legacyNumericId: Int? { Int(id) }

    var isSynthetic: Bool {
        id.hasPrefix("synthetic:") || (legacyNumericId.map { $0 < 0 } ?? false)
    }

    /// Compare transcript chronology without treating opaque identity or
    /// cursor bytes as sortable. Returns nil if the server has not supplied
    /// enough ordering information and timestamps are equal/unparseable.
    func isOrdered(before other: SessionEvent) -> Bool? {
        if let lhs = orderTimeUs, let rhs = other.orderTimeUs, lhs != rhs {
            return lhs < rhs
        }
        if let lhs = LonghouseDateParser.parse(timestamp),
           let rhs = LonghouseDateParser.parse(other.timestamp),
           lhs != rhs {
            return lhs < rhs
        }
        if let lhs = legacyNumericId, let rhs = other.legacyNumericId, lhs != rhs {
            return lhs < rhs
        }
        return nil
    }

    /// Lookup a top-level key from the tool input JSON as a string.
    func toolInputString(_ key: String) -> String? {
        switch toolInputJSON?[key] {
        case .string(let s): return s
        case .int(let n): return String(n)
        case .double(let n): return String(n)
        case .bool(let b): return String(b)
        case .array, .object, .null, .none: return nil
        }
    }
}
