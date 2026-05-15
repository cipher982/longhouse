import Foundation

extension APISessionCapabilitiesResponse {
    var sessionCapabilities: SessionCapabilities {
        SessionCapabilities(
            liveControlAvailable: liveControlAvailable ?? false,
            hostReattachAvailable: hostReattachAvailable ?? false,
            replyToLiveSessionAvailable: replyToLiveSessionAvailable ?? false,
            canQueueNextInput: canQueueNextInput,
            canSteerActiveTurn: canSteerActiveTurn,
            displayLabel: displayLabel,
            displayDetail: displayDetail,
            displayTone: displayTone
        )
    }
}

extension APISessionRuntimeDisplayResponse {
    var sessionRuntimeDisplay: SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: truthTier,
            signalTier: signalTier,
            state: state,
            tone: tone,
            headline: headline,
            detail: detail,
            phaseLabel: phaseLabel,
            compactToolLabel: compactToolLabel,
            isLive: isLive ?? false,
            isExecuting: isExecuting ?? false,
            needsAttention: needsAttention ?? false,
            isIdle: isIdle ?? false,
            isManagedLocalTruth: isManagedLocalTruth ?? false,
            hasSignal: hasSignal ?? false,
            controlPath: controlPath,
            activityRecency: activityRecency,
            lifecycle: lifecycle,
            hostState: hostState,
            terminalReason: terminalReason
        )
    }
}

extension APIHostObservationResponse {
    var hostObservation: HostObservation {
        HostObservation(state: state ?? "unknown", lastSeenAt: lastSeenAt, source: source)
    }
}

extension APIProcessObservationResponse {
    var processObservation: ProcessObservation {
        ProcessObservation(
            status: status ?? "unknown",
            pid: pid,
            processStartTime: processStartTime,
            observedAt: observedAt,
            lastSeenAt: lastSeenAt,
            sourceMtime: sourceMtime,
            sourcePath: sourcePath,
            reason: reason,
            source: source
        )
    }
}

extension APIPhaseObservationResponse {
    var phaseObservation: PhaseObservation {
        PhaseObservation(kind: kind, tool: tool, source: source, observedAt: observedAt, expiresAt: expiresAt)
    }
}

extension APIActivityObservationResponse {
    var activityObservation: ActivityObservation {
        ActivityObservation(
            lastTranscriptAt: lastTranscriptAt,
            lastRuntimeSignalAt: lastRuntimeSignalAt,
            lastProgressAt: lastProgressAt
        )
    }
}

extension APILifecycleFactResponse {
    var lifecycleFact: LifecycleFact {
        LifecycleFact(state: state ?? "unknown", reason: reason, observedAt: observedAt)
    }
}

extension APISessionLivenessFactsResponse {
    var sessionLivenessFacts: SessionLivenessFacts {
        SessionLivenessFacts(
            controlPath: controlPath,
            processState: processState,
            host: host.hostObservation,
            process: process.processObservation,
            phase: phase.phaseObservation,
            activity: activity.activityObservation,
            lifecycle: lifecycle.lifecycleFact
        )
    }
}

extension APITimelineBadgePresentationResponse {
    var timelineBadgePresentation: TimelineBadgePresentation {
        TimelineBadgePresentation(label: label, tone: tone)
    }
}

extension APITimelineStatusPresentationResponse {
    var timelineStatusPresentation: TimelineStatusPresentation {
        TimelineStatusPresentation(label: label, tone: tone, seenAt: seenAt, seenAtPrefix: seenAtPrefix)
    }
}

extension APITimelineCardPresentationResponse {
    var timelineCardPresentation: TimelineCardPresentation {
        TimelineCardPresentation(
            ownership: ownership.timelineBadgePresentation,
            status: status?.timelineStatusPresentation,
            borderTone: borderTone ?? "inactive"
        )
    }
}

extension APISessionResponse {
    var sessionDetail: SessionDetail {
        SessionDetail(
            id: id,
            provider: provider,
            project: project,
            cwd: cwd,
            gitBranch: gitBranch,
            summary: summary,
            summaryTitle: summaryTitle,
            presenceState: presenceState,
            presenceTool: presenceTool,
            userState: userState ?? "active",
            status: status,
            lastActivityAt: lastActivityAt,
            displayPhase: displayPhase,
            activeTool: activeTool,
            homeLabel: homeLabel,
            originLabel: originLabel,
            capabilities: capabilities.sessionCapabilities,
            runtimeDisplay: runtimeDisplay?.sessionRuntimeDisplay,
            runtimeFacts: runtimeFacts?.sessionLivenessFacts,
            loopMode: loopMode.flatMap(SessionLoopMode.init(rawValue:))
        )
    }
}

extension APITimelineSessionCardResponse {
    var sessionSummary: SessionSummary {
        SessionSummary(
            id: head.id,
            title: head.summaryTitle ?? head.summary ?? head.provider,
            presenceState: head.presenceState ?? "unknown",
            provider: head.provider,
            project: head.project,
            lastActivityAt: head.lastActivityAt,
            summary: head.summary,
            userState: head.userState,
            status: head.status,
            displayPhase: head.displayPhase,
            presenceTool: head.presenceTool,
            activeTool: head.activeTool,
            gitBranch: head.gitBranch,
            homeLabel: head.homeLabel,
            headOriginLabel: headOriginLabel,
            timelineAnchorAt: timelineAnchorAt ?? head.timelineAnchorAt,
            userMessages: head.userMessages,
            toolCalls: head.toolCalls,
            liveControlAvailable: head.capabilities.liveControlAvailable,
            hostReattachAvailable: head.capabilities.hostReattachAvailable,
            replyToLiveSessionAvailable: head.capabilities.replyToLiveSessionAvailable,
            runtimeDisplay: head.runtimeDisplay?.sessionRuntimeDisplay,
            runtimeFacts: head.runtimeFacts?.sessionLivenessFacts,
            timelineCard: head.timelineCard.timelineCardPresentation
        )
    }
}
