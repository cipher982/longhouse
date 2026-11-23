import Foundation
import Testing

@testable import LonghouseMenuBarCore

/// The menu bar lists live sessions from every machine on the account
/// (control-plane/docs/specs/macos-menu-bar-state-model.md, decision of
/// 2026-11-23). This Mac's engine stays the source of local rows; other
/// machines' rows and status come from the server.
struct MultiMachineTests {
    private static let collectedAt = "2026-04-21T18:35:00Z"

    private func fixture(_ name: String) throws -> HealthSnapshot {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(name).json")
        return try FixtureHealthSnapshotSource(fileURL: url).load()
    }

    private func snapshot(
        realtime: RealtimeConnectionSnapshot? = nil,
        managedSessions: [ManagedSessionSnapshot] = [],
        remoteSessions: [ManagedSessionSnapshot]? = nil,
        remoteMachines: [RemoteMachineSnapshot]? = nil
    ) -> HealthSnapshot {
        HealthSnapshot(
            schemaVersion: 1, collectedAt: Self.collectedAt,
            healthState: "healthy", severity: "green", headline: "Healthy",
            reasons: [], suggestedActions: [], service: nil, engineStatus: nil,
            outbox: nil, activitySummary: nil, managedSessions: managedSessions,
            realtime: realtime, launchReadiness: nil,
            remoteSessions: remoteSessions, remoteMachines: remoteMachines
        )
    }

    private func projection(
        _ sessionId: String,
        lifecycle: String?,
        mode: String = "helm",
        device: String = "workstation"
    ) -> SessionProjection {
        SessionProjection(
            sessionId: sessionId,
            timelineTitle: "Rebuild the fixtures",
            summaryTitle: nil,
            firstUserMessage: nil,
            titleState: "ready",
            titleSource: "ai",
            runtimePhase: "thinking",
            displayPhase: "Thinking",
            lastActivityAt: Self.collectedAt,
            source: "runtime_host",
            authority: "runtime_host",
            mode: mode,
            presentation: SessionPresentationSnapshot(
                primary: SessionPresentationLabelSnapshot(key: "needs_answer", label: "Needs answer", tone: "neutral"),
                signal: SessionSignalSnapshot(state: "attention", validUntil: nil),
                access: nil
            ),
            deviceId: device,
            provider: "codex",
            project: "longhouse",
            gitBranch: "main",
            runLifecycle: lifecycle
        )
    }

    @Test
    @MainActor
    func twoMachineFixtureListsEveryMachineAndARemoteQuestionTurnsTheIconAmber() throws {
        let snapshot = try fixture("two-machine")
        let date = try #require(HealthSnapshot.parseISO8601(Self.collectedAt))

        // The status item reads this reducer; `.needsUser` is the amber flame.
        let presentation = snapshot.menuBarPresentation(relativeTo: date)
        #expect(presentation.promotion == .needsUser)
        #expect(presentation.needsStatusItemBadge)
        #expect(presentation.headline == "1 session needs you")
        // Another machine's status line never promotes this Mac's health.
        #expect(presentation.systemPromotion == .normal)

        let view = MenuBarPanelView(
            snapshot: snapshot,
            history: [],
            presentationDate: date,
            feedback: nil,
            setFeedback: { _ in },
            actionSink: SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .logOnly),
            isManualRefreshing: false,
            refresh: {}
        )
        let entries = view.hearthEntries
        #expect(entries.map(\.id) == ["sess-remote-ask", "sess-local-api", "sess-remote-build", "sess-local-notes"])
        #expect(entries.map(\.kind) == [.needsYou, .working, .working, .quiet])
        // Rows from another machine lead with its name; this Mac's rows do not.
        #expect(entries[0].subtitle == "workstation · Waiting for your answer")
        #expect(entries[0].provider == "claude")
        #expect(entries[1].subtitle == "acme-api / main · Bash")
        #expect(entries[2].subtitle == "workstation · longhouse / menubar · Thinking")
        #expect(entries[3].subtitle == "notes")
        #expect(entries.allSatisfy { $0.openAction != nil })

