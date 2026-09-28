import Foundation
import SwiftUI

typealias SessionPauseQuestionOption = APISessionPauseQuestionOptionResponse

struct SessionPauseQuestion: Codable, Hashable, Sendable {
    let id: String
    let header: String?
    let question: String
    let multiSelect: Bool
    let options: [SessionPauseQuestionOption]
}

struct SessionPauseRequest: Codable, Hashable, Sendable, Identifiable {
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
    let questions: [SessionPauseQuestion]
    let occurredAt: String?
    let lastSeenAt: String?
    let resolvedAt: String?
    let expiresAt: String?

    var isPending: Bool { status == "pending" }
}

struct PauseRequestResponse: Codable, Sendable {
    let status: String
    let pauseRequest: SessionPauseRequest
}
