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
    provider: String = "claude",
    branch: String? = "main",
    statusLabel: String,
    statusTone: String,
    activityRecency: String,
    anchorSecondsAgo: TimeInterval,
    seenAtSecondsAgo: TimeInterval? = nil,
    seenAtPrefix: String = "Updated",
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
        isManagedLocalTruth: isManaged,
        hasSignal: true,
        controlPath: isManaged ? "managed" : "unmanaged",
        activityRecency: activityRecency,
        lifecycle: statusLabel == "Closed" ? "closed" : "running",
        hostState: nil,
        terminalReason: nil
    )
    return SessionSummary(
        id: id,
        title: title,
        presenceState: statusLabel.lowercased(),
        provider: provider,
        project: project,
        lastActivityAt: iso(anchorSecondsAgo),
        summary: summary,
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
        runtimeFacts: nil,
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
            seenAtSecondsAgo: 95
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
                TimelineSessionCardRow(session: session, emphasized: false)
            }
        }
        .padding(16)
    }
    .background(Color(.systemGroupedBackground))
}
#endif
