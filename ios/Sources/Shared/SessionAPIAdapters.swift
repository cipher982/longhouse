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
    case "antigravity", "gemini": return "Antigravity"
    default:
        return provider.prefix(1).uppercased() + String(provider.dropFirst())
    }
}

private func timelineCardTitle(for session: APISessionResponse) -> String {
    // The server resolves a single sanitized, frozen headline (timeline_title)
    // so iOS/web/widget render identical text and the row stays stable as the
    // live summary drifts. Prefer it whenever present.
    if let resolved = compactSessionText(session.timelineTitle) {
        return resolved
    }
    // Fallback ONLY for cached/pre-anchor payloads that predate timeline_title.
    // This path is intentionally NOT re-sanitized in Swift (that would fork the
    // sanitizer from the server and drift); a raw first message may briefly show
    // here, but it self-heals to the sanitized server title on the next fetch.
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
            turnState: turnState,
            canStartTurn: canStartTurn,
            startTurnBlockedBy: startTurnBlockedBy,
            canInterruptActiveTurn: canInterruptActiveTurn,
            attachImages: attachImages,
            stalenessReason: stalenessReason
        )
    }
}

private extension APISessionActionAvailability {
    var sessionStateAction: SessionStateAction {
        SessionStateAction(state: state, reason: reason)
    }
}

private extension APISessionPresentationLabel {
    var sessionStateLabel: SessionStateLabel {
        SessionStateLabel(key: key, label: label, tone: tone, observedAt: observedAt)
    }
}

private extension APISessionStateFacts {
    var sessionStateFacts: SessionStateFacts {
        SessionStateFacts(
            contractVersion: stateContractVersion ?? 1,
            presentationPolicyVersion: presentationPolicyVersion ?? 1,
            mode: mode,
            dispositionState: disposition.state,
            launchState: launch?.state,
            runLifecycle: run?.lifecycle,
            activityState: activity.state,
            activityTool: activity.tool,
            activityObservedAt: activity.observedAt,
            activityValidUntil: activity.validUntil,
            controlOwnership: control.ownership,
            controlConnection: control.connection,
            startTurn: control.actions.startTurn?.sessionStateAction,
            sendInput: control.actions.sendInput.sessionStateAction,
            interrupt: control.actions.interrupt.sessionStateAction,
            terminate: control.actions.terminate.sessionStateAction,
            reattach: control.actions.reattach.sessionStateAction,
            resume: control.actions.resume.sessionStateAction,
            pendingInteractionKind: pendingInteraction?.kind,
            transcriptConvergence: transcript.convergence,
            primary: presentation.primary?.sessionStateLabel,
            access: presentation.access?.sessionStateLabel,
            transcript: presentation.transcript?.sessionStateLabel
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
            terminalReason: terminalReason?.rawValue,
            pauseRequest: pauseRequest?.sessionPauseRequest
        )
    }
}

extension APISessionPauseQuestionOptionResponse {
    var sessionPauseQuestionOption: SessionPauseQuestionOption {
        SessionPauseQuestionOption(label: label, description: description, value: value)
    }
}

extension APISessionPauseQuestionResponse {
    var sessionPauseQuestion: SessionPauseQuestion {
        SessionPauseQuestion(
            id: id,
            header: header,
            question: question,
            multiSelect: multiSelect ?? false,
            options: (options ?? []).map(\.sessionPauseQuestionOption)
        )
    }
}

extension APISessionPauseRequestProjectionResponse {
    var sessionPauseRequest: SessionPauseRequest {
        SessionPauseRequest(
            id: id,
            sessionId: sessionId,
            runtimeKey: runtimeKey,
            kind: kind,
            status: status,
            provider: provider,
            canRespond: canRespond,
            title: title,
            summary: summary,
            toolName: toolName,
            questions: (questions ?? []).map(\.sessionPauseQuestion),
            occurredAt: occurredAt,
            lastSeenAt: lastSeenAt,
            resolvedAt: resolvedAt,
            expiresAt: expiresAt
        )
    }
}

