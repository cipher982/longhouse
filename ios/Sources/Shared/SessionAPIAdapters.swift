import Foundation

private func compactSessionText(_ value: String?) -> String? {
    guard let value else { return nil }
    let compact = value
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .split(whereSeparator: \.isWhitespace)
        .joined(separator: " ")
    return compact.isEmpty ? nil : compact
}

private func hasMeaningfulSessionTitle(_ value: String?) -> Bool {
    guard let title = compactSessionText(value) else { return false }
    switch title.lowercased() {
    case "untitled session", "generating summary", "generating title":
        return false
    default:
        return true
    }
}

private func providerDisplayName(_ provider: String) -> String {
    switch provider.lowercased() {
    case "codex": return "Codex"
    case "claude": return "Claude"
    case "antigravity": return "Antigravity"
    case "gemini": return "Gemini"
    default:
        return provider.prefix(1).uppercased() + String(provider.dropFirst())
    }
}

private func timelineCardTitle(for session: APISessionResponse) -> String {
    if hasMeaningfulSessionTitle(session.summaryTitle), let title = compactSessionText(session.summaryTitle) {
        return title
    }
    if let firstUser = compactSessionText(session.firstUserMessage) {
        return firstUser
    }
    let provider = providerDisplayName(session.provider)
    if let project = compactSessionText(session.project), project != session.provider {
        return "New \(provider) session in \(project)"
    }
    return "New \(provider) session"
}

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
            displayTone: displayTone,
            inputMode: inputMode,
            defaultInputIntent: defaultInputIntent,
            composerEnabled: composerEnabled,
            composerPlaceholder: composerPlaceholder,
            composerDisabledReason: composerDisabledReason,
            sendDisabledReason: sendDisabledReason,
            attachImages: attachImages
        )
    }
}

extension APISessionRuntimeDisplayResponse {
    var sessionRuntimeDisplay: SessionRuntimeDisplay {
        SessionRuntimeDisplay(
            truthTier: truthTier.rawValue,
            signalTier: signalTier.rawValue,
            state: state?.rawValue,
            tone: tone.rawValue,
            headline: headline,
            detail: detail,
            phaseLabel: phaseLabel,
            compactToolLabel: compactToolLabel,
            isLive: isLive,
            isExecuting: isExecuting,
            needsAttention: needsAttention,
            isIdle: isIdle,
            isStalled: isStalled,
            isManagedLocalTruth: isManagedLocalTruth,
            hasSignal: hasSignal,
            controlPath: controlPath.rawValue,
            activityRecency: activityRecency.rawValue,
            lifecycle: lifecycle.rawValue,
            hostState: hostState.rawValue,
            terminalReason: terminalReason?.rawValue
        )
    }
}

extension APISessionTranscriptPreviewResponse {
    var sessionTranscriptPreview: SessionTranscriptPreview {
        SessionTranscriptPreview(
            eventId: eventId,
            text: text,
            eventOrigin: eventOrigin,
            timestamp: timestamp,
            isProvisional: isProvisional,
            isComplete: isComplete,
            contentCursor: contentCursor,
            isStale: isStale,
            staleReason: staleReason
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
            status: status.timelineStatusPresentation,
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
            runtimeDisplay: runtimeDisplay.sessionRuntimeDisplay,
            loopMode: loopMode.flatMap { SessionLoopMode(rawValue: $0.rawValue) },
            transcriptPreview: transcriptPreview?.sessionTranscriptPreview
        )
    }
}

extension APIEventResponse {
    var sessionEvent: SessionEvent {
        SessionEvent(
            id: id,
            role: role,
            contentText: contentText,
            toolName: toolName,
            toolInputJSON: toolInputJson,
            toolOutputText: toolOutputText,
            toolCallId: toolCallId,
            toolCallState: toolCallState.flatMap { ToolCallState(rawValue: $0.rawValue) },
            timestamp: timestamp,
            inActiveContext: inActiveContext ?? true,
            isHeadBranch: isHeadBranch ?? true,
            inputOrigin: inputOrigin?.sessionInputOrigin,
            eventOrigin: eventOrigin
        )
    }
}

extension APIInputOriginResponse {
    var sessionInputOrigin: SessionInputOrigin {
        SessionInputOrigin(
            authoredVia: SessionInputAuthoredVia(serverValue: authoredVia),
            sessionInputId: sessionInputId,
            clientRequestId: clientRequestId
        )
    }
}

