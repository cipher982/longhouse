import Foundation
import Testing

@testable import Longhouse

/// A branch is deduplicated on its request id, so a retry after a dropped
/// response has to carry the id of the attempt that may have succeeded. A fresh
/// id would start a second branch.
@MainActor
struct SessionBranchRetryTests {
    private func makeModel(_ api: RecordingBranchClient) -> (SessionViewModel, AppState) {
        let appState = AppState()
        appState.serverURL = "https://example.longhouse.ai"
        let model = SessionViewModel(
            apiFactory: { _ in api },
            enableRealtime: false,
            pendingInputStore: PendingInputStore(
                directory: FileManager.default.temporaryDirectory
                    .appendingPathComponent("lh-branch-pending-\(UUID().uuidString)", isDirectory: true)
            )
        )
        return (model, appState)
    }

    @Test
    func retryingTheSameTextReusesTheRequestId() async {
        let api = RecordingBranchClient(failures: 1)
        let (model, appState) = makeModel(api)
        model.branchMessage = "keep going"

        await model.startBranch(sessionId: "s1", appState: appState)
        #expect(model.branchErrorMessage != nil)
        #expect(model.branchMessage == "keep going")
        await model.startBranch(sessionId: "s1", appState: appState)

        let ids = await api.requestIds
        #expect(ids.count == 2)
        #expect(ids[0] == ids[1])
        #expect(model.branchedSessionId == "branch-1")
    }

    @Test
    func editedTextIsANewRequest() async {
        let api = RecordingBranchClient(failures: 2)
        let (model, appState) = makeModel(api)
        model.branchMessage = "one"
        await model.startBranch(sessionId: "s1", appState: appState)
        model.branchMessage = "one two"
        await model.startBranch(sessionId: "s1", appState: appState)

        let ids = await api.requestIds
        #expect(ids.count == 2)
        #expect(ids[0] != ids[1])
    }
}

private actor RecordingBranchClient: SessionWorkspaceClient {
    private var remainingFailures: Int
    private(set) var requestIds: [String] = []

    init(failures: Int) {
        remainingFailures = failures
    }

    func createSessionBranch(id: String, message: String, clientRequestId: String) async throws -> SessionBranch {
        requestIds.append(clientRequestId)
        if remainingFailures > 0 {
            remainingFailures -= 1
            throw URLError(.networkConnectionLost)
        }
        return SessionBranch(
            sessionId: "branch-1",
            threadId: "thread-1",
            turnId: "turn-1",
            runId: nil,
            state: "queued",
            created: true
        )
    }

    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        throw URLError(.cannotConnectToHost)
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse {
        throw URLError(.cannotConnectToHost)
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String) async throws -> SessionInputResponse {
        throw URLError(.cannotConnectToHost)
    }

    func sendInputMultipart(
        id: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        clientRequestId: String
    ) async throws -> SessionInputResponse {
        throw URLError(.cannotConnectToHost)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}
}
