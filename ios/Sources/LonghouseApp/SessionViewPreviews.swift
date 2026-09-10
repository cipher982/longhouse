import SwiftUI

// MARK: - Preview helpers

/// Served session-state facts, camelCase because the preview decodes with a
/// plain JSONDecoder. Every key the contract carries is present so a preview
/// never renders the "unknown" fallback by accident.
private func factsJSON(
    mode: String = "helm",
    disposition: String = "open",
    runLifecycle: String? = "running",
    activity: String,
    tool: String? = nil,
    observedAt: String? = nil,
    validUntil: String? = nil,
    controlConnection: String = "connected",
    sendInput: String = "available",
    primaryKey: String,
    primaryLabel: String,
    primaryTone: String,
    access: (key: String, label: String, tone: String)? = nil,
    pendingInteractionKind: String? = nil
) -> String {
    func q(_ value: String?) -> String { value.map { "\"\($0)\"" } ?? "null" }
    let accessJSON = access.map {
        "{ \"key\": \"\($0.key)\", \"label\": \"\($0.label)\", \"tone\": \"\($0.tone)\", \"observedAt\": null }"
    } ?? "null"
    return """
    {
      "contractVersion": 2,
      "presentationPolicyVersion": 2,
      "mode": "\(mode)",
      "dispositionState": "\(disposition)",
      "dispositionCloseReason": null,
      "launchState": null,
      "runLifecycle": \(q(runLifecycle)),
      "activityState": "\(activity)",
      "activityRawKind": null,
      "activityTool": \(q(tool)),
      "activitySource": null,
      "activityObservedAt": \(q(observedAt)),
      "activityValidUntil": \(q(validUntil)),
      "controlOwnership": "owned",
      "controlConnection": "\(controlConnection)",
      "workingSet": "open",
      "unread": false,
      "lastResultAt": null,
      "lastResultOutcome": null,
      "startTurn": { "state": "unavailable", "reason": "not_console" },
      "sendInput": { "state": "\(sendInput)", "reason": null },
      "interrupt": { "state": "available", "reason": null },
      "terminate": { "state": "unavailable", "reason": "unsupported" },
      "reattach": { "state": "unavailable", "reason": "not_needed" },
      "resume": { "state": "unavailable", "reason": "run_active" },
      "branch": { "state": "unavailable", "reason": "run_active" },
      "pendingInteractionKind": \(q(pendingInteractionKind)),
      "transcriptConvergence": "current",
      "primary": { "key": "\(primaryKey)", "label": "\(primaryLabel)", "tone": "\(primaryTone)", "observedAt": \(q(observedAt)) },
      "access": \(accessJSON),
      "transcript": null,
      "commitSeq": null
    }
    """
}

private func isoDate(secondsAgo: TimeInterval) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: Date().addingTimeInterval(-secondsAgo))
}

private extension SessionDetail {
    static func mock(
        provider: String = "claude",
        project: String = "zerg",
        homeLabel: String = "clifford",
        canSteer: Bool = false,
        canQueue: Bool = false,
        executing: Bool = false,
        placeholder: String = "Message",
        stateFactsJSON: String,
        transcriptPreviewJSON: String? = nil,
        pauseRequestJSON: String? = nil
    ) -> SessionDetail {
        let json = """
        {
          "id": "preview-1",
          "provider": "\(provider)",
          "project": "\(project)",
          "cwd": "/Users/example/code",
          "gitBranch": "main",
          "summary": "Backup architecture restore",
          "summaryTitle": "Backup Architecture Restore",
          "presenceState": "\(executing ? "running" : "idle")",
          "userState": "active",
          "homeLabel": "\(homeLabel)",
          "capabilities": {
            "canQueueNextInput": \(canQueue),
            "canSteerActiveTurn": \(canSteer),
            "composerPlaceholder": "\(placeholder)"
          },
          "runtimeDisplay": {
            "truthTier": "managed-local",
            "signalTier": "phase_signal",
            "state": "\(executing ? "running" : "idle")",
            "tone": "\(executing ? "running" : "idle")",
            "headline": "",
            "detail": null,
            "phaseLabel": "",
            "compactToolLabel": null,
            "isLive": true,
            "isExecuting": \(executing),
            "needsAttention": false,
            "isIdle": \(!executing),
            "isStalled": false,
            "isManagedLocalTruth": true,
            "hasSignal": true,
            "controlPath": "managed",
            "activityRecency": "live",
            "lifecycle": "open",
            "hostState": "online",
            "terminalReason": null,
            "pauseRequest": \(pauseRequestJSON ?? "null")
          },
          "stateFacts": \(stateFactsJSON)\(transcriptPreviewJSON.map { ",\n          \"transcriptPreview\": \($0)" } ?? "")
        }
        """
        do {
            return try JSONDecoder().decode(SessionDetail.self, from: Data(json.utf8))
        } catch {
            print("--- [SessionDetail.mock decoding failure] ---")
            print(error)
            print(json)
            fatalError("Failed to decode SessionDetail mock: \(error)")
        }
    }
}

