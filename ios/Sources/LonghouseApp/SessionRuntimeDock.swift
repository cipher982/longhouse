import SwiftUI

/// Observed transitions, not transport reconnection alone, earn a notice.
struct SessionLedgerNoticeState {
    enum Notice { case finished, restored }
    private var previousState: SessionLedgerEvidence?
    private var previousEvidence: SessionProviderEvidenceIdentity?
    private var interruptedEvidence: SessionProviderEvidenceIdentity?
    private var previousResultAt: String?
    private(set) var notice: Notice?
    private(set) var until: Date?

    mutating func observe(
        state: SessionLedgerEvidence,
        evidence: SessionProviderEvidenceIdentity?,
        connection: SessionRealtimeConnection,
        resultAt: String?,
        now: Date
    ) {
        defer {
            previousState = state
            previousEvidence = evidence
            previousResultAt = resultAt
        }
        guard let previousState else { return }
        if state == .uncertain {
            if previousState == .working { interruptedEvidence = previousEvidence }
            clear()
        } else if state == .attention {
            interruptedEvidence = nil
            clear()
        } else if state == .quiet {
            interruptedEvidence = nil
            if let resultAt, resultAt != previousResultAt {
                notice = .finished
                until = now.addingTimeInterval(4)
            }
        } else if connection == .connected,
                  let interruptedEvidence, let evidence,
                  evidence != interruptedEvidence {
            notice = .restored
            until = now.addingTimeInterval(4)
            self.interruptedEvidence = nil
        } else if notice == .finished || resultAt != previousResultAt {
            clear()
        }
    }

    private mutating func clear() {
        notice = nil
        until = nil
    }
}

/// The integrated Ledger status row of the control card: an evidence-gated
/// activity strip, provider headline, elapsed observation and scoped stream
/// state. Literal tool/context details stay behind deliberate disclosure.
struct SessionRuntimeDock: View {
    let detail: SessionDetail
    @ObservedObject var activity: ActivityPulseStore
    var realtimeConnection: SessionRealtimeConnection = .disconnected

    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // A streaming provider refreshes the primary label's observed_at with
    // every provisional delta. Anchor the counter on the earliest observation
    // for the current label + tool so it counts up instead of resetting.
    @State private var elapsedAnchor: ElapsedAnchor?
    @State private var evidenceNow = Date()
    @State private var evidenceDisclosure = false

    private struct ElapsedAnchor: Equatable {
        let key: String
        let start: Date
    }
    @State private var transition = SessionLedgerNoticeState()
    @State private var noticeNow = Date()

