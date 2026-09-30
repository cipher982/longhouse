#if DEBUG
import SwiftUI
import UIKit

@MainActor
struct TimelineOpenUITestFixtureView: View {
    @State private var path: [TimelineOpenRoute] = []
    @State private var probe = ChatUITestProbe(path: UITestHooks.chatFixtureProbePath)

    private let sessions: [TimelineOpenFixtureSession]

    init() {
        sessions = (1...40).map { index in
            TimelineOpenFixtureSession(
                id: "ui-test-timeline-session-\(index)",
                title: index == 1
                    ? "A very long session title that must stay inside the navigation bar"
                    : "Timeline open fixture \(index)",
                fixture: ChatUITestFixture(name: index == 1 ? "loading-long-title" : "basic")
            )
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(sessions) { session in
                        NavigationLink(value: TimelineOpenRoute(
                            id: session.id,
                            title: session.title
                        )) {
                            TimelineSessionCardRow(session: session.summary, role: .recent)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("timeline-open-session-\(session.index)")
                    }
                }
                .padding(16)
            }
            .background(Ember.page)
            .navigationTitle("Timeline")
            .navigationDestination(for: TimelineOpenRoute.self) { route in
                if let session = sessions.first(where: { $0.id == route.id }) {
                    destination(for: session)
                } else {
                    Text("Session unavailable")
                }
            }
            .toolbar {
                // Mirror the production parent toolbar so the test includes
                // the same push-time opacity/geometry transition.
                ToolbarItem(placement: .topBarTrailing) {
                    Button {} label: {
                        Image(systemName: "plus.circle.fill")
                            .accessibilityLabel("Start session")
                    }
                    .transaction { transaction in transaction.animation = nil }
                    .opacity(path.isEmpty ? 1 : 0)
                    .disabled(!path.isEmpty)
                    .accessibilityHidden(!path.isEmpty)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {} label: {
                        Image(systemName: "gearshape")
                            .accessibilityLabel("Settings")
                    }
                    .transaction { transaction in transaction.animation = nil }
                    .opacity(path.isEmpty ? 1 : 0)
                    .disabled(!path.isEmpty)
                    .accessibilityHidden(!path.isEmpty)
                }
            }
            .task {
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard !Task.isCancelled else { return }
                WebTranscriptWebViewPool.prewarm()
            }
        }
    }

    private func destination(for session: TimelineOpenFixtureSession) -> some View {
        let client = ChatUITestWorkspaceClient(fixture: session.fixture, sessionID: session.id)
        let viewModel = SessionViewModel(
            apiFactory: { _ in client },
            streamFactory: { _, _, _, _ in client.streamSource() },
            enableRealtime: false,
            pendingInputStore: ChatUITestFixtureView.isolatedPendingInputStore()
        )
        return SessionView(
            sessionId: session.id,
            fallbackTitle: session.title,
            viewModel: viewModel,
            onTranscriptDiagnostics: { diagnostics in
                Task { @MainActor in
                    probe.record(diagnostics)
                }
            }
        )
    }
}

private struct TimelineOpenRoute: Hashable {
    let id: String
    let title: String
}

private struct TimelineOpenFixtureSession: Identifiable {
    let id: String
    let title: String
    let fixture: ChatUITestFixture

    var index: Int {
        Int(id.split(separator: "-").last ?? "0") ?? 0
    }

    var summary: SessionSummary {
        if index == 2 || index == 3 {
            let name = index == 2 ? "background-tasks-timeline" : "background-tasks-timeline-stale"
            let detail = ChatUITestWorkspaceClient.makeDetail(
                sessionID: id, events: [], title: ChatUITestWorkspaceClient.titleForFixture(name)
            )
            return SessionSummary(
                id: id, title: index == 2 ? "Background work continues" : "Expired background registry",
                presenceState: "quiescent", provider: "claude", project: "background-fixture",
                lastActivityAt: detail.lastActivityAt, runtimeDisplay: detail.runtimeDisplay,
                stateFacts: detail.stateFacts
            )
        }
        let working = index.isMultiple(of: 3)
        let statusLabel = working ? "Working" : "Idle"
        let statusTone = working ? "running" : "inactive"
        let card = TimelineCardPresentation(
            ownership: TimelineBadgePresentation(label: "Managed", tone: "neutral"),
            status: TimelineStatusPresentation(
                label: statusLabel,
                tone: statusTone,
                seenAt: "2026-07-17T12:00:00Z",
                seenAtPrefix: "Updated"
            ),
            borderTone: statusTone
        )
        let runtime = SessionRuntimeDisplay(
            truthTier: "live",
            signalTier: "live",
            state: working ? "executing" : "quiescent",
            tone: statusTone,
            headline: statusLabel,
            detail: nil,
            phaseLabel: statusLabel,
            compactToolLabel: nil,
            isLive: working,
            isExecuting: working,
            needsAttention: false,
            isIdle: !working,
            isStalled: false,
            isManagedLocalTruth: true,
            hasSignal: true,
            controlPath: "managed",
            activityRecency: working ? "live" : "recent",
            lifecycle: "running",
            hostState: "attached",
            terminalReason: nil
        )
        return SessionSummary(
            id: id,
            title: title,
            presenceState: working ? "executing" : "quiescent",
            provider: index.isMultiple(of: 2) ? "codex" : "claude",
            project: "fixture-\(index)",
            lastActivityAt: "2026-07-17T12:00:00Z",
            summary: "Fixture transcript",
            summaryStatus: nil,
            summaryTitle: nil,
            userState: "active",
            status: nil,
            displayPhase: statusLabel,
            presenceTool: nil,
            activeTool: nil,
            gitBranch: "main",
            homeLabel: nil,
            headOriginLabel: nil,
            timelineAnchorAt: "2026-07-17T12:00:00Z",
            userMessages: index,
            toolCalls: index * 2,
            runtimeDisplay: runtime,
            timelineCard: card
        )
    }
}
#endif