        // build-box is offline and was used today; old-mini has been off all
        // day and stays folded; workstation is online.
        #expect(snapshot.remoteMachineNotices.map(\.text) == ["build-box · Offline"])
    }

    @Test
    func otherMachinesRowsFollowTheServedLiveness() throws {
        let base = snapshot()
        let added = base.applyingRemote(projection("s1", lifecycle: "running"))
        let row = try #require(added.remoteSessions?.first)
        #expect(added.remoteSessions?.count == 1)
        #expect(row.isRemote && row.deviceId == "workstation")
        #expect(row.provider == "codex" && row.workspaceLabel == "longhouse" && row.branch == "main")
        #expect(added.managedSessions?.isEmpty == true)
        #expect(added.allSessions.map(\.sessionId) == ["s1"])

        // Replacing, not merging: a newer delta for the same session updates
        // the one row in place.
        #expect(added.applyingRemote(projection("s1", lifecycle: "starting")).remoteSessions?.count == 1)
        // An ended run, a Shadow session and an explicit removal never leave a row.
        #expect(added.applyingRemote(projection("s1", lifecycle: "ended")).remoteSessions?.isEmpty == true)
        #expect(base.applyingRemote(projection("s2", lifecycle: "running", mode: "shadow")).remoteSessions?.isEmpty == true)
        #expect(base.applyingRemote(projection("s3", lifecycle: nil)).remoteSessions?.isEmpty == true)
        #expect(added.removingRemoteSession("s1").remoteSessions?.isEmpty == true)
        // After a reconnect, rows the replay did not mention are gone.
        #expect(added.keepingRemoteSessions([]).remoteSessions?.isEmpty == true)
        #expect(added.keepingRemoteSessions(["s1"]).remoteSessions?.count == 1)
    }

    @Test
    func aLocalRefreshKeepsOtherMachinesUntilTheConnectionChanges() throws {
        let connection = RealtimeConnectionSnapshot(
            runtimeUrl: "https://runtime.example",
            machineName: "laptop",
            tokenPath: "/nonexistent/token"
        )
        let machine = RemoteMachineSnapshot(
            deviceId: "workstation", machineName: "workstation", online: true,
            tone: "live", label: "1 live", hint: nil, quiet: false
        )
        let previous = snapshot(realtime: connection)
            .applyingRemote(projection("s1", lifecycle: "running"))
        var withMachines = previous
        withMachines.remoteMachines = [machine]

        // `local-health` never reports other machines; a refresh must not erase them.
        let refreshed = snapshot(realtime: connection).preservingRealtimeProjection(from: withMachines)
        #expect(refreshed.remoteSessions == withMachines.remoteSessions)
        #expect(refreshed.remoteMachines == [machine])
        #expect(snapshot(realtime: nil).preservingRealtimeProjection(from: withMachines).remoteSessions == nil)

        // A stream that lost its lease keeps the row but claims no live state.
        let stale = withMachines.markingRuntimeHostProjectionUnavailable()
        #expect(stale.remoteSessions?.first?.menuBarAttentionKind(asOf: Date()) == .phaseUnavailable)
        #expect(stale.remoteSessions?.first?.isRemote == true)
        // The disk cache never carries another machine's rows across a launch.
        #expect(withMachines.withoutRemoteState().remoteSessions == nil)
        #expect(withMachines.withoutRemoteState().remoteMachines == nil)
    }

    @Test
    @MainActor
    func onlyAnotherDevicesSessionsCountAsRemote() {
        #expect(SnapshotStore.isOtherMachine("workstation", localDeviceId: "laptop"))
        #expect(!SnapshotStore.isOtherMachine("laptop", localDeviceId: "laptop"))
        #expect(!SnapshotStore.isOtherMachine(nil, localDeviceId: "laptop"))
        // Without this Mac's own id nothing can be told apart.
        #expect(!SnapshotStore.isOtherMachine("workstation", localDeviceId: nil))
    }

    @Test
    func stoppingAnotherMachinesSessionAsksTheServer() async throws {
        let tokenURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-remote-stop-token-\(UUID().uuidString)")
        try Data("device-token\n".utf8).write(to: tokenURL)
        defer { try? FileManager.default.removeItem(at: tokenURL) }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CapturingURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let connection = RealtimeConnectionSnapshot(
            runtimeUrl: "capture://runtime.example",
            machineName: "laptop",
            tokenPath: tokenURL.path
        )
        let target = snapshot(realtime: connection)
            .applyingRemote(projection("sess-remote-stop", lifecycle: "running"))
        let sink = SpyHealthActionSink(logURL: nil, uiURL: nil, effectMode: .live, serverSession: session)

        let feedback = try #require(sink.handleStopManagedBridge(
            sessionID: "sess-remote-stop",
            provider: "codex",
            workspaceLabel: "longhouse",
            snapshot: target
        ))
        #expect(feedback.title == "Stop requested")
        #expect(feedback.detail == "Longhouse asked the server to stop longhouse on workstation.")

        var request: URLRequest?
        for _ in 0..<100 {
            request = CapturingURLProtocol.captured.first(where: { $0.url?.path.hasSuffix("/sess-remote-stop/terminate-live") == true })
            if request != nil { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        let sent = try #require(request)
        #expect(sent.httpMethod == "POST")
        #expect(sent.url?.path == "/api/agents/sessions/sess-remote-stop/terminate-live")
        #expect(sent.value(forHTTPHeaderField: "X-Agents-Token") == "device-token")
    }

    @Test
    func machinesSummaryNamesOtherMachinesAndTheirFaults() throws {
        let body = """
        {"generated_at":"2026-04-21T18:35:00Z","days":1,"utc_offset_minutes":0,
         "first_day":"2026-04-21","last_day":"2026-04-21","machines":[
          {"machine":{"device_id":"laptop","machine_name":"laptop","online":true,"supports":[]},
           "activity":{"sessions_started":3,"live_count":1},"sync":null,
           "status":{"tone":"live","label":"1 live","hint":null,"quiet":false}},
          {"machine":{"device_id":"workstation","machine_name":"Workstation","online":true},
           "status":{"tone":"fault","label":"Needs repair",
                     "hint":"Run longhouse local-health on this machine to inspect the fault","quiet":false}},
          {"machine":{"device_id":"build-box","machine_name":"build-box","online":false},
           "status":{"tone":"off","label":"Offline","hint":null,"quiet":true}}
         ]}
        """
        let machines = try RemoteMachinesClient.decode(Data(body.utf8), excludingDeviceId: "laptop")
        #expect(machines.map(\.deviceId) == ["workstation", "build-box"])
        #expect(machines.first?.displayName == "Workstation")

        let notices = snapshot(remoteMachines: machines).remoteMachineNotices
        #expect(notices.map(\.text) == ["Workstation · Needs repair"])
        #expect(notices.first?.isFault == true)
        #expect(notices.first?.hint == "Run longhouse local-health on this machine to inspect the fault")

        // An offline machine folded as quiet still earns a line while it has
        // rows here: those rows are its last reported state.
        let withRows = snapshot(remoteMachines: machines)
            .applyingRemote(projection("s1", lifecycle: "running", device: "build-box"))
        #expect(withRows.remoteMachineNotices.map(\.text) == ["Workstation · Needs repair", "build-box · Offline"])
        #expect(withRows.remoteMachineNotices.last?.isFault == false)
        // Rows are labelled with the machine's display name.
        #expect(withRows.machineLabel(forDeviceId: "workstation") == "Workstation")
        #expect(withRows.machineLabel(forDeviceId: "unknown-box") == "unknown-box")
    }

    @Test
    func theStreamCarriesEverySessionWithItsMachineAndRun() async throws {
        let tokenURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("longhouse-replay-token-\(UUID().uuidString)")
        try Data("device-token".utf8).write(to: tokenURL)
        defer { try? FileManager.default.removeItem(at: tokenURL) }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReplayStreamURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let stream = SessionProjectionStream.projections(
            connection: RealtimeConnectionSnapshot(
                runtimeUrl: "replay://runtime.example",
                machineName: "laptop",
                tokenPath: tokenURL.path
            ),
            session: session
        )
        let delta = await withTaskGroup(of: SessionProjection?.self) { group -> SessionProjection? in
            group.addTask {
                for await event in stream {
                    if case .delta(let projection) = event { return projection }
                }
                return nil
            }
            group.addTask {
                try? await Task.sleep(for: .seconds(3))
                return nil
            }
            let result = await group.next() ?? nil
            group.cancelAll()
            return result
        }

        let projection = try #require(delta)
        #expect(projection.sessionId == "sess-remote")
        #expect(projection.deviceId == "workstation")
        #expect(projection.provider == "codex")
        #expect(projection.project == "longhouse")
        #expect(projection.gitBranch == "main")
        #expect(projection.runLifecycle == "running")
        #expect(projection.isLiveManagedSession)

        // The request asks for the whole account, not one device.
        let request = try #require(ReplayStreamURLProtocol.captured.earliest)
        let query = URLComponents(url: try #require(request.url), resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(!query.contains { $0.name == "device_id" })
        #expect(request.value(forHTTPHeaderField: "X-Agents-Token") == "device-token")
    }
}

private final class CapturedRequests: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [URLRequest] = []

    func append(_ request: URLRequest) {
        lock.withLock { requests.append(request) }
    }

    func first(where predicate: (URLRequest) -> Bool) -> URLRequest? {
        lock.withLock { requests.first(where: predicate) }
    }

    var earliest: URLRequest? {
        lock.withLock { requests.first }
    }
}