private func approvalPreviewJSON() -> String {
    """
    {
      "id": "pause-preview",
      "sessionId": "preview-1",
      "runtimeKey": "claude:preview-1",
      "kind": "permission_prompt",
      "status": "pending",
      "provider": "claude",
      "canRespond": false,
      "title": "Allow the Bash command?",
      "summary": "The provider is waiting before it runs the cleanup command.",
      "toolName": "Bash",
      "questions": [],
      "occurredAt": "\(isoDate(secondsAgo: 8))",
      "lastSeenAt": "\(isoDate(secondsAgo: 2))",
      "resolvedAt": null,
      "expiresAt": null
    }
    """
}

private func toolPreviewJSON(tool: String, command: String, running: Bool = true) -> String {
    """
    {
      "eventId": 4870,
      "text": "\(command)",
      "role": "assistant",
      "toolName": "\(tool)",
      "toolInputJSON": { "command": "\(command)" },
      "toolOutputText": null,
      "toolCallId": "call-1",
      "toolCallState": "\(running ? "running" : "completed")",
      "eventOrigin": "durable",
      "timestamp": "\(isoDate(secondsAgo: 31))",
      "isProvisional": false,
      "isComplete": false,
      "contentCursor": null,
      "isStale": false,
      "staleReason": null
    }
    """
}

private func provisionalTextPreviewJSON(_ text: String) -> String {
    """
    {
      "eventId": 4871,
      "text": "\(text)",
      "role": "assistant",
      "toolName": null,
      "toolInputJSON": null,
      "toolOutputText": null,
      "toolCallId": null,
      "toolCallState": null,
      "eventOrigin": "live_provisional",
      "timestamp": "\(isoDate(secondsAgo: 1))",
      "isProvisional": true,
      "isComplete": false,
      "contentCursor": "codex:preview:1",
      "isStale": false,
      "staleReason": null
    }
    """
}

/// A strip seeded with a realistic last twelve seconds. Offsets are seconds
/// ago; the store keeps whatever falls inside its window.
@MainActor
private func seededActivity(_ pattern: [(TimeInterval, ActivityPulse.Kind)]) -> ActivityPulseStore {
    let store = ActivityPulseStore()
    let now = Date()
    for (secondsAgo, kind) in pattern.sorted(by: { $0.0 > $1.0 }) {
        store.record(kind, at: now.addingTimeInterval(-secondsAgo))
    }
    return store
}

@MainActor
private func codexBurst() -> ActivityPulseStore {
    var pattern: [(TimeInterval, ActivityPulse.Kind)] = [(11.4, .toolStart), (6.1, .toolResult), (5.2, .message), (0.4, .toolStart)]
    var t: TimeInterval = 11.1
    while t > 6.4 { pattern.append((t, .textDelta)); t -= Double.random(in: 0.12...0.38) }
    t = 5.0
    while t > 3.6 { pattern.append((t, .textDelta)); t -= Double.random(in: 0.09...0.24) }
    return seededActivity(pattern)
}

@MainActor
private func claudeSparse() -> ActivityPulseStore {
    seededActivity([(10.8, .toolResult), (10.2, .message), (7.9, .toolStart), (2.4, .state)])
}

// MARK: - Preview chrome — the real bottom card over a fake transcript

private struct SessionScreenPreview: View {
    let detail: SessionDetail
    let activity: ActivityPulseStore
    var transcript: [String] = [
        "The restore has reached 9.2 GB and remains healthy.",
        "Checksums so far match the manifest; continuing with the exact restore.",
    ]
    var connection: SessionRealtimeConnection = .connected
    var isSending: Bool = false
    var queuedInputCount: Int = 0
    @State private var text: String
    @FocusState private var composerFocused: Bool