extension APISessionTranscriptPreviewResponse {
    var sessionTranscriptPreview: SessionTranscriptPreview {
        SessionTranscriptPreview(
            eventId: eventId,
            text: text,
            role: role,
            toolName: toolName,
            toolInputJSON: toolInputJson,
            toolOutputText: toolOutputText,
            toolCallId: toolCallId,
            toolCallState: toolCallState.flatMap(ToolCallState.init(rawValue:)),
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
            title: timelineCardTitle(for: self),
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
            stateFacts: DefaultUnknownSessionStateFacts(
                wrappedValue: sessionState.sessionStateFacts
            ),
            transcriptPreview: transcriptPreview?.sessionTranscriptPreview
        )
    }
}

extension APIEventResponse {
    var sessionEvent: SessionEvent {
        SessionEvent(
            id: id.sessionEventIdentifier,
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
            eventOrigin: eventOrigin,
            mediaRefs: mediaRefs?.map(\.sessionEventMediaRef) ?? []
        )
    }
}

private extension JSONValue {
    var sessionEventIdentifier: String {
        switch self {
        case .string(let value): value
        case .int(let value): String(value)
        case .double(let value): String(value)
        case .bool(let value): String(value)
        case .array, .object, .null: "invalid-event-id"
        }
    }
}

extension APIEventMediaRefResponse {
    var sessionEventMediaRef: SessionEventMediaRef {
        SessionEventMediaRef(
            sha256: sha256,
            mediaState: mediaState,
            mimeType: mimeType,
            byteSize: byteSize,
            blobUrl: blobUrl,
            thumbUrl: thumbUrl,
            sourcePath: sourcePath,
            sourceOffset: sourceOffset,
            jsonPointer: jsonPointer,
            originalKind: originalKind
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

extension APITranscriptActionResponse {
    var sessionAction: SessionAction {
        SessionAction(
            id: id,
            kind: kind,
            provider: provider,
            source: source ?? "unknown",
            providerReason: providerReason,
            eventId: eventId
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
            action: action?.sessionAction,
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

extension APISessionWorkspaceRevisionResponse {
    var sessionWorkspaceRevision: SessionWorkspaceRevision {
        SessionWorkspaceRevision(
            latestEventId: latestEventId.map(String.init),
            latestSessionUpdatedAt: latestSessionUpdatedAt,
            latestRuntimeSignalAt: latestRuntimeSignalAt,
            runtimeVersionSum: runtimeVersionSum,
            pauseRequestCount: pauseRequestCount,
            pauseRequestFingerprint: pauseRequestFingerprint,
            managedControlCount: managedControlCount,
            managedControlFingerprint: managedControlFingerprint,
            livePreviewUpdatedAt: livePreviewUpdatedAt,
            threadSessionCount: threadSessionCount,
            fingerprint: fingerprint
        )
    }
}

extension APISessionWorkspaceResponse {
    var sessionWorkspaceResponse: SessionWorkspaceResponse {
        SessionWorkspaceResponse(
            session: session.sessionDetail,
            thread: thread.sessionThreadResponse,
            projection: projection.sessionProjectionResponse,
            workspaceRevision: workspaceRevision.sessionWorkspaceRevision
        )
    }
}

extension APIQueuedInputSummary {
    var queuedInputSummary: QueuedInputSummary {
        QueuedInputSummary(
            id: id,
            liveInputId: liveInputId,
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
            liveInputId: liveInputId,
            clientRequestId: clientRequestId,
            turn: turn.map {
                ConsoleTurnReceipt(turnId: $0.turnId, runId: $0.runId, state: $0.state)
            },
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
            summaryTitle: head.summaryTitle,
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
            timelineCard: head.timelineCard.timelineCardPresentation,
            stateFacts: head.sessionState.sessionStateFacts
        )
    }
}
