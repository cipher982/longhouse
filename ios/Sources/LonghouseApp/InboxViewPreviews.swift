#if DEBUG
import SwiftUI

// MARK: - Timeline card mock factory

private func iso(_ secondsAgo: TimeInterval) -> String {
    let d = Date().addingTimeInterval(-secondsAgo)
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: d)
}

private func mockSession(
    id: String,
    project: String,
    title: String,
    summary: String,
    summaryStatus: String? = nil,
    provider: String = "claude",
    branch: String? = "main",
    statusLabel: String,
    statusTone: String,
    activityRecency: String,
    anchorSecondsAgo: TimeInterval,
    seenAtSecondsAgo: TimeInterval? = nil,
    seenAtPrefix: String = "Updated",
    phaseExpiresInSeconds: TimeInterval? = 12,
    isManaged: Bool = true,
    turns: Int = 4,
    tools: Int = 12
) -> SessionSummary {
    let cardStatus = TimelineStatusPresentation(
        label: statusLabel,
        tone: statusTone,
        seenAt: seenAtSecondsAgo.map(iso),
        seenAtPrefix: seenAtPrefix
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
        userState: "active",
        status: nil,
        displayPhase: statusLabel,
        presenceTool: nil,
        activeTool: nil,
        gitBranch: branch,
        homeLabel: nil,
        headOriginLabel: nil,
        timelineAnchorAt: iso(anchorSecondsAgo),
        userMessages: turns,
        toolCalls: tools,
        liveControlAvailable: isManaged,
        hostReattachAvailable: false,
        replyToLiveSessionAvailable: isManaged,
        runtimeDisplay: display,
        timelineCard: card
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
            project: "zeta",
            title: "Confirmed GCP Credentials Path Blocker MR Review",
            summary: "Re-verified MR 1009 against design docs and test plan CT-4, confirming path mismatch from legacy SSM to required self-service shape.",
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
            summary: "Investigating tone-mapping regressions; runner went silent mid-job.",
            provider: "claude",
            branch: "feat/tone-mapping",
            statusLabel: "Idle",
            statusTone: "idle",
            activityRecency: "stale",
            anchorSecondsAgo: 18 * 60,
            seenAtSecondsAgo: 95,
            phaseExpiresInSeconds: -45 // server already declared this stale
        ),
        mockSession(
            id: "6",
            project: "sauron",
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
                TimelineSessionCardRow(session: session, emphasized: false, connectionState: .healthy)
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
                TimelineSessionCardRow(session: session, emphasized: false, connectionState: .healthy)
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
            title: "Ready — backend has summary",
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
                TimelineSessionCardRow(session: session, emphasized: false, connectionState: .healthy)
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
    .preferredColorScheme(.dark)
}

#Preview("Connection states") {
    let session = mockSession(
        id: "1",
        project: "chaos",
        title: "Chaos BranchTrace Blog Post Refinement",
        summary: "Same card under each global connection state.",
        statusLabel: "Thinking",
        statusTone: "thinking",
        activityRecency: "live",
        anchorSecondsAgo: 5,
        seenAtSecondsAgo: 5
    )
    let cases: [(String, ConnectionState)] = [
        ("connecting", .connecting),
        ("healthy", .healthy),
        ("reconnecting", .reconnecting),
        ("offline", .offline),
    ]
    return ScrollView {
        VStack(alignment: .leading, spacing: 18) {
            ForEach(cases, id: \.0) { label, state in
                VStack(alignment: .leading, spacing: 0) {
                    Text(label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 4)
                    ConnectionStatusStrip(state: state)
                    TimelineSessionCardRow(session: session, emphasized: false, connectionState: state)
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
