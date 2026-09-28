import Foundation
import SwiftUI

struct SessionTranscriptPreview: Codable, Hashable, Sendable {
    let eventId: Int
    let text: String
    let role: String?
    let toolName: String?
    let toolInputJSON: [String: JSONValue]?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: ToolCallState?
    let eventOrigin: String
    let timestamp: String?
    let isProvisional: Bool
    let isComplete: Bool?
    let contentCursor: String?
    let isStale: Bool?
    let staleReason: String?

    var shouldRender: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && isStale != true
    }

    var syntheticEvents: [SessionEvent] {
        let callId = toolName == nil
            ? "synthetic:preview:\(eventId)"
            : "synthetic:preview:\(eventId):call"
        let call = SessionEvent(
            id: callId,
            role: role ?? "assistant",
            contentText: toolName == nil ? text : nil,
            toolName: toolName,
            toolInputJSON: toolInputJSON,
            toolOutputText: nil,
            toolCallId: toolCallId,
            toolCallState: toolCallState,
            timestamp: timestamp ?? "",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            eventOrigin: eventOrigin
        )
        guard toolName != nil,
              let toolOutputText,
              toolCallState != .running else {
            return [call]
        }
        let result = SessionEvent(
            id: "synthetic:preview:\(eventId):result",
            role: "tool",
            contentText: nil,
            toolName: toolName,
            toolInputJSON: nil,
            toolOutputText: toolOutputText,
            toolCallId: toolCallId,
            toolCallState: toolCallState,
            timestamp: timestamp ?? "",
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: nil,
            eventOrigin: eventOrigin
        )
        return [call, result]
    }
}

enum TranscriptPreviewProjection {
    static func visibleEvents(
        durableEvents: [SessionEvent],
        preview: SessionTranscriptPreview?
    ) -> [SessionEvent] {
        guard let preview, preview.shouldRender else { return durableEvents }
        let previewText = preview.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !previewText.isEmpty else { return durableEvents }
        guard let previewTimestamp = preview.timestamp else { return durableEvents }

        if let lastDurableAssistant = durableEvents.reversed().first(where: {
            $0.role == "assistant" && ($0.contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        }) {
            let lastText = (lastDurableAssistant.contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if lastText == previewText { return durableEvents }
        }

        if let previewAt = LonghouseDateParser.parse(previewTimestamp),
           let latestEvent = durableEvents.last,
           let latestDurableAt = LonghouseDateParser.parse(latestEvent.timestamp),
           latestDurableAt >= previewAt {
            return durableEvents
        }

        return durableEvents + preview.syntheticEvents
    }
}
