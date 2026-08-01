#if DEBUG
import SwiftUI

// MARK: - Timeline card mock factory

private func iso(_ secondsAgo: TimeInterval) -> String {
    let d = Date().addingTimeInterval(-secondsAgo)
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: d)
}

private func previewStateFacts(
    statusLabel: String,
    statusTone: String,
    isManaged: Bool,
    unread: Bool,
    resultSecondsAgo: TimeInterval?,
    lastResultOutcome: String?,
    workingSet overrideWorkingSet: String?,
    pendingInteractionKind explicitPendingInteraction: String?
) -> SessionStateFacts {
    let available = SessionStateAction(state: "available", reason: nil)
    let unavailable = SessionStateAction(state: "unavailable", reason: "preview_not_granted")
    let closed = statusLabel == "Closed"
    let activity: String = switch statusTone {
    case "thinking": "thinking"
    case "running": "executing"
    case "blocked": "blocked"
    case "stalled": "stalled"
    case "idle": "quiescent"
    default: closed ? "quiescent" : "unknown"
    }
    let pendingInteractionKind = explicitPendingInteraction
        ?? (statusTone == "blocked" ? "permission" : nil)
    let workingSet = overrideWorkingSet
        ?? ((!closed && (pendingInteractionKind != nil || activity == "thinking" || activity == "executing"))
            ? "open"
            : "history")

    return SessionStateFacts(
        contractVersion: 2,
        presentationPolicyVersion: 1,
        mode: unread ? "console" : (isManaged ? "helm" : "shadow"),
        dispositionState: closed ? "closed" : "open",
        launchState: nil,
        runLifecycle: closed ? "ended" : "running",
        activityState: activity,
        activityRawKind: nil,
        activityTool: nil,
        activitySource: "preview",
        activityObservedAt: nil,
        activityValidUntil: nil,
        controlOwnership: isManaged ? "owned" : "unowned",
        controlConnection: isManaged ? "connected" : "not_applicable",
        workingSet: workingSet,
        unread: unread,
        lastResultAt: resultSecondsAgo.map(iso),
        lastResultOutcome: lastResultOutcome,
        startTurn: isManaged ? available : unavailable,
        sendInput: isManaged ? available : unavailable,
        interrupt: isManaged ? available : unavailable,
        terminate: isManaged ? available : unavailable,
        reattach: unavailable,
        resume: unavailable,
        pendingInteractionKind: pendingInteractionKind,
        transcriptConvergence: "current",
        primary: SessionStateLabel(
            key: statusTone,
            label: statusLabel,
            tone: statusTone,
            observedAt: nil
        ),
        access: nil,
        transcript: nil
    )
}

private func mockSession(
    id: String,
    project: String,
    title: String,
    summary: String,
    summaryStatus: String? = nil,
    summaryTitle: String? = nil,
    matchSnippet: String? = nil,
    provider: String = "claude",
    branch: String? = "main",
    machine: String? = "macbook-pro",
    statusLabel: String,
    statusTone: String,
    activityRecency: String,
    anchorSecondsAgo: TimeInterval,
    seenAtSecondsAgo: TimeInterval? = nil,
    seenAtPrefix: String = "Updated",
    phaseExpiresInSeconds: TimeInterval? = 12,
    isManaged: Bool = true,
    unread: Bool = false,
    resultSecondsAgo: TimeInterval? = nil,
    lastResultOutcome: String? = nil,
    workingSet: String? = nil,
    pendingInteractionKind: String? = nil,
    turns: Int = 4,
    tools: Int = 12
) -> SessionSummary {
    let cardStatus = TimelineStatusPresentation(
        label: statusLabel,
        tone: statusTone,
        seenAt: seenAtSecondsAgo.map(iso),
        seenAtPrefix: seenAtPrefix
    )
    let stateFacts = previewStateFacts(
        statusLabel: statusLabel,
        statusTone: statusTone,
        isManaged: isManaged,
        unread: unread,
        resultSecondsAgo: resultSecondsAgo,
        lastResultOutcome: lastResultOutcome,
        workingSet: workingSet,
        pendingInteractionKind: pendingInteractionKind
    )
    let card = TimelineCardPresentation(
        ownership: TimelineBadgePresentation(label: isManaged ? "Managed" : "Unmanaged", tone: "neutral"),
        status: cardStatus,
        borderTone: statusTone
    )
    let display = SessionRuntimeDisplay(
        truthTier: "live",
        signalTier: "live",
        state: statusLabel.lowercased(),
        tone: statusTone,
        headline: statusLabel,
        detail: nil,
        phaseLabel: statusLabel,
        compactToolLabel: nil,
        isLive: activityRecency == "live",
        isExecuting: statusTone == "running" || statusTone == "thinking",
        needsAttention: statusTone == "blocked",
        isIdle: statusLabel == "Idle",
        isStalled: false,
        isManagedLocalTruth: isManaged,
        hasSignal: true,
        controlPath: isManaged ? "managed" : "unmanaged",
        activityRecency: activityRecency,
        lifecycle: statusLabel == "Closed" ? "closed" : "running",
        hostState: "unknown",
        terminalReason: nil
    )
    _ = phaseExpiresInSeconds
    return SessionSummary(
        id: id,
        title: title,
        presenceState: statusLabel.lowercased(),
        provider: provider,
        project: project,
        lastActivityAt: iso(anchorSecondsAgo),
        summary: summary,
        summaryStatus: summaryStatus,
        matchSnippet: matchSnippet,
        summaryTitle: summaryTitle,
        userState: "active",
        status: nil,
        displayPhase: statusLabel,
        presenceTool: nil,
        activeTool: nil,
        gitBranch: branch,
        homeLabel: machine,
        headOriginLabel: nil,
        timelineAnchorAt: iso(anchorSecondsAgo),
        userMessages: turns,
        toolCalls: tools,
        liveControlAvailable: isManaged,
        hostReattachAvailable: false,
        replyToLiveSessionAvailable: isManaged,
        runtimeDisplay: display,
        timelineCard: card,
        stateFacts: stateFacts
    )
}