private final class CapturingURLProtocol: URLProtocol, @unchecked Sendable {
    static let captured = CapturedRequests()

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.scheme == "capture"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.captured.append(request)
        guard let client, let url = request.url else { return }
        let response = HTTPURLResponse(
            url: url,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client.urlProtocol(self, didLoad: Data(#"{"terminate_dispatched":true,"session_id":"sess-remote-stop"}"#.utf8))
        client.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// Serves one replayed delta, then leaves the body open like a live stream.
private final class ReplayStreamURLProtocol: URLProtocol, @unchecked Sendable {
    static let captured = CapturedRequests()

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.scheme == "replay"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.captured.append(request)
        guard let client, let url = request.url else { return }
        let response = HTTPURLResponse(
            url: url,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "text/event-stream"]
        )!
        client.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        let delta = #"{"session_id":"sess-remote","device_id":"workstation","provider":"codex","project":"longhouse","git_branch":"main","timeline_title":"Rebuild","source":"runtime_host","authority":"runtime_host","mode":"helm","run":{"id":"run-1","lifecycle":"running"}}"#
        client.urlProtocol(self, didLoad: Data("event: connected\ndata: {}\n\nevent: session_delta\ndata: \(delta)\n\n".utf8))
    }

    override func stopLoading() {}
}