extension APISessionProjectionItemResponse {
    var sessionProjectionItem: SessionProjectionItem {
        SessionProjectionItem(
            kind: kind,
            sessionId: sessionId,
            timestamp: timestamp,
            event: event?.sessionEvent,
            continuedFromSessionId: continuedFromSessionId,
            continuationKind: continuationKind,
            originLabel: originLabel,
            parentOriginLabel: parentOriginLabel,
            parentContinuationKind: parentContinuationKind,
            branchedFromEventId: branchedFromEventId
        )
    }
}

extension APISessionProjectionResponse {
    var sessionProjectionResponse: SessionProjectionResponse {
        SessionProjectionResponse(
            rootSessionId: rootSessionId,
            focusSessionId: focusSessionId,
            headSessionId: headSessionId,
            pathSessionIds: pathSessionIds,
            items: items.map(\.sessionProjectionItem),
            total: total,
            pageOffset: pageOffset ?? 0,
            branchMode: branchMode ?? "head",
            abandonedEvents: abandonedEvents ?? 0
        )
    }
}

extension APISessionThreadResponse {
    var sessionThreadResponse: SessionThreadResponse {
        SessionThreadResponse(
            rootSessionId: rootSessionId,
            headSessionId: headSessionId,
            sessions: sessions.map(\.sessionDetail)
        )
    }
}

extension APISessionWorkspaceResponse {
    var sessionWorkspaceResponse: SessionWorkspaceResponse {
        SessionWorkspaceResponse(
            session: session.sessionDetail,
            thread: thread.sessionThreadResponse,
            projection: projection.sessionProjectionResponse
        )
    }
}

extension APIQueuedInputSummary {
    var queuedInputSummary: QueuedInputSummary {
        QueuedInputSummary(
            id: id,
            text: text,
            intent: SessionInputIntent(rawValue: intent) ?? .auto,
            status: SessionInputStatus(rawValue: status) ?? .queued,
            lastError: lastError,
            createdAt: createdAt
        )
    }
}

extension APISessionInputResponse {
    var sessionInputResponse: SessionInputResponse {
        SessionInputResponse(
            outcome: SessionInputOutcome(rawValue: outcome) ?? .queued,
            inputId: inputId,
            clientRequestId: clientRequestId,
            intent: SessionInputIntent(rawValue: intent) ?? .auto,
            queued: (queued ?? []).map(\.queuedInputSummary)
        )
    }
}

extension APISessionDraftReplyResponse {
    var draftReplyResponse: DraftReplyResponse {
        DraftReplyResponse(
            draftText: draftText,
            model: model,
            generatedAt: generatedAt,
            basedOnEventIds: basedOnEventIds
        )
    }
}

extension APISessionLoopModeResponse {
    var loopModeResponse: LoopModeResponse {
        LoopModeResponse(
            sessionId: sessionId,
            loopMode: SessionLoopMode(rawValue: loopMode.rawValue) ?? .assist
        )
    }
}

extension APISessionTurnResponse {
    var sessionTurn: SessionTurn {
        SessionTurn(
            id: id,
            sessionId: sessionId,
            sessionInputId: sessionInputId,
            state: state,
            terminalPhase: terminalPhase,
            errorCode: errorCode,
            userSubmittedAt: userSubmittedAt,
            terminalAt: terminalAt
        )
    }
}

extension APISessionTurnsListResponse {
    var sessionTurnsResponse: SessionTurnsResponse {
        SessionTurnsResponse(turns: turns.map(\.sessionTurn), total: total)
    }
}

extension APITimelineSessionCardResponse {
    var sessionSummary: SessionSummary {
        SessionSummary(
            id: head.id,
            threadId: threadId,
            title: timelineCardTitle(for: head),
            presenceState: head.presenceState ?? "unknown",
            provider: head.provider,
            project: head.project,
            lastActivityAt: head.lastActivityAt,
            summary: head.summary,
            summaryStatus: head.summaryStatus,
            firstUserMessage: head.firstUserMessage,
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
            runtimeDisplay: head.runtimeDisplay.sessionRuntimeDisplay,
            timelineCard: head.timelineCard.timelineCardPresentation
        )
    }
}
