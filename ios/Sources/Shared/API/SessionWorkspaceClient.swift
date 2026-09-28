import Foundation
import OSLog

protocol SessionWorkspaceClient: Sendable {
    /// Lightweight session chrome/state. This is intentionally separate from
    /// the transcript projection so the route can paint its title and controls
    /// while the larger tail is still arriving.
    func sessionDetail(id: String) async throws -> SessionDetail
    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse
    /// Workers this session spawned. Hidden from the timeline by design — a
    /// subagent is a turn artifact, not a session — so this is the route that
    /// keeps them reachable from the work they belong to.
    func sessionSubagents(id: String) async throws -> SessionSubagentsResponse

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse
    func sendInput(id: String, text: String, intent: String, clientRequestId: String) async throws -> SessionInputResponse
    func sendInput(
        id: String,
        text: String,
        intent: String,
        clientRequestId: String,
        model: String?
    ) async throws -> SessionInputResponse
    func sendInputMultipart(
        id: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        clientRequestId: String
    ) async throws -> SessionInputResponse
    func sendInputMultipart(
        id: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        clientRequestId: String,
        model: String?
    ) async throws -> SessionInputResponse
    /// Reads the server-owned receipt for one client request identity. A nil
    /// result means the authority could not confirm a receipt; it is never
    /// interpreted as permission to allocate a new request ID.
    func sessionInputReceipt(id: String, clientRequestId: String) async throws -> SessionInputReceiptState?
    func respondToPauseRequest(
        sessionId: String,
        pauseRequestId: String,
        decision: String,
        answers: [String: [String]]?,
        content: String?,
        message: String?
    ) async throws -> PauseRequestResponse
    func markSessionRead(id: String, readThrough: String) async throws
    func sessionResumeIntent(id: String) async throws -> SessionResumeIntent
    func createSessionBranch(id: String, message: String, clientRequestId: String) async throws -> SessionBranch
    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async
    func postClientDiagnostics(_ payload: ClientDiagnosticsPayload) async
}

extension SessionWorkspaceClient {
    func sendInput(
        id: String,
        text: String,
        intent: String,
        clientRequestId: String,
        model: String?
    ) async throws -> SessionInputResponse {
        try await sendInput(id: id, text: text, intent: intent, clientRequestId: clientRequestId)
    }

    func sendInputMultipart(
        id: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        clientRequestId: String,
        model: String?
    ) async throws -> SessionInputResponse {
        try await sendInputMultipart(
            id: id,
            text: text,
            intent: intent,
            attachments: attachments,
            clientRequestId: clientRequestId
        )
    }


}

extension SessionWorkspaceClient {
    /// Existing fixtures and narrow test doubles can derive the chrome from
    /// their workspace response. The live API overrides this with the
    /// lightweight timeline detail route.
    func sessionDetail(id: String) async throws -> SessionDetail {
        try await sessionWorkspace(id: id, limit: 1, branchMode: "head").session
    }

    /// Older protocol doubles and cached-only fixtures have no dedicated
    /// receipt route. Their session detail still carries the authoritative
    /// recent receipt projection, so use it as the compatibility read.
    func sessionInputReceipt(id: String, clientRequestId: String) async throws -> SessionInputReceiptState? {
        let detail = try await sessionDetail(id: id)
        guard let receipt = detail.inputReceipts?.first(where: {
            $0.clientRequestId == clientRequestId
        }) else {
            return nil
        }
        return SessionInputReceiptState(
            clientRequestId: clientRequestId,
            intent: receipt.intent,
            status: receipt.status,
            // Finding a durable legacy row proves ownership. Its status is
            // carried separately as deliveryStatus.
            disposition: .accepted,
            deliveryStatus: receipt.status,
            eventId: receipt.eventId
        )
    }

    // Mocks/fixtures that never exercise acknowledgement inherit a no-op.
    func markSessionRead(id: String, readThrough: String) async throws {}

    func postClientDiagnostics(_ payload: ClientDiagnosticsPayload) async {}

    func sessionResumeIntent(id: String) async throws -> SessionResumeIntent {
        throw LonghouseAPIError.requestFailed
    }

    func createSessionBranch(id: String, message: String, clientRequestId: String) async throws -> SessionBranch {
        throw LonghouseAPIError.requestFailed
    }

    func respondToPauseRequest(
        sessionId: String,
        pauseRequestId: String,
        decision: String,
        answers: [String: [String]]?,
        content: String?,
        message: String?
    ) async throws -> PauseRequestResponse {
        throw LonghouseAPIError.requestFailed
    }
}

extension SessionWorkspaceClient {
    /// Doubles that do not model worker transcripts report none rather than
    /// forcing every fake to grow a method it has no opinion about.
    func sessionSubagents(id: String) async throws -> SessionSubagentsResponse {
        SessionSubagentsResponse(sessionId: id, children: [])
    }
}
