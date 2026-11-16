import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

/// What native hands the transcript document. Rows are not built here: the
/// document runs the shared TypeScript presentation model
/// (`web/src/shared/session/model/presentTranscript.ts`) over the served
/// events, so this file carries only state the document cannot know: the
/// optimistic submitted inputs, which lite bodies were loaded, the server URL.
extension WebTranscriptView {
    struct WebTranscriptPayloadInput: Sendable {
        let serverURL: String?
        let projectionItems: [SessionProjectionItem]
        let subagents: [SessionSubagent]
        let submittedInputs: [SubmittedInput]
        let errorMessage: String?
        let contentRevision: UInt64
        let transcriptReadThrough: String?
        let retryRevision: UInt64
        let sourceRevision: Int?
        let sourceOperation: String?
        var liteBodies = LiteBodyState()
    }

    nonisolated static func preparedPayload(
        serverURL: String? = nil,
        projectionItems: [SessionProjectionItem],
        subagents: [SessionSubagent] = [],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        contentRevision: UInt64 = 0,
        transcriptReadThrough: String? = nil,
        retryRevision: UInt64 = 0,
        sourceRevision: Int? = nil,
        sourceOperation: String? = nil,
        liteBodies: LiteBodyState = LiteBodyState()
    ) -> WebTranscriptPreparedPayload {
        let startedAt = Date()
        let data = payloadData(
            serverURL: serverURL,
            projectionItems: projectionItems,
            subagents: subagents,
            submittedInputs: submittedInputs,
            errorMessage: errorMessage,
            liteBodies: liteBodies
        )
        return WebTranscriptPreparedPayload(
            base64: data.base64EncodedString(),
            payloadByteSize: data.count,
            rowCount: projectionItems.count,
            latestItemId: nil,
            payloadFingerprint: Self.payloadFingerprint(data),
            prepareDurationMs: Int(Date().timeIntervalSince(startedAt) * 1000),
            contentRevision: contentRevision,
            transcriptReadThrough: transcriptReadThrough,
            retryRevision: retryRevision,
            sourceRevision: sourceRevision,
            sourceOperation: sourceOperation
        )
    }

    nonisolated static func preparedPayload(
        input: WebTranscriptPayloadInput
    ) -> WebTranscriptPreparedPayload {
        preparedPayload(
            serverURL: input.serverURL,
            projectionItems: input.projectionItems,
            subagents: input.subagents,
            submittedInputs: input.submittedInputs,
            errorMessage: input.errorMessage,
            contentRevision: input.contentRevision,
            transcriptReadThrough: input.transcriptReadThrough,
            retryRevision: input.retryRevision,
            sourceRevision: input.sourceRevision,
            sourceOperation: input.sourceOperation,
            liteBodies: input.liteBodies
        )
    }

    private nonisolated static func payloadFingerprint(_ data: Data) -> String {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in data {
            hash ^= UInt64(byte)
            hash &*= 0x100000001b3
        }
        return String(format: "%016llx", hash)
    }

    /// `{"errorMessage": ..., "projection": {items, subagents, serverUrl, liteBodies, overlay}}`.
    /// The served models are encoded to the wire's snake_case; the envelope and
    /// the overlay rows are the document's own camelCase contract, so the
    /// parts are encoded separately and joined.
    nonisolated static func payloadData(
        serverURL: String?,
        projectionItems: [SessionProjectionItem],
        subagents: [SessionSubagent],
        submittedInputs: [SubmittedInput],
        errorMessage: String?,
        liteBodies: LiteBodyState
    ) -> Data {
        let wire = JSONEncoder.wire
        let plain = JSONEncoder()
        func part<T: Encodable>(_ value: T, _ encoder: JSONEncoder) -> Data {
            (try? encoder.encode(value)) ?? Data("null".utf8)
        }
        var out = Data()
        out.append(Data("{\"errorMessage\":".utf8))
        out.append(part(errorMessage, plain))
        out.append(Data(",\"projection\":{\"items\":".utf8))
        out.append(part(projectionItems, wire))
        out.append(Data(",\"subagents\":".utf8))
        out.append(part(subagents, wire))
        out.append(Data(",\"serverUrl\":".utf8))
        out.append(part(serverURL, plain))
        out.append(Data(",\"liteBodies\":".utf8))
        out.append(part(WebTranscriptLiteBodies(liteBodies), wire))
        out.append(Data(",\"overlay\":".utf8))
        out.append(part(overlayRows(projectionItems: projectionItems, submittedInputs: submittedInputs), plain))
        out.append(Data("}}".utf8))
        return out
    }

    /// Submitted inputs the served transcript does not yet hold. A durable
    /// user input (matched by session input id or client request id) is the
    /// transcript's own row now, so its optimistic twin is dropped.
    nonisolated static func overlayRows(
        projectionItems: [SessionProjectionItem],
        submittedInputs: [SubmittedInput]
    ) -> [WebTranscriptOverlayRow] {
        guard !submittedInputs.isEmpty else { return [] }
        let durable = durableUserInputIdentities(projectionItems)
        return submittedInputs
            .filter { !durable.contains($0) }
            .map(overlayRow)
    }

