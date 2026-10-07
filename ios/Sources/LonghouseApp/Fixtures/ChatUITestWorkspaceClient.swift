#if DEBUG
import SwiftUI
import UIKit

struct ChatUITestFixture: Sendable {
    let name: String
    let eventCount: Int
    let replayPath: String?

    init(name: String) {
        self.name = name
        replayPath = name == "replay-file" ? UITestHooks.chatFixtureReplayPath : nil
        let defaultCount: Int
        switch name {
        case "stress": defaultCount = 500
        case "benchmark-core": defaultCount = TranscriptBenchmarkTrace.initialRowCount
        default: defaultCount = 80
        }
        eventCount = max(0, UITestHooks.chatFixtureEventCount ?? defaultCount)
    }

    var usesRealtimeStream: Bool {
        name.hasPrefix("assistant-stream") || name == "console-reconcile"
    }
}

actor ChatUITestWorkspaceClient: SessionWorkspaceClient {
    let sessionID: String
    private let fixtureName: String
    static let endedCodexHelmChildID = "ui-test-codex-helm-child"
    private var nextEventID = 1
    private var events: [SessionEvent]
    private var realtimeContinuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation?
    private var streamingAssistantEventID: Int?

    init(fixture: ChatUITestFixture, sessionID: String = "ui-test-chat-session") {
        self.sessionID = sessionID
        self.fixtureName = fixture.name
        var seedEvents: [SessionEvent] = []
        if let replayPath = fixture.replayPath {
            // A replay run must exercise the actual exported transcript. If the
            // file is missing/unreadable/malformed, surface it loudly rather
            // than silently falling back to synthetic data (which would let a
            // replay QA run pass without the replay).
            if let replayEvents = Self.loadReplayEvents(path: replayPath) {
                seedEvents = replayEvents
            } else {
                seedEvents = [Self.makeEvent(
                    id: 1,
                    role: "assistant",
                    content: "⚠️ Replay fixture failed to load from \(replayPath). Check LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH and the export schema.",
                    timestamp: Self.fixedTimestamp(offset: 0)
                )]
            }
        } else if fixture.name == "benchmark-core" {
            seedEvents = TranscriptBenchmarkTrace.initialEvents()
        } else if fixture.name == "tools" {
            seedEvents = Self.toolFixtureEvents()
        } else if fixture.name == "provider-notification" {
            seedEvents = Self.providerNotificationFixtureEvents()
        } else if fixture.name == "marketing" {
            seedEvents = Self.marketingFixtureEvents()
        } else if fixture.name == "lite-bodies" {
            seedEvents = Self.liteBodiesFixtureEvents()
        } else {
            // Every assistant reply ends a turn, and the provider stamps each
            // one; the fixture carries that so the footer is part of what the
            // captures and UI tests see. Benchmarks keep the bare shape.
            let stampsTurns = !fixture.name.hasPrefix("benchmark") && fixture.name != "render-storm"
            for index in 0..<fixture.eventCount {
                let role = index.isMultiple(of: 2) ? "user" : "assistant"
                let timestamp = Self.fixedTimestamp(offset: index)
                seedEvents.append(Self.makeEvent(
                    id: index + 1,
                    role: role,
                    content: Self.messageText(index: index, role: role, fixtureName: fixture.name),
                    timestamp: timestamp,
                    turnEnd: role == "assistant" && stampsTurns
                        ? SessionTurnEnd(durationMs: 129_299, endedAt: timestamp, messageCount: nil)
                        : nil
                ))
            }
        }
        events = seedEvents
        nextEventID = (seedEvents.compactMap(\.legacyNumericId).max() ?? 0) + 1
    }

    func sessionDetail(id: String) async throws -> SessionDetail {
        if let delayMs = UITestHooks.mobileDetailDelayMs, delayMs > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
        }
        return Self.makeDetail(
            sessionID: id,
            events: events,
            title: Self.titleForSession(fixtureName: fixtureName, sessionID: id)
        )
    }

    func sessionResumeIntent(id: String) async throws -> SessionResumeIntent {
        guard fixtureName == "ended-codex-helm" else {
            throw LonghouseAPIError.requestFailed
        }
        return SessionResumeIntent(
            sessionId: id,
            provider: "codex",
            machineId: "cinder",
            machineLabel: "cinder",
            cwd: "/Users/example/code",
            available: true,
            reason: nil,
            argv: ["longhouse", "codex", "--cwd", "/Users/example/code", "--resume-session", id],
            command: "longhouse codex --cwd /Users/example/code --resume-session \(id)",
            handoff: "terminal_command"
        )
    }

    func createSessionBranch(
        id: String,
        message: String,
        clientRequestId: String
    ) async throws -> SessionBranch {
        guard fixtureName == "ended-codex-helm", !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw LonghouseAPIError.requestFailed
        }
        return SessionBranch(
            sessionId: Self.endedCodexHelmChildID,
            threadId: "ui-test-codex-helm-child-thread",
            turnId: "ui-test-codex-helm-child-turn",
            runId: "ui-test-codex-helm-child-run",
            state: "created",
            created: true
        )
    }

    private static func titleForSession(fixtureName: String, sessionID: String) -> String {
        guard fixtureName == "ended-codex-helm",
              sessionID == endedCodexHelmChildID else {
            return titleForFixture(fixtureName)
        }
        return "Codex Helm branch"
    }


    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse {
        Self.makeWorkspace(
            sessionID: id,
            events: events,
            title: Self.titleForSession(fixtureName: fixtureName, sessionID: id)
        )
    }

    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: String?,
        cursor: String?
    ) async throws -> SessionMobileTailResponse {
        if let delayMs = UITestHooks.mobileTailDelayMs, delayMs > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
        }
        let page = fixtureName == "benchmark-core"
            ? (events: events, pageOffset: 0)
            : Self.tailPage(events: events, limit: limit, offset: offset)
        return Self.makeMobileTail(
            sessionID: id,
            events: page.events,
            total: events.count,
            pageOffset: page.pageOffset,
            snapshotEventId: events.compactMap(\.legacyNumericId).max().map(String.init),
            title: Self.titleForSession(fixtureName: fixtureName, sessionID: id)
        )
    }

    func sendInput(id: String, text: String, intent: String, clientRequestId: String) async throws -> SessionInputResponse {
        let delay = fixtureName == "helm-channel-reconcile" ? 20_000_000_000 : 650_000_000
        try await Task.sleep(nanoseconds: UInt64(delay))
        let inputID = nextEventID
        if fixtureName != "console-sent-unlinked" {
            events.append(Self.makeEvent(
                id: nextEventID,
                role: "user",
                content: text,
                timestamp: ISO8601DateFormatter().string(from: Date()),
                // The storage boundary strips Claude channel framing, then links
                // this event to the accepted receipt by client_request_id.
                inputOrigin: SessionInputOrigin(
                    authoredVia: .longhouse,
                    sessionInputId: ["console-reconcile", "helm-channel-reconcile"].contains(fixtureName)
                        ? nil
                        : inputID,
                    clientRequestId: clientRequestId
                )
            ))
            nextEventID += 1
        }
        if fixtureName == "console-reconcile" {
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                self.appendAssistantMessage("Console fixture durable reply.")
                self.emitWorkspaceChanged()
            }
            return SessionInputResponse(
                outcome: .sent,
                inputId: nil,
                liveInputId: nil,
                clientRequestId: clientRequestId,
                turn: ConsoleTurnReceipt(
                    turnId: "fixture-turn",
                    receiptId: nil,
                    runId: "fixture-run",
                    state: "active",
                    origin: "user",
                    isFresh: true
                ),
                intent: .auto,
                queued: []
            )
        }
        if fixtureName == "console-sent-unlinked" {
            return SessionInputResponse(
                outcome: .sent,
                disposition: .accepted,
                inputId: nil,
                liveInputId: nil,
                clientRequestId: clientRequestId,
                turn: ConsoleTurnReceipt(
                    turnId: "fixture-completed-turn",
                    receiptId: nil,
                    runId: "fixture-completed-run",
                    state: "completed",
                    origin: "user",
                    isFresh: true
                ),
                intent: .auto,
                queued: []
            )
        }
        return SessionInputResponse(
            outcome: .sent,
            inputId: inputID,
            liveInputId: nil,
            clientRequestId: clientRequestId,
            intent: SessionInputIntent(rawValue: intent) ?? .auto,
            queued: []
        )
    }

    func sendInputMultipart(
        id: String,
        text: String,
        intent: String,
        attachments: [ComposerAttachment],
        clientRequestId: String
    ) async throws -> SessionInputResponse {
        try await sendInput(id: id, text: text, intent: intent, clientRequestId: clientRequestId)
    }

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {}

    nonisolated func streamSource() -> SessionWorkspaceStreamSource {
        SessionWorkspaceStreamSource(
            start: { self.startRealtimeStream() },
            stop: { await self.stopRealtimeStream() },
            clockSkewMs: { 0 }
        )
    }

    nonisolated func startRealtimeStream() -> AsyncStream<SessionWorkspaceStream.Event> {
        AsyncStream { continuation in
            Task {
                await self.attachRealtimeContinuation(continuation)
            }
        }
    }

    nonisolated func stopRealtimeStream() async {
        await finishRealtimeStream()
    }

    private func finishRealtimeStream() {
        realtimeContinuation?.finish()
        realtimeContinuation = nil
    }

    /// Returns the timeline row id the appended reply will have once a reload shows it.
    @discardableResult
    func appendAssistantMessage(_ text: String) -> String {
        let endedAt = ISO8601DateFormatter().string(from: Date())
        let rowID = "prose:\(nextEventID)"
        events.append(Self.makeEvent(
            id: nextEventID,
            role: "assistant",
            content: text,
            timestamp: endedAt,
            // Every provider turn ends with its own accounting; the fixture
            // reply carries one so the footer is part of the served shape.
            turnEnd: SessionTurnEnd(durationMs: 129_299, endedAt: endedAt, messageCount: nil)
        ))
        nextEventID += 1
        return rowID
    }

    func streamAssistantMessage(chunks: [String], intervalNanoseconds: UInt64) async {
        for chunk in chunks {
            upsertStreamingAssistantMessage(chunk)
            emitWorkspaceChanged()
            try? await Task.sleep(nanoseconds: intervalNanoseconds)
        }
    }

    func runTranscriptBenchmarkTrace(
        onUpdate: @MainActor @Sendable (Int, String) async -> Void,
        onScrollCheckpoint: @MainActor @Sendable (Int) async -> Void
    ) async -> TranscriptBenchmarkTraceResult {
        precondition(fixtureName == "benchmark-core")
        var updateCount = 0

        for snapshot in TranscriptBenchmarkTrace.streamingSnapshots() {
            upsertStreamingAssistantMessage(snapshot)
            updateCount += 1
            await applyBenchmarkUpdate(onUpdate, revision: updateCount, operation: "stream")
            if updateCount == 20 {
                await onScrollCheckpoint(updateCount)
            }
        }

        for ordinal in 1...3 {
            let callID = "benchmark-tool-\(ordinal)"
            let callEventID = nextEventID
            events.append(TranscriptBenchmarkTrace.toolCallEvent(
                id: callEventID,
                callID: callID,
                ordinal: ordinal,
                state: .running
            ))
            nextEventID += 1
            updateCount += 1
            await applyBenchmarkUpdate(onUpdate, revision: updateCount, operation: "tool_running")

            if let index = events.firstIndex(where: { $0.id == String(callEventID) }) {
                events[index] = TranscriptBenchmarkTrace.toolCallEvent(
                    id: callEventID,
                    callID: callID,
                    ordinal: ordinal,
                    state: .completed
                )
            }
            events.append(TranscriptBenchmarkTrace.toolResultEvent(
                id: nextEventID,
                callID: callID,
                ordinal: ordinal
            ))
            nextEventID += 1
            updateCount += 1
            await applyBenchmarkUpdate(onUpdate, revision: updateCount, operation: "tool_completed")
        }

        events.insert(contentsOf: TranscriptBenchmarkTrace.olderEvents(), at: 0)
        updateCount += 1
        await applyBenchmarkUpdate(onUpdate, revision: updateCount, operation: "prepend")

        let finalID = nextEventID
        events.append(TranscriptBenchmarkTrace.messageEvent(
            id: finalID,
            role: "assistant",
            content: "Benchmark trace complete after \(updateCount) renderer updates.",
            timestampOffset: TranscriptBenchmarkTrace.initialRowCount + finalID
        ))
        nextEventID += 1
        updateCount += 1
        await applyBenchmarkUpdate(onUpdate, revision: updateCount, operation: "append_final")

        return TranscriptBenchmarkTraceResult(
            updateCount: updateCount,
            expectedLatestItemID: "prose:\(finalID)"
        )
    }

    private func applyBenchmarkUpdate(
        _ onUpdate: @MainActor @Sendable (Int, String) async -> Void,
        revision: Int,
        operation: String
    ) async {
        let startedAt = DispatchTime.now().uptimeNanoseconds
        await onUpdate(revision, operation)
        let elapsed = DispatchTime.now().uptimeNanoseconds - startedAt
        if TranscriptBenchmarkTrace.streamingIntervalNanoseconds > elapsed {
            try? await Task.sleep(
                nanoseconds: TranscriptBenchmarkTrace.streamingIntervalNanoseconds - elapsed
            )
        }
    }

    private func attachRealtimeContinuation(
        _ continuation: AsyncStream<SessionWorkspaceStream.Event>.Continuation
    ) {
        realtimeContinuation?.finish()
        realtimeContinuation = continuation
        continuation.yield(.connected(SessionWorkspaceStream.Connected(
            session_id: sessionID,
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000)
        )))
        continuation.onTermination = { [weak self] _ in
            Task { await self?.clearRealtimeContinuation() }
        }
    }

    private func clearRealtimeContinuation() {
        realtimeContinuation = nil
    }

    private func upsertStreamingAssistantMessage(_ text: String) {
        if let eventID = streamingAssistantEventID,
           let index = events.firstIndex(where: { $0.legacyNumericId == eventID }) {
            events[index] = Self.makeEvent(
                id: eventID,
                role: "assistant",
                content: text,
                timestamp: ISO8601DateFormatter().string(from: Date())
            )
            return
        }

        let eventID = nextEventID
        streamingAssistantEventID = eventID
        events.append(Self.makeEvent(
            id: eventID,
            role: "assistant",
            content: text,
            timestamp: ISO8601DateFormatter().string(from: Date())
        ))
        nextEventID += 1
    }

    private func emitWorkspaceChanged() {
        let latestID = events.last?.legacyNumericId ?? 0
        realtimeContinuation?.yield(.changed(SessionWorkspaceStream.WorkspaceChanged(
            session_id: sessionID,
            latest_event_id: latestID,
            thread_session_count: 1,
            latest_event_emitted_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_fanout_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            server_now_ms: Int64(Date().timeIntervalSince1970 * 1000),
            pubsub_seq: latestID,
            transcript_preview: nil
        )))
    }

    private static func makeWorkspace(
        sessionID: String,
        events: [SessionEvent],
        title: String = "Chat UI Fixture"
    ) -> SessionWorkspaceResponse {
        let detail = makeDetail(sessionID: sessionID, events: events, title: title)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
                timestamp: event.timestamp,
                event: event,
                continuedFromSessionId: nil,
                continuationKind: nil,
                originLabel: nil,
                parentOriginLabel: nil,
                parentContinuationKind: nil,
                branchedFromEventId: nil
            )
        }
        return SessionWorkspaceResponse(
            session: detail,
            thread: SessionThreadResponse(
                rootSessionId: sessionID,
                headSessionId: sessionID,
                sessions: [detail]
            ),
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: projectionItems.count,
                pageOffset: 0,
                branchMode: "head",
                abandonedEvents: 0
            )
        )
    }

    private static func makeMobileTail(
        sessionID: String,
        events: [SessionEvent],
        total: Int,
        pageOffset: Int,
        snapshotEventId: String?,
        title: String = "Chat UI Fixture"
    ) -> SessionMobileTailResponse {
        let detail = makeDetail(sessionID: sessionID, events: events, title: title)
        let projectionItems = events.map { event in
            SessionProjectionItem(
                kind: "event",
                sessionId: sessionID,
                timestamp: event.timestamp,
                event: event,
                continuedFromSessionId: nil,
                continuationKind: nil,
                originLabel: nil,
                parentOriginLabel: nil,
                parentContinuationKind: nil,
                branchedFromEventId: nil
            )
        }
        return SessionMobileTailResponse(
            session: detail,
            projection: SessionProjectionResponse(
                rootSessionId: sessionID,
                focusSessionId: sessionID,
                headSessionId: sessionID,
                pathSessionIds: [sessionID],
                items: projectionItems,
                total: total,
                pageOffset: pageOffset,
                branchMode: "head",
                abandonedEvents: 0
            ),
            snapshotEventId: snapshotEventId
        )
    }

    private static func tailPage(events: [SessionEvent], limit: Int, offset: Int) -> (events: [SessionEvent], pageOffset: Int) {
        let total = events.count
        let pageOffset = max(0, total - limit - offset)
        let end = max(0, total - offset)
        guard pageOffset < end else {
            return ([], pageOffset)
        }
        return (Array(events[pageOffset..<end]), pageOffset)
    }

    /// Session header title for a given fixture. Marketing captures want a
    /// realistic session title, not the test-harness label.
    static func titleForFixture(_ fixtureName: String) -> String {
        switch fixtureName {
        case "marketing":
            return "Wire up OAuth refresh flow"
        case "ended-codex-helm":
            return "Ended Codex Helm"
        case "loading-long-title":
            return "A very long session title that must stay inside the navigation bar"
        case "helm-channel-reconcile":
            return "Helm Send Reconciliation"
        case "background-tasks":
            return "Background Tasks"
        case "background-tasks-sheet":
            return "Background Tasks (sheet)"
        case "background-tasks-recent-sheet":
            return "Background Tasks (empty) (recent)"
        case "background-tasks-recent-stale-sheet":
            return "Background Tasks (stale) (recent)"
        case "background-tasks-timeline":
            return "Background Tasks (timeline)"
        case "background-tasks-attention":
            return "Background Tasks (attention)"
        case "background-tasks-timeline-stale":
            return "Background Tasks (timeline) (stale)"
        case "background-tasks-transition":
            return "Background Tasks"
        case "background-tasks-empty":
            return "Background Tasks (empty)"
        case "background-tasks-local-expired-empty":
            return "Background Tasks (local expired empty)"
        case "background-tasks-stale":
            return "Background Tasks (stale)"
        case "background-tasks-expired-positive":
            return "Background Tasks (expired positive)"
        case "background-tasks-expired-empty":
            return "Background Tasks (expired empty)"
        case "background-tasks-unobserved-unknown":
            return "Background Tasks (unobserved unknown)"
        default:
            return "Chat UI Fixture"
        }
    }

    static func makeDetail(
        sessionID: String,
        events: [SessionEvent],
        title: String = "Chat UI Fixture"
    ) -> SessionDetail {
        // Marketing captures must not leak test-harness copy into the chrome.
        let isMarketing = title == titleForFixture("marketing")
        let isEndedCodexHelm = title == titleForFixture("ended-codex-helm")
        let isHelmChannelReconcile = title == titleForFixture("helm-channel-reconcile")
        let isTimelineDelegation = title.contains("Background Tasks (timeline)")
        let isAttentionDelegation = title == titleForFixture("background-tasks-attention")
        let composerPlaceholder = isMarketing
            ? "Message"
            : "Steer this turn"
        let idleDetail = isHelmChannelReconcile
            ? "Working on the current turn"
            : (isMarketing ? "Waiting for input" : "Waiting for UI test input")
        let available = SessionStateAction(state: "available", reason: nil)
        let unavailable = SessionStateAction(state: "unavailable", reason: "fixture_not_granted")
        var detail = SessionDetail(
            id: sessionID,
            title: title,
            provider: isHelmChannelReconcile ? "claude" : "codex",
            project: "longhouse",
            cwd: "/Users/example/git/zerg/longhouse",
            gitBranch: "main",
            summary: title,
            summaryTitle: title,
            presenceState: isEndedCodexHelm ? "ended" : (isHelmChannelReconcile ? "thinking" : "idle"),
            presenceTool: nil,
            userState: "active",
            status: isEndedCodexHelm ? "ended" : (isHelmChannelReconcile ? "thinking" : "idle"),
            lastActivityAt: events.last?.timestamp,
            displayPhase: isEndedCodexHelm ? "Ended" : (isHelmChannelReconcile ? "Thinking" : "Idle"),
            activeTool: nil,
            homeLabel: isEndedCodexHelm ? "misleading-home" : "MacBook",
            originLabel: isEndedCodexHelm ? "misleading-origin" : "UI test",
            capabilities: SessionCapabilities(
                canQueueNextInput: true,
                canSteerActiveTurn: isHelmChannelReconcile,
                defaultInputIntent: "auto",
                composerPlaceholder: composerPlaceholder,
                attachImages: false
            ),
            runtimeDisplay: SessionRuntimeDisplay(
                truthTier: "live",
                signalTier: "none",
                state: isEndedCodexHelm ? "ended" : (isHelmChannelReconcile ? "thinking" : "idle"),
                tone: isEndedCodexHelm ? "ended" : (isHelmChannelReconcile ? "thinking" : "idle"),
                headline: isEndedCodexHelm ? "Ended" : (isHelmChannelReconcile ? "Thinking" : "Idle"),
                detail: isEndedCodexHelm ? "Run ended" : idleDetail,
                phaseLabel: isEndedCodexHelm ? "Ended" : (isHelmChannelReconcile ? "Thinking" : "Idle"),
                compactToolLabel: nil,
                isLive: !isEndedCodexHelm,
                isExecuting: isHelmChannelReconcile,
                needsAttention: false,
                isIdle: !isHelmChannelReconcile && !isEndedCodexHelm,
                isStalled: false,
                isManagedLocalTruth: true,
                hasSignal: true,
                controlPath: "managed",
                activityRecency: isEndedCodexHelm ? "historical" : "live",
                lifecycle: isEndedCodexHelm ? "ended" : "open",
                hostState: "online",
                terminalReason: isEndedCodexHelm ? "provider_exit" : nil
            ),
            stateFacts: DefaultUnknownSessionStateFacts(
                wrappedValue: SessionStateFacts(
                    contractVersion: 1,
                    presentationPolicyVersion: 1,
                    mode: "helm",
                    dispositionState: "open",
                    dispositionCloseReason: nil,
                    launchState: nil,
                    runLifecycle: isEndedCodexHelm ? "ended" : "running",
                    activityState: isEndedCodexHelm ? "ended" : (isHelmChannelReconcile ? "thinking" : "quiescent"),
                    activityRawKind: nil,
                    activityTool: nil,
                    activitySource: nil,
                    activityObservedAt: nil,
                    activityValidUntil: isTimelineDelegation ? "2000-01-01T00:00:00Z" : nil,
                    controlOwnership: "owned",
                    controlConnection: "connected",
                    workingSet: "open",
                    unread: false,
                    lastResultAt: nil,
                    lastResultOutcome: nil,
                    startTurn: isEndedCodexHelm ? unavailable : available,
                    sendInput: isEndedCodexHelm
                        ? SessionStateAction(state: "unavailable", reason: "run_ended")
                        : available,
                    interrupt: available,
                    terminate: available,
                    reattach: unavailable,
                    resume: isEndedCodexHelm ? available : unavailable,
                    branch: isEndedCodexHelm ? available : unavailable,
                    pendingInteractionKind: isAttentionDelegation ? "question" : nil,
                    transcriptConvergence: "current",
                    primary: SessionStateLabel(
                        key: isEndedCodexHelm ? "ended" : (isAttentionDelegation ? "needs_answer" : (isTimelineDelegation ? "delegated_work" : (isHelmChannelReconcile ? "thinking" : "idle"))),
                        label: isEndedCodexHelm ? "Ended" : (isAttentionDelegation ? "Needs answer" : (isTimelineDelegation ? "Background · 1 agent · 1 command" : (isHelmChannelReconcile ? "Thinking" : "Idle"))),
                        tone: isEndedCodexHelm ? "quiet" : (isAttentionDelegation ? "blocked" : (isTimelineDelegation ? "active" : (isHelmChannelReconcile ? "thinking" : "idle"))),
                        observedAt: nil
                    ),
                    access: SessionStateLabel(key: "live_control", label: "Live control", tone: "live", observedAt: nil),
                    transcript: nil,
                    commitSeq: nil
                )
            ),
            deviceId: isEndedCodexHelm ? "cinder" : nil
        )
        if title.hasPrefix("Background Tasks") {
            let positiveExpired = title.contains("(stale)") || title.contains("(expired positive)")
            let serverEmptyExpired = title.contains("(expired empty)")
            let localEmptyExpired = title.contains("(local expired empty)")
            let unobservedUnknown = title.contains("(unobserved unknown)")
            let emptyExpired = serverEmptyExpired || localEmptyExpired
            let empty = title.contains("(empty)") || emptyExpired
            let observedAt = unobservedUnknown ? nil : Self.fixedTimestamp(offset: -20)
            let validUntil = positiveExpired || emptyExpired
                ? "2000-01-01T00:00:00Z"
                : "2099-01-01T00:00:00Z"
            let tasks: [SessionDelegationTask] = empty
                ? []
                : [
                    SessionDelegationTask(
                        id: "agent-1",
                        kind: "subagent",
                        status: "running",
                        description: "Review the exact child transcript",
                        firstObservedAt: Self.fixedTimestamp(offset: -18),
                        startedAt: Self.fixedTimestamp(offset: -16),
                        lastActivityAt: Self.fixedTimestamp(offset: -2),
                        sessionId: "019fc50b-1111-4111-8111-111111111111",
                        userMessages: 1, assistantMessages: 4, toolCalls: 12,
                        registeredAt: Self.fixedTimestamp(offset: -20),
                        nativeProgress: SessionDelegationProgress(
                            observedAt: Self.fixedTimestamp(offset: -3),
                            status: "running",
                            currentTool: "Read",
                            lastIntent: "Inspect reconnect evidence",
                            toolCount: 14, requests: 6, tokens: 4200,
                            contextTokens: nil, contextWindow: nil, durationMs: 8000
                        )
                    ),
                    SessionDelegationTask(
                        id: "shell-1",
                        kind: "shell",
                        status: "queued",
                        description: "Collect fixture metadata",
                        firstObservedAt: Self.fixedTimestamp(offset: -15),
                        startedAt: nil,
                        lastActivityAt: Self.fixedTimestamp(offset: -5),
                        sessionId: nil
                    ),
                ]
            let serverUnknown = unobservedUnknown || positiveExpired || serverEmptyExpired
            detail.stateFacts.delegation = SessionDelegationFacts(
                state: serverUnknown ? "unknown" : (empty ? "none" : "pending"),
                count: serverUnknown || localEmptyExpired ? nil : tasks.count,
                kinds: serverUnknown || localEmptyExpired ? nil : (empty ? [:] : ["subagent": 1, "shell": 1]),
                source: "ui_fixture",
                observedAt: observedAt,
                validUntil: validUntil,
                items: positiveExpired || unobservedUnknown ? nil : tasks,
                recentItems: !title.contains("(recent)") ? nil : [
                    SessionDelegationTask(
                        id: "finished-command", kind: "shell", status: "failed",
                        description: "Completed provider command", firstObservedAt: Self.fixedTimestamp(offset: -20),
                        startedAt: nil, lastActivityAt: nil, sessionId: nil,
                        registeredAt: Self.fixedTimestamp(offset: -20), endedAt: Self.fixedTimestamp(offset: -4)
                    )
                ]
            )
        }
        return detail
    }

    private static func makeEvent(
        id: Int,
        role: String,
        content: String,
        timestamp: String,
        inputOrigin: SessionInputOrigin? = nil,
        turnEnd: SessionTurnEnd? = nil
    ) -> SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: content,
            toolName: nil,
            toolInputJSON: nil,
            toolOutputText: nil,
            toolCallId: nil,
            toolCallState: nil,
            timestamp: timestamp,
            inActiveContext: true,
            isHeadBranch: true,
            inputOrigin: inputOrigin,
            turnEnd: turnEnd
        )
    }

    /// A realistic mixed transcript that exercises the redesign's demoted
    /// tool-row CSS + TimelineBuilder pairing: assistant prose, a paired
    /// tool call+result, a passive group, and a dropped (orphaned) tool call.
    private static func toolFixtureEvents() -> [SessionEvent] {
        var events: [SessionEvent] = []
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }

        events.append(SessionEvent(
            id: next(), role: "user",
            contentText: "Find who renamed the ticket after the meeting and retry the MR state.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Now I can see exactly what Alex did. Two new blocker tickets appeared: **PROJ-101** and **PROJ-102**. Let me pull those.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // Paired tool call + result.
        let callId = "call-jira-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil, toolOutputText: nil, toolCallId: callId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "getJiraIssue", toolInputJSON: nil,
            toolOutputText: "PROJ-101: blocked on MR rename by Alex at 18:42.",
            toolCallId: callId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A Bash call with a large-ish output (the "work is not noise" case).
        let bashId = "call-bash-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Bash", toolInputJSON: nil, toolOutputText: nil, toolCallId: bashId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Bash", toolInputJSON: nil,
            toolOutputText: String(repeating: "git log line for changelog parsing\n", count: 12),
            toolCallId: bashId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        // A dropped/orphaned tool call — no matching result (the trust case).
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "mcp__atlassian__getJiraIssue", toolInputJSON: nil, toolOutputText: nil,
            toolCallId: "call-dropped-1", toolCallState: .dropped, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "The MR was renamed by Alex at 18:42, then moved back to In Review.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        return events
    }

    /// The served shape of OMP background-job results and a Claude task
    /// notice: `system` rows with `interaction_kind=provider_notification`,
    /// text bounded by the server (header, an elision line, the output's tail),
    /// between tool rows. Session 352dfbc4 filled the viewport with these.
    private static func providerNotificationFixtureEvents() -> [SessionEvent] {
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }
        func notice(_ text: String) -> SessionEvent {
            SessionEvent(
                id: next(),
                role: "system",
                contentText: text,
                interactionKind: "provider_notification",
                toolName: nil,
                toolInputJSON: nil,
                toolOutputText: nil,
                toolCallId: nil,
                toolCallState: nil,
                timestamp: ts(),
                inActiveContext: true,
                isHeadBranch: true,
                inputOrigin: nil
            )
        }
        func tool(_ name: String, output: String, callId: String) -> [SessionEvent] {
            [
                SessionEvent(
                    id: next(), role: "assistant", contentText: nil,
                    toolName: name, toolInputJSON: nil, toolOutputText: nil, toolCallId: callId,
                    toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
                ),
                SessionEvent(
                    id: next(), role: "tool", contentText: nil,
                    toolName: name, toolInputJSON: nil, toolOutputText: output, toolCallId: callId,
                    toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
                ),
            ]
        }

        let ompLong = [
            "Background job bg_96 has completed.",
            "…",
            "ered job: zerg-tenant-data-reserve (cron=*/5 * * * *, enabled=True)",
            "2026-09-30 15:24:21,974 [INFO] sauron.jobs.registry: Registered job: llm-bench-opencode-dry-run (cron=0 9 * * *, enabled=False)",
            "2026-09-30 15:24:21,974 [INFO] sauron.jobs.registry: Registered job: llm-bench-discovery (cron=0 7 * * *, enabled=False)",
            "2026-09-30 15:24:21,974 [INFO] sauron.jobs.registry: Registered job: llm-bench-provider-discovery (cron=0 7 * * *, enabled=True)",
            "2026-09-30 15:24:21,975 [INFO] sauron.jobs.registry: Registered job: llm-bench-health (cron=0 8 * * *, enabled=False)",
            "2026-09-30 15:24:21,975 [INFO] sauron.jobs.registry: Registered job: llm-bench-invariant-watch (cron=0 */6 * * *, enabled=True)",
            "2026-09-30 15:24:21,976 [INFO] sauron.jobs.registry: Registered job: longhouse-hosted-storage-ops-alerts (cron=*/5 * * * *, enabled=True)",
            "2026-09-30 15:24:22,038 [INFO] sauron.jobs.registry: Registered job: gmail-watchdog (cron=0 */2 * * *, enabled=True)",
            "2026-09-30 15:24:22,038 [INFO] sauron.jo",
            "[Output truncated. Showing first 4,000 characters.]",
            "Full output: artifact://335",
        ].joined(separator: "\n")
        let ompCI = [
            "Background job bg_97 has completed.",
            "* main Deploy Factory · 36736390872",
            "Triggered via push about 1 minute ago",
            "",
            "JOBS",
            "✓ Deploy or roll back in 1m46s (ID 109959098753)",
            "  ✓ Set up job",
            "  ✓ Run actions/checkout@v4",
            "  ✓ Verify image exists on GHCR (deploy only)",
            "",
            "ANNOTATIONS",
            "! Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4.",
        ].joined(separator: "\n")

        var events: [SessionEvent] = []
        events.append(makeEvent(id: next(), role: "user", content: "Triage the Sauron alert emails and tell me which jobs are actually broken.", timestamp: ts()))
        events.append(makeEvent(id: next(), role: "assistant", content: "I started the registry listing in the background; the provider will report back when it finishes.", timestamp: ts()))
        events += tool("Bash", output: "Backgrounded early: the command keeps running as bg_96.", callId: "call-bg-1")
        events.append(notice(ompLong))
        events += tool("Read", output: "def register(job): ...", callId: "call-read-1")
        events.append(notice(ompCI))
        events += tool("Bash", output: "no failed steps", callId: "call-gh-1")
        events.append(notice("Background command \"Run the checks\" completed (exit code 0)"))
        events.append(makeEvent(id: next(), role: "assistant", content: "None of the alerting jobs is broken: the registry is healthy and the factory deploy finished green.", timestamp: ts()))
        // Longhouse's own close notice after "Stop background work", with the
        // server's line for a close that could not confirm its processes are gone.
        events.append(makeEvent(
            id: next(),
            role: "user",
            content: "Stopped 1 background task: tail the dev server log. Some processes were still running, "
                + "but Longhouse couldn't confirm they were this session's, so it left them alone.",
            timestamp: ts(),
            inputOrigin: SessionInputOrigin(
                authoredVia: .longhouse,
                origin: "longhouse",
                sessionInputId: nil,
                clientRequestId: "close:invocation-fixture"
            )
        ))
        return events
    }

    /// The full output behind the `lite-bodies` fixture's preview; its last
    /// line only appears once the expanded row has loaded the whole body.
    static let liteBodiesFullOutput = "Chunk ID: 1\nWall time: 41.2 seconds\nProcess exited with code 0\nOutput:\n"
        + (1...40).map { "tests/test_case_\($0).py::test_passes PASSED" }.joined(separator: "\n")
        + "\nFull body loaded: 40 passed in 41.2s"

    /// A lite page: the command and its output arrive as the collapsed row's
    /// preview, and `/event-bodies` (below) answers with the whole body.
    private static func liteBodiesFixtureEvents() -> [SessionEvent] {
        let preview = "Chunk ID: 1\nWall time: 41.2 seconds\nProcess exited with code 0\nOutput:\n"
            + "tests/test_case_1.py::test_passes PASSED\ntests/test_case_2.py::test_passes PASSED\n… 31 more lines …\n"
            + (34...40).map { "tests/test_case_\($0).py::test_passes PASSED" }.joined(separator: "\n")
        let callId = "call-lite-bash"
        return [
            makeEvent(id: 1, role: "user", content: "Run the whole test suite.", timestamp: fixedTimestamp(offset: 0)),
            SessionEvent(
                id: "2", role: "assistant", contentText: nil,
                toolName: "Bash", toolInputJSON: ["command": .string("uv run pytest -q tests/")],
                toolOutputText: nil, toolCallId: callId, toolCallState: .completed,
                timestamp: fixedTimestamp(offset: 1), inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                cursor: "lite-cursor-2"
            ),
            SessionEvent(
                id: "3", role: "tool", contentText: nil,
                toolName: "Bash", toolInputJSON: nil, toolOutputText: preview,
                toolCallId: callId, toolCallState: .completed,
                timestamp: fixedTimestamp(offset: 2), inActiveContext: true, isHeadBranch: true, inputOrigin: nil,
                cursor: "lite-cursor-3",
                toolOutputTruncated: true,
                toolOutputOriginalChars: liteBodiesFullOutput.count
            ),
            makeEvent(id: 4, role: "assistant", content: "All 40 tests pass.", timestamp: fixedTimestamp(offset: 3)),
        ]
    }

    func sessionEventBodies(id: String, cursors: [String]) async throws -> SessionEventBodiesResponse {
        guard fixtureName == "lite-bodies" else { throw LonghouseAPIError.requestFailed }
        // A real read takes time; the UI test waits for the loaded body.
        try await Task.sleep(nanoseconds: 600_000_000)
        return SessionEventBodiesResponse(
            events: cursors.filter { $0 == "lite-cursor-3" }.map {
                SessionEventBody(id: "3", cursor: $0, toolInputJson: nil, toolOutputText: Self.liteBodiesFullOutput, toolPresentation: nil)
            },
            missing: []
        )
    }

    /// A realistic CODING session for marketing captures: a real-feeling task
    /// (OAuth refresh), paired Read/Edit/Bash tool calls, and a clean result.
    /// On-message for the launch wedge (a coding agent you steer), unlike the
    /// Jira `tools` fixture which exists to exercise the dropped-tool case.
    private static func marketingFixtureEvents() -> [SessionEvent] {
        var events: [SessionEvent] = []
        var id = 0
        func next() -> Int { id += 1; return id }
        func ts() -> String { fixedTimestamp(offset: id) }

        events.append(SessionEvent(
            id: next(), role: "user",
            contentText: "The access token expires mid-session and users get logged out. Add silent refresh before it expires.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Found it — the client only refreshes on a 401. I'll add a timer that refreshes ~60s before expiry so a request never races the token.",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let readId = "call-read-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Read", toolInputJSON: nil, toolOutputText: nil, toolCallId: readId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Read", toolInputJSON: nil,
            toolOutputText: "src/lib/auth-refresh.ts — single-flight 401 retry, no proactive refresh.",
            toolCallId: readId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let editId = "call-edit-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Edit", toolInputJSON: nil, toolOutputText: nil, toolCallId: editId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Edit", toolInputJSON: nil,
            toolOutputText: "scheduleRefresh() armed on token issue; cleared on logout.",
            toolCallId: editId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        let bashId = "call-bash-1"
        events.append(SessionEvent(
            id: next(), role: "assistant", contentText: nil,
            toolName: "Bash", toolInputJSON: nil, toolOutputText: nil, toolCallId: bashId,
            toolCallState: .completed, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "tool", contentText: nil,
            toolName: "Bash", toolInputJSON: nil,
            toolOutputText: "✓ auth.test.ts (14 passed) — refreshes 60s pre-expiry, no logout",
            toolCallId: bashId, toolCallState: .completed, timestamp: ts(),
            inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        events.append(SessionEvent(
            id: next(), role: "assistant",
            contentText: "Done. Tokens now refresh silently a minute before expiry and tests pass. Want me to rebase onto main and open the PR?",
            toolName: nil, toolInputJSON: nil, toolOutputText: nil, toolCallId: nil,
            toolCallState: nil, timestamp: ts(), inActiveContext: true, isHeadBranch: true, inputOrigin: nil
        ))
        return events
    }

    private static func loadReplayEvents(path: String) -> [SessionEvent]? {
        let url = URL(fileURLWithPath: path)
        guard let data = try? Data(contentsOf: url) else { return nil }
        do {
            let fixture = try JSONDecoder().decode(ChatUITestReplayFile.self, from: data)
            return fixture.events.enumerated().map { index, event in
                SessionEvent(
                    id: event.id ?? index + 1,
                    role: event.role,
                    contentText: event.contentText,
                    toolName: event.toolName,
                    toolInputJSON: event.toolInputJson,
                    toolOutputText: event.toolOutputText,
                    toolCallId: event.toolCallId,
                    // Real exports can't carry tool_call_state (server-derived at
                    // projection time); synthetic fixtures may set it explicitly.
                    toolCallState: event.toolCallState.flatMap(ToolCallState.init(rawValue:)),
                    timestamp: event.timestamp,
                    inActiveContext: true,
                    isHeadBranch: true,
                    inputOrigin: nil
                )
            }
        } catch {
            return nil
        }
    }

    private static func messageText(index: Int, role: String, fixtureName: String) -> String {
        if role == "assistant" {
            if fixtureName == "render-storm" {
                return """
                Assistant fixture message \(index): realistic long response with markdown, code, and enough text to stress WebKit rendering.

                - Session event id: \(index)
                - Tool summary: read, search, patch, validate
                - Runtime state: streaming

                ```swift
                struct FixtureRow\(index) {
                    let id = \(index)
                    let text = "This is a realistic transcript payload with code blocks and wrapping text."
                }
                ```

                The transcript renderer should handle this without repeatedly re-rendering identical payloads, without blocking touch scrolling, and without snapping back to the bottom when the user has intentionally scrolled upward.

                \(String(repeating: "Detailed fixture paragraph for mobile rendering, scroll anchoring, markdown layout, and text wrapping. ", count: 8))
                """
            }
            return "Assistant fixture message \(index): streaming-style response with enough body to exercise row layout."
        }
        if fixtureName == "render-storm" {
            return "User fixture message \(index): realistic request text for mobile chat stress testing, scroll anchoring, and duplicate render detection."
        }
        return "User fixture message \(index): request text for chat scroll anchoring."
    }

    private static func fixedTimestamp(offset: Int) -> String {
        let date = Date(timeIntervalSince1970: 1_777_737_600 + TimeInterval(offset))
        return ISO8601DateFormatter().string(from: date)
    }
}

private struct ChatUITestReplayFile: Decodable {
    let events: [ChatUITestReplayEvent]
}

private struct ChatUITestReplayEvent: Decodable {
    let id: Int?
    let role: String
    let contentText: String?
    let toolName: String?
    let toolInputJson: [String: JSONValue]?
    let toolOutputText: String?
    let toolCallId: String?
    let toolCallState: String?
    let timestamp: String
}
#endif