    init(
        detail: SessionDetail,
        activity: ActivityPulseStore,
        transcript: [String] = [
            "The restore has reached 9.2 GB and remains healthy.",
            "Checksums so far match the manifest; continuing with the exact restore.",
        ],
        connection: SessionRealtimeConnection = .connected,
        draft: String = "",
        isSending: Bool = false,
        queuedInputCount: Int = 0
    ) {
        self.detail = detail
        self.activity = activity
        self.transcript = transcript
        self.connection = connection
        self.isSending = isSending
        self.queuedInputCount = queuedInputCount
        _text = State(initialValue: draft)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(transcript, id: \.self) { line in
                        Text(line).font(.body)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(alignment: .leading, spacing: 8) {
                    SessionRuntimeDock(detail: detail, activity: activity, realtimeConnection: connection)
                    if SessionComposerControlState.isVisible(for: detail) {
                        SessionComposer(
                            detail: detail,
                            text: $text,
                            focused: $composerFocused,
                            queuedInputCount: queuedInputCount,
                            isSending: isSending,
                            onQueueInstead: {},
                            onDismissTurnEnded: {},
                            onPauseRespond: { _, _, _, _ in false },
                            onSend: { _ in },
                            actionMenu: {
                                SessionComposerActionMenu(
                                    detail: detail,
                                    attachmentSlotsLeft: ComposerAttachmentLimits.maxAttachments,
                                    attachmentInputEnabled: SessionComposerControlState.attachmentInputEnabled(for: detail),
                                    isProcessing: false,
                                    isSending: isSending,
                                    onAttach: {}
                                )
                            },
                            attachmentTray: { EmptyView() }
                        )
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(.ultraThinMaterial)
                        .overlay(
                            RoundedRectangle(cornerRadius: 24, style: .continuous)
                                .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                        )
                )
                .shadow(color: .black.opacity(0.28), radius: 16, y: 5)
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
            }
            .navigationTitle(detail.displayTitle)
            .navigationBarTitleDisplayMode(.inline)
            .modifier(PreviewSubtitle(subtitle: detail.identitySubtitle))
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button {} label: { Label("Lock Screen Updates", systemImage: "bell") }
                        Divider()
                        Button {} label: { Label("Copy Link", systemImage: "link") }
                        Button {} label: { Label("Open on Web", systemImage: "safari") }
                    } label: {
                        Label("Session actions", systemImage: "ellipsis").labelStyle(.iconOnly)
                    }
                }
            }
        }
    }
}

private struct PreviewSubtitle: ViewModifier {
    let subtitle: String?
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *), let subtitle {
            content.navigationSubtitle(subtitle)
        } else {
            content
        }
    }
}

/// A deterministic preview of a transport loss after the dock has observed a
/// healthy stream. This exercises the same local transition as a real viewer,
/// so it does not need a preview-only production flag.
private struct PreviewConnectionDrop: View {
    let detail: SessionDetail
    let activity: ActivityPulseStore
    var transcript: [String] = []
    @State private var connection: SessionRealtimeConnection = .connected

    var body: some View {
        SessionScreenPreview(
            detail: detail,
            activity: activity,
            transcript: transcript,
            connection: connection
        )
        .onAppear {
            connection = .disconnected
        }
    }
}

// MARK: - Previews

#Preview("Working · Codex · strip busy · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex",
            canSteer: true,
            canQueue: true,
            executing: true,
            placeholder: "Steer this turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "shell",
                observedAt: isoDate(secondsAgo: 31),
                validUntil: isoDate(secondsAgo: -90),
                primaryKey: "executing",
                primaryLabel: "Using shell",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            ),
            transcriptPreviewJSON: toolPreviewJSON(
                tool: "shell",
                command: "rsync -a --info=progress2 /vol/backups/exact /mnt/restore"
            )
        ),
        activity: codexBurst(),
        draft: "Check the newest restore checkpoint"
    )
    .preferredColorScheme(.dark)
}

