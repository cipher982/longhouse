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
    @State private var startupGraceExpired = false
    @State private var hasObservedConnection = false

    private static let startupGrace: TimeInterval = 2

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
            startupGraceExpired = false
            hasObservedConnection = realtimeConnection == .connected
            observeStatus()
            reanchorElapsed()
        }
        .onChange(of: detail.activityStartedAt) { _, _ in reanchorElapsed() }
        .onChange(of: detail.id) { _, _ in
            evidenceDisclosure = false
            startupGraceExpired = false
            hasObservedConnection = realtimeConnection == .connected
            transition = SessionLedgerNoticeState()
            observeStatus()
        }
        .onChange(of: realtimeConnection) { _, connection in
            if connection == .connected {
                hasObservedConnection = true
            }
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
        // A stale observation's age is the one number on this row that keeps
        // changing while nothing else does. Tick it slowly; minute granularity
        // needs no more, and a quiet session should stay cheap.
        .task(id: observationClockKey) {
            guard detail.stateFacts.primary?.key == "no_recent_activity" else { return }
            guard !UITestHooks.holdsAmbientMotion else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                if Task.isCancelled { break }
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
        // A newly opened screen starts with an intentionally quiet transport
        // grace period. The timer only allows an exception to become visible;
        // it never turns uncertain provider evidence into a healthy claim.
        .task(id: startupGraceTaskKey) {
            startupGraceExpired = false
            try? await Task.sleep(nanoseconds: UInt64(Self.startupGrace * 1_000_000_000))
            if !Task.isCancelled {
                startupGraceExpired = true
            }
        }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: evidenceDisclosure)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: noticeIsVisible)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: connectionLabel(for: ledger(asOf: evidenceNow)))
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


    private var startupGraceTaskKey: String {
        "\(detail.id):transport-startup"
    }

    private var observationClockKey: String {
        [detail.id, detail.stateFacts.primary?.key ?? "", detail.stateFacts.primary?.observedAt ?? ""]
            .joined(separator: ":")
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
        guard isOpen else { return nil }
        if let anchor = elapsedAnchor, anchor.key == elapsedAnchorKey {
            return anchor.start
        }
        return detail.activityStartedAt
    }

    private var isExecuting: Bool { isOpen && detail.isSessionExecuting }
    private func ledger(asOf now: Date) -> SessionLedgerEvidence {
        guard isOpen else { return .quiet }
        return detail.ledgerEvidence(connection: realtimeConnection, asOf: now)
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
                Group {
                    if state == .working && !reduceMotion && !UITestHooks.holdsAmbientMotion {
                        // This means the provider still reports work, not that
                        // new output arrived. Receipts have their own trace.
                        ProgressView()
                            .controlSize(.small)
                            .tint(headlineColor(for: state))
                            .accessibilityIdentifier("session-runtime-working")
                    } else {
                        Image(systemName: statusGlyph(for: state))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(headlineColor(for: state))
                    }
                }
                .frame(width: 16, height: 16)
                .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline(for: state))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(headlineColor(for: state))
                        .lineLimit(2)
                    if state == .working && typeSize.isAccessibilitySize {
                        elapsed(asOf: now, state: state)
                    }
                    if let subline = subline(for: state, asOf: now) {
                        Text(subline)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                            .accessibilityIdentifier("session-runtime-subline")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(1)
                if state == .working && !typeSize.isAccessibilitySize {
                    elapsed(asOf: now, state: state)
                        .fixedSize(horizontal: true, vertical: false)
                }
                ActivityStrip(
                    store: activity,
                    tone: tone(for: state),
                    evidenceLive: evidenceIsLive(asOf: now)
                )
                if isOpen || detail.runtimeTailLine != nil || detail.runtimeCapabilityLabel != nil {
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
    private var isOpen: Bool {
        !detail.isClosed && detail.stateFacts.workingSet == "open"
    }

    private var shouldExpand: Bool {
        ledger(asOf: evidenceNow) == .uncertain
            || detail.activePauseRequest != nil
            || detail.stateFacts.pendingInteractionKind != nil
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

    private var transportFailureVisible: Bool {
        isOpen
            && (hasObservedConnection || startupGraceExpired)
            && realtimeConnection != .connected
    }

    /// A stale observation carries its own clock. The server names what was
    /// last seen ("Last observed idle"); the age belongs beside it, not behind
    /// the disclosure toggle, because "how long ago" is the whole question a
    /// quiet session raises.
    private func observationAge(asOf now: Date) -> String? {
        guard let primary = detail.stateFacts.primary, primary.key == "no_recent_activity" else { return nil }
        guard let observed = primary.observedAt.flatMap(LonghouseDateParser.parse) else { return nil }
        return RuntimeElapsed.ageLabel(from: observed, to: now)
    }

    /// The connection half is exception-first and often absent, which leaves the
    /// age standing alone. That is the right emphasis for a quiet session.
    private func subline(for state: SessionLedgerEvidence, asOf now: Date) -> String? {
        let parts = [observationAge(asOf: now), connectionLabel(for: state)].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    private func connectionLabel(for state: SessionLedgerEvidence) -> String? {
        guard isOpen, startupGraceExpired || hasObservedConnection else { return nil }
        switch realtimeConnection {
        case .connected:
            return nil
        case .connecting:
            return "Updates connecting"
        case .disconnected:
            return "Updates disconnected"
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
            }
            if shouldShowConnectionEvidence(state: state) {
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
                if state != .working {
                    elapsed(asOf: evidenceNow, state: state)
                }
                capabilityChip
            }
        }
        .padding(.leading, 24)
    }

    private func shouldShowConnectionEvidence(state: SessionLedgerEvidence) -> Bool {
        evidenceDisclosure || state == .uncertain
    }

    private func evidenceLabel(_ state: SessionLedgerEvidence) -> String {
        switch realtimeConnection {
        case .connected:
            if state == .uncertain {
                return "The update connection is healthy, but current provider activity is unconfirmed."
            }
            return "Updates connected"
        case .connecting:
            return startupGraceExpired || hasObservedConnection ? "Updates connecting" : "Checking for updates…"
        case .disconnected:
            if !startupGraceExpired && !hasObservedConnection {
                return "Checking for updates…"
            }
            if state == .uncertain {
                return "Updates disconnected; the agent may still be running"
            }
            return "Updates disconnected"
        }
    }
    private var launchSetupLine: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                ActivityStrip(store: activity, tone: RuntimeSignal.live.color, evidenceLive: false)
                Text(detail.launchSetupStatusLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(typeSize.isAccessibilitySize ? 2 : 1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if isOpen {
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
            if evidenceDisclosure {
                evidenceContext(state: ledger(asOf: evidenceNow))
            }
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
        if let age = observationAge(asOf: evidenceNow) { parts.append(age) }
        if let start = elapsedStart {
            let end = isExecuting
                ? RuntimeElapsed.observedEnd(validUntil: detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse), now: evidenceNow)
                : evidenceNow
            parts.append(RuntimeElapsed.label(from: start, to: end, precise: isExecuting))
        }
        if let detailLabel = detail.runtimeDetail { parts.append(detailLabel) }
        if style.capability != .live, let label = detail.runtimeCapabilityLabel { parts.append(label) }
        if evidenceDisclosure || state == .uncertain || transportFailureVisible {
            parts.append(evidenceLabel(state))
        }
        if noticeIsVisible, let notice = transition.notice {
            parts.append(notice == .finished ? "Turn finished" : "Activity evidence restored")
        }
        return parts.joined(separator: ", ")
    }
}