#Preview("Timeline cards — all states") {
    let sessions: [SessionSummary] = [
        mockSession(
            id: "1",
            project: "chaos",
            title: "Chaos BranchTrace Blog Post Refinement",
            summary: "Session refined the Chaos project blog post by critiquing and removing the branch cards section to avoid dilution. Implemented cuts to personal anecdotes…",
            statusLabel: "Thinking",
            statusTone: "thinking",
            activityRecency: "live",
            anchorSecondsAgo: 5,
            seenAtSecondsAgo: 5
        ),
        mockSession(
            id: "2",
            project: "acme",
            title: "Confirmed credentials path blocker in PR review",
            summary: "Re-verified PR 1009 against design docs and test plan CT-4, confirming path mismatch from legacy config to required self-service shape.",
            provider: "codex",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "live",
            anchorSecondsAgo: 11,
            seenAtSecondsAgo: 11,
            isManaged: false,
            turns: 4,
            tools: 94
        ),
        mockSession(
            id: "3",
            project: "zerg",
            title: "Zerg iOS Chat Hardening",
            summary: "Implemented Hatch review fixes for iOS chat including LazyVStack restoration for sticky bottom and inflight send reset.",
            provider: "codex",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "live",
            anchorSecondsAgo: 5 * 60,
            seenAtSecondsAgo: 11
        ),
        mockSession(
            id: "4",
            project: "longhouse",
            title: "Refresh Token Rotation Hardening",
            summary: "Implemented RFC 9700 rotating refresh tokens across backend and web frontend.",
            summaryTitle: "Now wiring web interceptor retries",
            statusLabel: "Using bash",
            statusTone: "running",
            activityRecency: "live",
            anchorSecondsAgo: 12,
            seenAtSecondsAgo: 12
        ),
        mockSession(
            id: "7",
            project: "runtime",
            title: "Approval Needed for Shell Command",
            summary: "The managed session is waiting on a permission decision before it can continue the current turn.",
            provider: "gemini",
            statusLabel: "Blocked Shell",
            statusTone: "blocked",
            activityRecency: "live",
            anchorSecondsAgo: 20,
            seenAtSecondsAgo: 20
        ),
        mockSession(
            id: "8",
            project: "agents",
            title: "Worker Stalled During Local QA",
            summary: "The session stopped making progress during verification and needs inspection before the next action.",
            provider: "antigravity",
            statusLabel: "Stalled",
            statusTone: "stalled",
            activityRecency: "live",
            anchorSecondsAgo: 2 * 60,
            seenAtSecondsAgo: 2 * 60
        ),
        mockSession(
            id: "5",
            project: "hdr",
            title: "Photo Pipeline Rebuild",
            summary: "The provider is idle, but Longhouse still has a fresh send-capable control path to the terminal session.",
            provider: "claude",
            branch: "feat/tone-mapping",
            statusLabel: "Activity unknown",
            statusTone: "idle",
            activityRecency: "stale",
            anchorSecondsAgo: 18 * 60,
            seenAtSecondsAgo: nil,
            phaseExpiresInSeconds: -45 // server already declared this stale
        ),
        mockSession(
            id: "6",
            project: "acme-api",
            title: "Scheduler Maintenance Sweep",
            summary: "Closed cleanly after job graph drained.",
            provider: "claude",
            statusLabel: "Closed",
            statusTone: "closed",
            activityRecency: "none",
            anchorSecondsAgo: 2 * 60 * 60,
            seenAtSecondsAgo: 2 * 60 * 60,
            seenAtPrefix: "Closed",
            turns: 22,
            tools: 140
        ),
    ]

    return ScrollView {
        VStack(spacing: 12) {
            ForEach(sessions) { session in
                TimelineSessionCardRow(session: session, role: .recent, connectivityBanner: .none)
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
    .preferredColorScheme(.dark)
}

#Preview("Timeline cards — attention colors light") {
    let sessions: [SessionSummary] = [
        mockSession(
            id: "thinking-light",
            project: "zerg",
            title: "Agent Working",
            summary: "Thinking and tool use share one working treatment.",
            provider: "claude",
            statusLabel: "Thinking",
            statusTone: "thinking",
            activityRecency: "live",
            anchorSecondsAgo: 7,
            seenAtSecondsAgo: 7
        ),
        mockSession(
            id: "running-light",
            project: "zerg",
            title: "Agent Running Shell",
            summary: "Tool execution stays in the same working family as thinking.",
            provider: "codex",
            statusLabel: "Using Shell",
            statusTone: "running",
            activityRecency: "live",
            anchorSecondsAgo: 12,
            seenAtSecondsAgo: 12
        ),
        mockSession(
            id: "blocked-light",
            project: "zerg",
            title: "Needs User Attention",
            summary: "Blocked uses amber attention while red remains reserved for broken transport.",
            provider: "gemini",
            statusLabel: "Blocked Shell",
            statusTone: "blocked",
            activityRecency: "live",
            anchorSecondsAgo: 30,
            seenAtSecondsAgo: 30
        ),
        mockSession(
            id: "idle-light",
            project: "zerg",
            title: "Parked Session",
            summary: "Idle stays quiet unless a later watched-session pipeline makes it important.",
            provider: "antigravity",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "live",
            anchorSecondsAgo: 4 * 60,
            seenAtSecondsAgo: 4 * 60
        ),
    ]

    return ScrollView {
        VStack(spacing: 12) {
            ForEach(sessions) { session in
                TimelineSessionCardRow(session: session, role: .recent, connectivityBanner: .none)
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
    .preferredColorScheme(.light)
}

#Preview("Summary status — all four") {
    let sessions: [SessionSummary] = [
        mockSession(
            id: "ready",
            project: "longhouse",
            title: "Backend summary available",
            summary: "Wired summary_status into timeline payload. Single batched query joins session_tasks for the latest summary task per session, derives ready/pending/failed/unavailable in the projection layer, and threads the result through SessionResponse so iOS can render honestly.",
            summaryStatus: "ready",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "live",
            anchorSecondsAgo: 30,
            seenAtSecondsAgo: 30
        ),
        mockSession(
            id: "pending",
            project: "longhouse",
            title: "Pending — task queued/running",
            summary: "",
            summaryStatus: "pending",
            statusLabel: "Thinking",
            statusTone: "thinking",
            activityRecency: "live",
            anchorSecondsAgo: 4,
            seenAtSecondsAgo: 4
        ),
        mockSession(
            id: "failed",
            project: "longhouse",
            title: "Failed — terminal, won't auto-retry",
            summary: "",
            summaryStatus: "failed",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "stale",
            anchorSecondsAgo: 12 * 60,
            seenAtSecondsAgo: 12 * 60
        ),
        mockSession(
            id: "unavailable",
            project: "longhouse",
            title: "Unavailable — too little content",
            summary: "",
            summaryStatus: "unavailable",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "stale",
            anchorSecondsAgo: 60,
            seenAtSecondsAgo: 60,
            turns: 1,
            tools: 0
        ),
    ]
    return ScrollView {
        VStack(spacing: 12) {
            ForEach(sessions) { session in
                TimelineSessionCardRow(session: session, role: .recent, connectivityBanner: .none)
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
    .preferredColorScheme(.dark)
}

#Preview("Connection banners") {
    let session = mockSession(
        id: "1",
        project: "chaos",
        title: "Chaos BranchTrace Blog Post Refinement",
        summary: "Same card under each global connection banner.",
        statusLabel: "Thinking",
        statusTone: "thinking",
        activityRecency: "live",
        anchorSecondsAgo: 5,
        seenAtSecondsAgo: 5
    )
    let cases: [(String, TimelineConnectivityBanner)] = [
        ("hidden", .none),
        ("updating", .updating),
        ("degraded", .degraded),
        ("offline", .offline),
        ("sign in required", .authRequired),
    ]
    return ScrollView {
        VStack(alignment: .leading, spacing: 18) {
            ForEach(cases, id: \.0) { label, banner in
                VStack(alignment: .leading, spacing: 0) {
                    Text(label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 4)
                    ConnectionStatusStrip(banner: banner)
                    TimelineSessionCardRow(session: session, role: .open, connectivityBanner: banner)
                        .padding(.top, 8)
                }
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
    .preferredColorScheme(.dark)
}
#endif

#Preview("Timeline shell — toolbar") {
    TimelineView()
        .environmentObject(AppState())
}

// MARK: - Obligation-ranked timeline

#Preview("Timeline triage — obligations and results") {
    let sessions: [SessionSummary] = [
        mockSession(
            id: "needs-1",
            project: "longhouse",
            title: "Approve hosted smoke test",
            summary: "The session is waiting for permission before it can run the hosted smoke test.",
            provider: "codex",
            machine: "clifford",
            statusLabel: "Permission required",
            statusTone: "blocked",
            activityRecency: "live",
            anchorSecondsAgo: 120,
            seenAtSecondsAgo: 120
        ),
        mockSession(
            id: "result-complete",
            project: "site-optimizer",
            title: "Evaluate candidate quality regression",
            summary: "Compared the latest candidate set against the known-good scoring distribution.",
            provider: "codex",
            branch: nil,
            machine: "ml-gpu-02",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "stale",
            anchorSecondsAgo: 7 * 60,
            unread: true,
            resultSecondsAgo: 7 * 60,
            lastResultOutcome: "completed"
        ),
        mockSession(
            id: "result-failed",
            project: "sauron",
            title: "Repair nightly location refresh",
            summary: "The refresh failed while reconnecting to the source mirror.",
            branch: nil,
            machine: "clifford",
            statusLabel: "Closed",
            statusTone: "closed",
            activityRecency: "none",
            anchorSecondsAgo: 18 * 60,
            unread: true,
            resultSecondsAgo: 18 * 60,
            lastResultOutcome: "failed"
        ),
        mockSession(
            id: "open-working",
            project: "zerg",
            title: "Build iOS unread result states",
            summary: "Rendering canonical preview states and validating the timeline layout.",
            machine: "cube",
            statusLabel: "Running preview tests",
            statusTone: "running",
            activityRecency: "live",
            anchorSecondsAgo: 38,
            seenAtSecondsAgo: 38
        ),
        mockSession(
            id: "open-ready",
            project: "agent-home",
            title: "Provider registry cleanup",
            summary: "The managed session is quiet but still ready to continue on its recorded machine.",
            provider: "codex",
            branch: nil,
            machine: "macbook-pro",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "live",
            anchorSecondsAgo: 12 * 60,
            seenAtSecondsAgo: 12 * 60,
            workingSet: "open"
        ),
        mockSession(
            id: "recent-1",
            project: "g55",
            title: "NAG TCU telemetry and adaptations",
            summary: "Captured a short log and the learned adaptation row.",
            provider: "codex",
            machine: "garage-mac",
            statusLabel: "Closed",
            statusTone: "closed",
            activityRecency: "none",
            anchorSecondsAgo: 5 * 24 * 3600
        ),
    ]

    NavigationStack {
        TimelineSessionList(sessions: sessions, connectivityBanner: .none)
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Timeline")
    }
    .preferredColorScheme(.dark)
}

#Preview("Timeline search — archive results") {
    let sessions: [SessionSummary] = [
        mockSession(
            id: "search-1",
            project: "longhouse",
            title: "Native provider channel hardening",
            summary: "Verified the managed channel after an engine restart.",
            matchSnippet: "Verified the provider channel stays attached through a cold engine restart.",
            provider: "codex",
            machine: "cube",
            statusLabel: "Closed",
            statusTone: "closed",
            activityRecency: "none",
            anchorSecondsAgo: 24 * 3600,
            turns: 46
        ),
        mockSession(
            id: "search-2",
            project: "agent-home",
            title: "Managed CLI registry cleanup",
            summary: "Moved capability declarations into one schema.",
            matchSnippet: "Moved provider channel declarations into one schema so every client reads the same truth.",
            provider: "codex",
            machine: "clifford",
            statusLabel: "Closed",
            statusTone: "closed",
            activityRecency: "none",
            anchorSecondsAgo: 7 * 24 * 3600,
            turns: 19
        ),
    ]

    NavigationStack {
        TimelineSearchResultsList(
            sessions: sessions,
            query: "provider channel",
            connectivityBanner: .none
        )
        .background(Color(.systemGroupedBackground))
        .navigationTitle("Timeline")
    }
    .preferredColorScheme(.dark)
}