#Preview("Working · quiet tool · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "claude",
            executing: true,
            placeholder: "Steer this turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 120),
                validUntil: isoDate(secondsAgo: -480),
                primaryKey: "executing",
                primaryLabel: "Using Bash",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: ActivityPulseStore(),
        transcript: ["The command is still running. No new output has arrived."]
    )
    .preferredColorScheme(.dark)
}
#Preview("Composer · sending · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "claude",
            canSteer: false,
            canQueue: true,
            executing: true,
            placeholder: "Queue for next turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 4),
                validUntil: isoDate(secondsAgo: -45),
                primaryKey: "executing",
                primaryLabel: "Using Bash",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: claudeSparse(),
        draft: "Send the latest restore checkpoint",
        isSending: true
    )
    .preferredColorScheme(.dark)
}

#Preview("Working · Claude · long tool · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "claude",
            canSteer: false,
            canQueue: true,
            executing: true,
            placeholder: "Queue for next turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 72),
                validUntil: isoDate(secondsAgo: -120),
                primaryKey: "executing",
                primaryLabel: "Using Bash",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            ),
            transcriptPreviewJSON: toolPreviewJSON(
                tool: "Bash",
                command: "pg_restore --verbose --jobs=4 -d longhouse backups/2026-09-01.dump"
            )
        ),
        activity: claudeSparse(),
        transcript: ["Running the full restore now. This will take a while."],
        draft: "Queue a checksum comparison for the next turn",
        queuedInputCount: 1
    )
    .preferredColorScheme(.dark)
}

#Preview("Thinking · Codex streaming · Light") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex",
            canSteer: true,
            canQueue: true,
            executing: true,
            placeholder: "Steer this turn",
            stateFactsJSON: factsJSON(
                activity: "thinking",
                observedAt: isoDate(secondsAgo: 8),
                validUntil: isoDate(secondsAgo: -60),
                primaryKey: "thinking",
                primaryLabel: "Thinking",
                primaryTone: "thinking",
                access: ("live_control", "Live control", "success")
            ),
            transcriptPreviewJSON: provisionalTextPreviewJSON(
                "The manifest lists 4,930 objects.\\nComparing checksums for the last 60 now."
            )
        ),
        activity: codexBurst()
    )
    .preferredColorScheme(.light)
}

#Preview("Needs approval · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "claude",
            executing: false,
            stateFactsJSON: factsJSON(
                activity: "blocked",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 140),
                primaryKey: "needs_approval",
                primaryLabel: "Needs approval",
                primaryTone: "blocked",
                access: ("live_control", "Live control", "success"),
                pendingInteractionKind: "approval"
            ),
            pauseRequestJSON: approvalPreviewJSON()
        ),
        activity: seededActivity([(9.5, .toolStart)]),
    )
    .preferredColorScheme(.dark)
}

#Preview("Activity expired · connected viewer · Light") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex",
            canSteer: true,
            canQueue: true,
            executing: true,
            placeholder: "Steer this turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "shell",
                observedAt: isoDate(secondsAgo: 100),
                validUntil: isoDate(secondsAgo: 10),
                primaryKey: "executing",
                primaryLabel: "Using shell",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            ),
            transcriptPreviewJSON: toolPreviewJSON(
                tool: "shell",
                command: "tail -f /var/log/restore.log"
            )
        ),
        activity: seededActivity([(1.5, .toolStart)]),
        transcript: ["The restore has not reported a new checkpoint."],
        connection: .connected
    )
    .preferredColorScheme(.light)
}

#Preview("Ledger · Accessibility text") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex", canSteer: true, canQueue: true, executing: true,
            placeholder: "Steer this turn",
            stateFactsJSON: factsJSON(
                activity: "executing", tool: "shell",
                observedAt: isoDate(secondsAgo: 100), validUntil: isoDate(secondsAgo: 10),
                primaryKey: "executing", primaryLabel: "Using shell", primaryTone: "running",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: seededActivity([]),
        transcript: ["The restore has not reported a new checkpoint."],
        connection: .connected
    )
    .preferredColorScheme(.dark)
    .environment(\.dynamicTypeSize, .accessibility2)
}

#Preview("Activity uncertain · stream disconnected · Dark") {

    PreviewConnectionDrop(
        detail: .mock(
            provider: "claude",
            executing: true,
            placeholder: "Queue for next turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 45),
                validUntil: isoDate(secondsAgo: -45),
                primaryKey: "executing",
                primaryLabel: "Using Bash",
                primaryTone: "running",
                access: ("live_control", "Live control", "success")
            ),
            transcriptPreviewJSON: toolPreviewJSON(
                tool: "Bash",
                command: "pg_restore --verbose backups/latest.dump"
            )
        ),
        activity: seededActivity([(2.0, .toolResult)]),
        transcript: ["The session was working when this viewer lost updates."]
    )
    .preferredColorScheme(.dark)
}