    var body: some View {
        Group {
            if detail.canDraftBeforeSendReady {
                launchSetupLine
            } else {
                statusLines(asOf: evidenceNow)
            }
        }
        .padding(.horizontal, 4)
        .onAppear {
            evidenceNow = Date()
            noticeNow = Date()
            observeStatus()
            reanchorElapsed()
        }
        .onChange(of: detail.activityStartedAt) { _, _ in reanchorElapsed() }
        .onChange(of: detail.id) { _, _ in
            evidenceDisclosure = false
            transition = SessionLedgerNoticeState()
            observeStatus()
        }
        .onChange(of: elapsedAnchorKey) { _, _ in reanchorElapsed() }
        .onChange(of: statusSignature) { _, _ in
            observeStatus()
        }
        // server's valid_until passes, labels and motion change immediately.
        .task(id: evidenceDeadlineKey) {
            guard let deadline = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse) else {
                return
            }
            let remaining = deadline.timeIntervalSinceNow
            if remaining > 0 {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            }
            if !Task.isCancelled {
                evidenceNow = Date()
            }
        }
        .task(id: noticeTaskKey) {
            guard let deadline = transition.until else { return }
            let remaining = deadline.timeIntervalSinceNow
            if remaining > 0 {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            }
            if !Task.isCancelled {
                noticeNow = Date()
            }
        }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: evidenceDisclosure)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: noticeIsVisible)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var style: RuntimeChromeStyle { RuntimeChromeStyle(detail: detail) }

    private var elapsedAnchorKey: String {
        [detail.id, detail.stateFacts.primary?.key ?? "", detail.stateFacts.activityTool ?? "", detail.stateFacts.activityState]
            .joined(separator: ":")
    }

    private var evidenceDeadlineKey: String {
        "\(detail.id):\(detail.stateFacts.activityValidUntil ?? "")"
    }
    private var statusSignature: String {
        [
            detail.id,
            detail.stateFacts.primary?.key ?? "",
            detail.stateFacts.activityState,
            detail.stateFacts.activityTool ?? "",
            detail.stateFacts.activitySource ?? "",
            detail.stateFacts.activityObservedAt ?? "",
            detail.stateFacts.activityValidUntil ?? "",
            detail.stateFacts.lastResultAt ?? "",
            String(describing: ledger(asOf: evidenceNow)),
            String(describing: realtimeConnection),
            detail.runtimeDisplay.hostState,
            detail.stateFacts.transcriptConvergence
        ].joined(separator: "|")
    }

    private var noticeTaskKey: String {
        "\(transition.notice.map { String(describing: $0) } ?? "none"):\(transition.until?.timeIntervalSince1970 ?? 0)"
    }

    private var noticeIsVisible: Bool {
        guard transition.notice != nil, let until = transition.until else { return false }
        return noticeNow < until
    }

    private func observeStatus() {
        let now = Date()
        transition.observe(
            state: ledger(asOf: evidenceNow),
            evidence: detail.stateFacts.providerEvidenceIdentity,
            connection: realtimeConnection,
            resultAt: detail.stateFacts.lastResultAt,
            now: now
        )
        noticeNow = now
    }

    private func reanchorElapsed() {
        guard let observed = detail.activityStartedAt else {
            if elapsedAnchor?.key != elapsedAnchorKey { elapsedAnchor = nil }
            return
        }
        if let current = elapsedAnchor, current.key == elapsedAnchorKey {
            if observed < current.start {
                elapsedAnchor = ElapsedAnchor(key: current.key, start: observed)
            }
        } else {
            elapsedAnchor = ElapsedAnchor(key: elapsedAnchorKey, start: observed)
        }
    }

    private var elapsedStart: Date? {
        if let anchor = elapsedAnchor, anchor.key == elapsedAnchorKey {
            return anchor.start
        }
        return detail.activityStartedAt
    }

    private var isExecuting: Bool { detail.isSessionExecuting }
    private func ledger(asOf now: Date) -> SessionLedgerEvidence {
        detail.ledgerEvidence(connection: realtimeConnection, asOf: now)
    }

    private func evidenceIsLive(asOf now: Date) -> Bool {
        guard ledger(asOf: now) == .working else { return false }
        guard detail.stateFacts.activityEvidenceIsLive(asOf: now) else { return false }
        return true
    }

    private func statusLines(asOf now: Date) -> some View {
        let state = ledger(asOf: now)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Image(systemName: statusGlyph(for: state))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(headlineColor(for: state))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline(for: state))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(headlineColor(for: state))
                        .lineLimit(2)
                    if let connectionLabel = connectionLabel(for: state) {
                        Text(connectionLabel)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(1)
                ActivityStrip(
                    store: activity,
                    tone: tone(for: state),
                    evidenceLive: evidenceIsLive(asOf: now)
                )
                if state == .uncertain || realtimeConnection == .connected || shouldExpand || evidenceDisclosure {
                    Button {
                        evidenceDisclosure.toggle()
                    } label: {
                        Image(systemName: evidenceDisclosure ? "chevron.up" : "info.circle")
                            .font(.caption.weight(.semibold))
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel(evidenceDisclosure ? "Hide status evidence" : "Show status evidence")
                    .accessibilityIdentifier("session-runtime-evidence-toggle")
                }
            }
            if shouldExpand || evidenceDisclosure || noticeIsVisible {
                evidenceContext(state: state)
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
    }
    private var shouldExpand: Bool {
        ledger(asOf: evidenceNow) == .uncertain
            || detail.activePauseRequest != nil
            || detail.stateFacts.pendingInteractionKind != nil
            || ["offline", "stale"].contains(detail.runtimeDisplay.hostState)
            || detail.controlBlock.isFault
            || detail.isTranscriptSyncing
    }

    private func statusGlyph(for state: SessionLedgerEvidence) -> String {
        switch state {
        case .working: return "bolt.fill"
        case .attention: return "hand.raised.fill"
        case .uncertain: return "questionmark.circle"
        case .quiet: return "circle"
        }
    }

    private func connectionLabel(for state: SessionLedgerEvidence) -> String? {
        switch realtimeConnection {
        case .connected:
            return "Updates connected"
        case .connecting:
            return "Updates connecting"
        case .disconnected:
            return state == .uncertain ? "Updates disconnected" : nil
        }
    }

    private func headline(for state: SessionLedgerEvidence) -> String {
        switch state {
        case .uncertain: return "Activity uncertain"
        default: return detail.runtimeHeadline
        }
    }
    private func tone(for state: SessionLedgerEvidence) -> Color {
        state == .uncertain ? Color.secondary : style.dot.color
    }

    private func headlineColor(for state: SessionLedgerEvidence) -> Color {
        if state == .uncertain { return .secondary }
        switch style.dot {
        case .attention: return TranscriptPalette.attention
        case .live: return .primary
        case .idle, .dormant: return .secondary
        }
    }


    private func evidenceContext(state: SessionLedgerEvidence) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if noticeIsVisible, let notice = transition.notice {
                Text(notice == .finished ? "Turn finished" : "Activity evidence restored")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            if let pauseRequest = detail.activePauseRequest {
                Text(pauseRequest.canRespond ? "Answer in the session card below." : "Answer in the provider terminal.")
                    .foregroundStyle(.secondary)
            } else {
                HStack(spacing: 6) {
                    Image(systemName: state == .uncertain ? "questionmark.circle" : "antenna.radiowaves.left.and.right")
                        .font(.caption)
                    Text(evidenceLabel(state))
                        .font(.caption.weight(.medium))
                }
                .foregroundStyle(.secondary)
            }
            if detail.controlBlock.isFault, let message = detail.controlHealthMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
            }
            if detail.isTranscriptSyncing {
                Text("Transcript is catching up with the session.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
            }
            if evidenceDisclosure, let tail = detail.runtimeTailLine, !typeSize.isAccessibilitySize {
                Text(tail)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
                    .truncationMode(tail.hasPrefix("$ ") ? .tail : .head)
                    .accessibilityIdentifier("session-runtime-tail")
            }
            if evidenceDisclosure {
                elapsed(asOf: evidenceNow, state: state)
                capabilityChip
            }
        }
        .padding(.leading, 24)
    }

    private func evidenceLabel(_ state: SessionLedgerEvidence) -> String {
        switch realtimeConnection {
        case .connected:
            if state == .uncertain {
                return "The update connection is healthy, but current provider activity is unconfirmed."
            }
            return "Updates connected"
        case .connecting:
            return "Updates connecting"
        case .disconnected:
            if state == .uncertain {
                return "Updates disconnected; the agent may still be running"
            }
            return "Updates disconnected"
        }
    }
    private var launchSetupLine: some View {
        HStack(spacing: 8) {
            ActivityStrip(store: activity, tone: RuntimeSignal.live.color, evidenceLive: false)
            Text(detail.launchSetupStatusLabel)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(typeSize.isAccessibilitySize ? 2 : 1)
        }
    }

    // Executing: a precise count while server evidence is valid. Once it
    // expires, the count freezes rather than following a wall-clock timer.
    @ViewBuilder
    private func elapsed(asOf now: Date, state: SessionLedgerEvidence) -> some View {
        if let start = elapsedStart {
            let validUntil = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse)
            let live = evidenceIsLive(asOf: now)
            if live {
                SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { context in
                    let end = RuntimeElapsed.observedEnd(validUntil: validUntil, now: context.date)
                    elapsedText(RuntimeElapsed.label(from: start, to: end, precise: true), state: state)
                }
            } else {
                let end = isExecuting
                    ? RuntimeElapsed.observedEnd(validUntil: validUntil, now: now)
                    : now
                elapsedText(RuntimeElapsed.label(from: start, to: end, precise: isExecuting), state: state)
            }
        }
    }

    private func elapsedText(_ label: String, state: SessionLedgerEvidence) -> some View {
        Text(label)
            .font(.subheadline)
            .monospacedDigit()
            .foregroundStyle(isExecuting ? AnyShapeStyle(.secondary) : AnyShapeStyle(.tertiary))
            .lineLimit(1)
            .accessibilityIdentifier("session-runtime-elapsed")
    }

    @ViewBuilder
    private var capabilityChip: some View {
        if style.capability != .live, let label = detail.runtimeCapabilityLabel {
            Text(label)
                .font(.caption2.weight(.medium))
                .lineLimit(1)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .foregroundStyle(style.capability == .warning ? TranscriptPalette.attention : Color.secondary)
                .background(
                    Capsule(style: .continuous).fill(
                        style.capability == .warning
                            ? TranscriptPalette.attention.opacity(0.14)
                            : Color(.quaternarySystemFill)
                    )
                )
                .accessibilityIdentifier("session-runtime-capability")
        }
    }

    private var accessibilityLabel: String {
        let state = ledger(asOf: evidenceNow)
        if detail.canDraftBeforeSendReady { return detail.launchSetupStatusLabel }
        var parts = [headline(for: state)]
        if let start = elapsedStart {
            let end = isExecuting
                ? RuntimeElapsed.observedEnd(validUntil: detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse), now: evidenceNow)
                : evidenceNow
            parts.append(RuntimeElapsed.label(from: start, to: end, precise: isExecuting))
        }
        if let detailLabel = detail.runtimeDetail { parts.append(detailLabel) }
        if style.capability != .live, let label = detail.runtimeCapabilityLabel { parts.append(label) }
        if let connectionLabel = connectionLabel(for: state) { parts.append(connectionLabel) }
        if state == .uncertain { parts.append(evidenceLabel(state)) }
        if noticeIsVisible, let notice = transition.notice {
            parts.append(notice == .finished ? "Turn finished" : "Activity evidence restored")
        }
        return parts.joined(separator: ", ")
    }
}