    private nonisolated static func durableUserInputIdentities(
        _ projectionItems: [SessionProjectionItem]
    ) -> DurableUserInputIdentities {
        var sessionInputIds = Set<Int>()
        var clientRequestIds = Set<String>()
        for item in projectionItems {
            guard let event = item.event,
                  event.role == "user",
                  event.isHeadBranch,
                  let origin = event.inputOrigin else { continue }
            if let sessionInputId = origin.sessionInputId {
                sessionInputIds.insert(sessionInputId)
            }
            if let clientRequestId = origin.clientRequestId, !clientRequestId.isEmpty {
                clientRequestIds.insert(clientRequestId)
            }
        }
        return DurableUserInputIdentities(
            sessionInputIds: sessionInputIds,
            clientRequestIds: clientRequestIds
        )
    }

    private nonisolated static func overlayRow(_ input: SubmittedInput) -> WebTranscriptOverlayRow {
        let row: WebTranscriptOverlayItem
        if input.origin == "wake" || input.origin == "longhouse" {
            row = WebTranscriptOverlayItem(
                id: input.id,
                kind: "providerNotification",
                body: input.text,
                origin: input.origin == "longhouse" ? "longhouse" : nil,
                wake: input.origin == "wake" ? true : nil
            )
        } else {
            row = WebTranscriptOverlayItem(
                id: input.id,
                kind: "submitted",
                role: "user",
                subtitle: submittedStatus(input.phase, lastError: input.lastError),
                body: input.text,
                // A placed receipt is not this phone's to edit or discard: the
                // extra class keeps its styling and drops the row's actions.
                status: input.placedAtSendTime ? "\(input.phase.rawValue) placed" : input.phase.rawValue,
                attachments: input.attachmentSummaries
            )
        }
        return WebTranscriptOverlayRow(
            row: row,
            placedAtSendTime: input.placedAtSendTime,
            createdAtMs: input.createdAt.timeIntervalSince1970 * 1000,
            phase: input.phase.rawValue
        )
    }

    private nonisolated static func submittedStatus(_ phase: SubmittedInputPhase, lastError: String?) -> String {
        switch phase {
        // One vocabulary with web: "Sending…" only while the POST is in
        // flight. A running Console turn means the server has the input, so
        // it reads "Sent"; turn progress belongs to the activity dock.
        case .submitting:
            if lastError == HostLinkCopy.sendQueued { return HostLinkCopy.sendQueued }
            return lastError.map { "Sending… — \($0)" } ?? "Sending…"
        case .working, .sent: return "Sent"
        case .queued: return "Queued · sends after this turn"
        case .couldNotConfirm:
            return lastError == HostLinkCopy.sendQueued ? HostLinkCopy.sendQueued : "Not confirmed"
        case .failed: return lastError.map { "Not delivered — \($0)" } ?? "Not delivered"
        case .needsUserDecision:
            return lastError.map { "Needs choice — \($0)" } ?? "Needs choice"
        }
    }
}

struct DurableUserInputIdentities {
    let sessionInputIds: Set<Int>
    let clientRequestIds: Set<String>

    func contains(_ input: SubmittedInput) -> Bool {
        if let serverInputId = input.serverInputId,
           sessionInputIds.contains(serverInputId) {
            return true
        }
        return clientRequestIds.contains(input.clientRequestId)
    }
}

/// A row native owns and where it stands among the transcript's rows
/// (`OverlayRow` in the document's `types.ts`).
struct WebTranscriptOverlayRow: Encodable {
    let row: WebTranscriptOverlayItem
    let placedAtSendTime: Bool
    let createdAtMs: Double
    let phase: String
}

/// The subset of the document's `TranscriptItem` native fills in.
struct WebTranscriptOverlayItem: Encodable {
    let id: String
    let kind: String
    var role: String? = nil
    var subtitle: String? = nil
    var body: String? = nil
    var status: String? = nil
    var origin: String? = nil
    var wake: Bool? = nil
    /// Bounded attachment metadata; full bytes never cross the WebView boundary.
    var attachments: [SubmittedInputAttachmentSummary]? = nil
    let collapsed = false
    let calls: [String] = []
}

/// Lite bodies loaded so far (`LiteBodiesPayload`), in wire form so the
/// document merges them over the previews its events carry.
struct WebTranscriptLiteBodies: Encodable {
    let bodies: [WebTranscriptEventBody]
    let loading: [String]
    let failed: [String]
    let unavailable: [String]

    init(_ state: LiteBodyState) {
        bodies = state.bodies.values.map(WebTranscriptEventBody.init)
        loading = Array(state.loading)
        failed = Array(state.failed)
        unavailable = Array(state.unavailable)
    }
}

struct WebTranscriptEventBody: Encodable {
    let id: String
    let cursor: String
    let toolInputJson: JSONValue?
    let toolOutputText: String?
    let toolPresentation: ToolPresentation?

    init(_ body: SessionEventBody) {
        id = body.id
        cursor = body.cursor
        toolInputJson = body.toolInputJson
        toolOutputText = body.toolOutputText
        toolPresentation = body.toolPresentation
    }
}