#Preview("Idle · disconnected after connection · Dark") {
    PreviewConnectionDrop(
        detail: .mock(
            provider: "codex",
            executing: false,
            stateFactsJSON: factsJSON(
                activity: "quiescent",
                observedAt: isoDate(secondsAgo: 24),
                primaryKey: "idle",
                primaryLabel: "Idle",
                primaryTone: "idle",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: ActivityPulseStore(),
        transcript: ["Restore complete: 9.4 GB, 0 errors. Checksums match the manifest."]
    )
    .preferredColorScheme(.dark)
}

#Preview("Connecting startup · checking · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "claude",
            executing: true,
            placeholder: "Queue for next turn",
            stateFactsJSON: factsJSON(
                activity: "executing",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 1),
                validUntil: isoDate(secondsAgo: -30),
                primaryKey: "executing",
                primaryLabel: "Using Bash",
                primaryTone: "running"
            )
        ),
        activity: seededActivity([(1.2, .toolStart)]),
        transcript: ["The provider is starting; waiting for the first live update."],
        connection: .connecting
    )
    .preferredColorScheme(.dark)
}

#Preview("Approval · disconnected after connection · Dark") {
    PreviewConnectionDrop(
        detail: .mock(
            provider: "claude",
            executing: false,
            stateFactsJSON: factsJSON(
                activity: "blocked",
                tool: "Bash",
                observedAt: isoDate(secondsAgo: 8),
                primaryKey: "needs_approval",
                primaryLabel: "Needs approval",
                primaryTone: "blocked",
                access: ("live_control", "Live control", "success"),
                pendingInteractionKind: "approval"
            ),
            pauseRequestJSON: approvalPreviewJSON()
        ),
        activity: seededActivity([(1.5, .toolStart)]),
        transcript: ["The provider is waiting for approval before continuing."]
    )
    .preferredColorScheme(.dark)
}

#Preview("Idle · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex",
            executing: false,
            stateFactsJSON: factsJSON(
                activity: "quiescent",
                observedAt: isoDate(secondsAgo: 240),
                primaryKey: "idle",
                primaryLabel: "Idle",
                primaryTone: "idle",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: ActivityPulseStore(),
        transcript: ["Restore complete: 9.4 GB, 0 errors. Checksums match the manifest."]
    )
    .preferredColorScheme(.dark)
}

/// The shape behind the old "No recent activity (last: idle)": provider
/// evidence expired while the control lease stayed healthy. The dock names what
/// was last seen and shows how long ago, without claiming a current state.
#Preview("Last observed idle · live lease · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "cursor",
            executing: false,
            stateFactsJSON: factsJSON(
                activity: "unknown",
                observedAt: isoDate(secondsAgo: 10_920),
                primaryKey: "no_recent_activity",
                primaryLabel: "Last observed idle",
                primaryTone: "quiet",
                access: ("live_control", "Live control", "success")
            )
        ),
        activity: ActivityPulseStore(),
        transcript: ["All 136 unit tests across gating, middleware, schema validation, and CRUD passed cleanly."]
    )
    .preferredColorScheme(.dark)
}

#Preview("Control offline · chip · Dark") {
    SessionScreenPreview(
        detail: .mock(
            provider: "codex",
            executing: false,
            stateFactsJSON: factsJSON(
                runLifecycle: "running",
                activity: "unknown",
                observedAt: isoDate(secondsAgo: 600),
                controlConnection: "disconnected",
                sendInput: "unavailable",
                primaryKey: "activity_unknown",
                primaryLabel: "Activity unknown",
                primaryTone: "quiet",
                access: ("control_offline", "Control offline", "warning")
            )
        ),
        activity: ActivityPulseStore(),
        transcript: ["Restore complete: 9.4 GB, 0 errors. Checksums match the manifest."]
    )
    .preferredColorScheme(.dark)
}

