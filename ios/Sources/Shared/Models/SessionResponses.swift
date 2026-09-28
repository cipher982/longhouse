import Foundation
import SwiftUI

struct SessionResumeIntent: Codable, Identifiable, Sendable {
    let sessionId: String
    let provider: String
    let machineId: String?
    let machineLabel: String?
    let cwd: String?
    let available: Bool
    let reason: String?
    let argv: [String]
    let command: String?
    let handoff: String

    var id: String { sessionId }
}

struct SessionThreadResponse: Codable, Sendable {
    let rootSessionId: String
    let headSessionId: String
    let sessions: [SessionDetail]
}

struct SessionAction: Codable, Hashable, Sendable {
    let id: String
    let kind: String
    let provider: String?
    let source: String
    let providerReason: String?
    let eventId: Int?
}

struct SessionProjectionItem: Codable, Identifiable, Sendable {
    let kind: String
    let sessionId: String
    let timestamp: String
    let event: SessionEvent?
    var action: SessionAction? = nil
    let continuedFromSessionId: String?
    let continuationKind: String?
    let originLabel: String?
    let parentOriginLabel: String?
    let parentContinuationKind: String?
    let branchedFromEventId: Int?

    var id: String {
        if kind == "event", let event {
            return "event:\(event.id)"
        }
        if kind == "action", let action {
            return action.id
        }
        return "seam:\(sessionId):\(timestamp)"
    }
}

struct SessionProjectionResponse: Codable, Sendable {
    let rootSessionId: String
    let focusSessionId: String
    let headSessionId: String
    let pathSessionIds: [String]
    let items: [SessionProjectionItem]
    let total: Int
    let pageOffset: Int
    let branchMode: String
    let abandonedEvents: Int
    var generationId: String? = nil
    var nextCursor: String? = nil
    var hasMore: Bool? = nil
}

@propertyWrapper
struct FlexibleStringID: Codable, Hashable, Sendable {
    var wrappedValue: String?

    init(wrappedValue: String?) {
        self.wrappedValue = wrappedValue
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            wrappedValue = nil
        } else if let value = try? container.decode(String.self) {
            wrappedValue = value
        } else if let value = try? container.decode(Int.self) {
            wrappedValue = String(value)
        } else {
            throw DecodingError.typeMismatch(
                String.self,
                .init(codingPath: decoder.codingPath, debugDescription: "Expected string or integer identity")
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(wrappedValue)
    }
}

struct SessionWorkspaceRevision: Codable, Hashable, Sendable {
    @FlexibleStringID var latestEventId: String?
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

struct SessionWorkspaceResponse: Codable, Sendable {
    let session: SessionDetail
    let thread: SessionThreadResponse
    let projection: SessionProjectionResponse
    var workspaceRevision: SessionWorkspaceRevision? = nil

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
    }
}

struct SessionMobileTailResponse: Codable, Sendable {
    let session: SessionDetail
    let projection: SessionProjectionResponse
    @FlexibleStringID var snapshotEventId: String?
    var workspaceRevision: SessionWorkspaceRevision? = nil

    var events: [SessionEvent] {
        projection.items.compactMap(\.event)
    }
}

/// One immutable-render transcript page. `nextCursor` is opaque and already
/// generation-qualified by the Runtime Host; clients must never parse or
/// compare it.
struct SessionEventsPage: Codable, Sendable {
    let v: Int
    let sessionId: String
    let generationId: String
    let events: [SessionEvent]
    let nextCursor: String?
    let hasMore: Bool
    let total: Int
}