#Preview("Ended Helm · Resume command") {
    ResumeCommandSheet(
        intent: SessionResumeIntent(
            sessionId: "019fc50b-1111-4111-8111-111111111111",
            provider: "codex",
            machineId: "cinder",
            machineLabel: "cinder",
            cwd: "/Users/example/code",
            available: true,
            reason: nil,
            argv: ["longhouse", "codex", "--cwd", "/Users/example/code", "--resume-session", "019fc50b-1111-4111-8111-111111111111"],
            command: "longhouse codex --cwd /Users/example/code --resume-session 019fc50b-1111-4111-8111-111111111111",
            handoff: "terminal_command"
        ),
        unexpectedStop: false
    )
}

#Preview("Crashed Helm · Resume command") {
    ResumeCommandSheet(
        intent: SessionResumeIntent(
            sessionId: "019fc50b-1111-4111-8111-111111111111",
            provider: "codex",
            machineId: "cinder",
            machineLabel: "cinder",
            cwd: "/Users/example/code",
            available: true,
            reason: nil,
            argv: ["longhouse", "codex", "--cwd", "/Users/example/code", "--resume-session", "019fc50b-1111-4111-8111-111111111111"],
            command: "longhouse codex --cwd /Users/example/code --resume-session 019fc50b-1111-4111-8111-111111111111",
            handoff: "terminal_command"
        ),
        unexpectedStop: true
    )
}

// MARK: - Transcript load-state previews (one shared overlay component)

#Preview("Transcript · hard error · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .hardError("Couldn't load session: The Internet connection appears to be offline."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · refresh banner · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .contentWithRefreshError("Live update temporarily unavailable. Showing saved messages."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · empty refresh error · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .emptyWithRefreshError("Live update temporarily unavailable. Retry to check again."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · syncing refresh error · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .syncingWithRefreshError("Archive sync is delayed. Retry to check again."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · loading · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(state: .loading, onRetry: {})
    }
    .preferredColorScheme(.dark)
}

#Preview("Session · first paint loading shell · Dark") {
    NavigationStack {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            TranscriptStateOverlay(state: .loading, onRetry: {})
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            SessionLoadingDock()
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
        }
        .navigationTitle("Meeting prep research")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                SessionNavigationHeader(
                    title: "Meeting prep research",
                    subtitle: "Cursor · zeta · cinder"
                )
            }
            ToolbarItem(placement: .topBarTrailing) {
                Image(systemName: "ellipsis")
                    .foregroundStyle(.secondary)
            }
        }
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · restoring · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(state: .restoring, onRetry: {})
    }
    .preferredColorScheme(.dark)
}

// MARK: - Provider facts chrome

/// The session screen's top inset, in the same shell the real screen uses:
/// a navigation bar above, a scrolling transcript below. A bare view over a
/// transparent canvas renders the bar material and secondary text as nothing.
private struct ProviderChromePreview: View {
    let recap: SessionRecap?
    let usage: SessionUsageLatest?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Rebuilt the DRIVE page as a console instrument and pushed v35.").font(.body)
                    Text("Next: check it in the truck, then pick up a1ac834 for v40.").font(.body)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                if let recap {
                    VStack(spacing: 0) {
                        SessionRecapBanner(recap: recap, usage: usage)
                        Divider()
                    }
                } else if let usage {
                    VStack(spacing: 0) {
                        SessionUsageChip(usage: usage)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 4)
                            .background(.bar)
                        Divider()
                    }
                }
            }
            .navigationTitle("G55 DRIVE page")
            .navigationBarTitleDisplayMode(.inline)
            .modifier(PreviewSubtitle(subtitle: "Claude · g55 · cinder"))
        }
    }
}

#Preview("Recap banner + usage chip · Dark") {
    ProviderChromePreview(
        recap: SessionRecap(
            text: "Rebuilt the G55 tablet DRIVE page as a center-console instrument showing what the factory cluster can't. v35 is installed and pushed. Next: check it in the truck.",
            at: "2026-09-02T23:10:05.294000+00:00"
        ),
        usage: SessionUsageLatest(model: "claude-opus-5", effort: "high", contextTokens: 501_447, outputTokens: 177, thinkingTokens: 0, at: "2026-09-02T23:07:00Z")
    )
    .preferredColorScheme(.dark)
}

#Preview("Usage chip alone · Light") {
    ProviderChromePreview(
        recap: nil,
        usage: SessionUsageLatest(model: "openai/gpt-5.6-sol", effort: "xhigh", contextTokens: 25_210, outputTokens: 80, thinkingTokens: 33, at: "2026-09-02T23:07:00Z", contextWindow: 258_400)
    )
    .preferredColorScheme(.light)
}
