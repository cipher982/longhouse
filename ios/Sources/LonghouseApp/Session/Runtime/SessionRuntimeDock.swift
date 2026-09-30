import Foundation
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
        } else if let interruptedEvidence, let evidence, evidence != interruptedEvidence {
            // Recovery needs an interruption and a *new* provider observation.
            // The viewer's socket deliberately does not participate: it is not
            // provider evidence, so reconnecting must not announce a recovery.
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

/// The integrated Ledger status row of the control card: provider headline,
/// elapsed observation and scoped stream state. Receipt history belongs to the
/// enclosing Balanced signal field. Literal work context is visible at rest;
/// detailed evidence remains behind deliberate disclosure.
struct SessionRuntimeDock: View {
    let detail: SessionDetail
    @ObservedObject var activity: ActivityPulseStore
    var realtimeConnection: SessionRealtimeConnection = .disconnected
    /// The enclosing navigation stack owns routes; the dock only supplies the
    /// exact provider-linked child session id.
    var onOpenSubagent: ((String) -> Void)? = nil

    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    // A streaming provider refreshes the primary label's observed_at with
    // every provisional delta. Anchor the counter on the earliest observation
    // for the current label + tool so it counts up instead of resetting.
    @State private var elapsedAnchor: ElapsedAnchor?
    @State private var evidenceNow = Date()
    @State private var evidenceDisclosure = false
    @State private var delegationSheetPresented = false
    @State private var pendingChildSessionId: String?
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
                    .id(reduceMotion)
                    .transition(.identity)
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
            evidenceNow = Date()
            if connection == .connected {
                hasObservedConnection = true
            }
            observeStatus()
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            evidenceNow = Date()
            noticeNow = Date()
            observeStatus()
        }
        .onChange(of: elapsedAnchorKey) { _, _ in reanchorElapsed() }
        .onChange(of: statusSignature) { _, _ in
            observeStatus()
        }
        // server's valid_until passes, labels and motion change immediately.
        .task(id: evidenceDeadlineKey) {
            evidenceNow = Date()
            guard let deadline = detail.stateFacts.workClaimValidUntil.flatMap(LonghouseDateParser.parse) else {
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
        .task(id: delegationDeadlineKey) {
            evidenceNow = Date()
            guard let deadline = detail.stateFacts.delegation?.validUntil.flatMap(LonghouseDateParser.parse) else {
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
        .sheet(
            isPresented: $delegationSheetPresented,
            onDismiss: {
                guard let childSessionId = pendingChildSessionId else { return }
                pendingChildSessionId = nil
                onOpenSubagent?(childSessionId)
            }
        ) {
            SessionDelegationTaskSheet(
                facts: detail.stateFacts.delegation,
                asOf: evidenceNow,
                onOpenSubagent: { childSessionId in
                    pendingChildSessionId = childSessionId
                    delegationSheetPresented = false
                }
            )
        }
        .animation(
            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.38),
            value: statusGeometrySignature
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityLabel)
    }

    private var style: RuntimeChromeStyle { RuntimeChromeStyle(detail: detail) }

    private var elapsedAnchorKey: String {
        [detail.id, detail.stateFacts.primary?.key ?? "", detail.stateFacts.activityTool ?? "", detail.stateFacts.activityState]
            .joined(separator: ":")
    }
    private var delegationDeadlineKey: String {
        "\(detail.id):\(detail.stateFacts.delegation?.validUntil ?? "")"
    }

    private enum DelegationPresentation {
        case absent
        case unknown
        case known(SessionDelegationFacts)

        var isUnknown: Bool {
            if case .unknown = self { return true }
            return false
        }
    }

    private var delegationPresentation: DelegationPresentation {
        guard let facts = detail.stateFacts.delegation else { return .absent }
        let state = facts.state.lowercased()
        if facts.items?.isEmpty == true {
            return .absent
        }
        if state == "unknown" {
            let hasObservation = facts.observedAt?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
            guard hasObservation else { return .absent }
        }
        guard facts.isValid(asOf: evidenceNow), state != "unknown" else {
            return .unknown
        }
        return .known(facts)
    }

    private var delegationSummaryLabel: String? {
        switch delegationPresentation {
        case .absent:
            return nil
        case .unknown:
            return "Background work status unknown"
        case .known(let facts):
            return delegationSummary(for: facts)
        }
    }


    private var evidenceDeadlineKey: String {
        "\(detail.id):\(detail.stateFacts.primary?.key ?? ""):\(detail.stateFacts.workClaimValidUntil ?? "")"
    }
    private var statusSignature: String {
        var components = [detail.id]
        components.append(detail.stateFacts.primary?.key ?? "")
        components.append(detail.stateFacts.activityState)
        components.append(detail.stateFacts.activityTool ?? "")
        components.append(detail.stateFacts.activitySource ?? "")
        components.append(detail.stateFacts.activityObservedAt ?? "")
        components.append(detail.stateFacts.activityValidUntil ?? "")
        components.append(detail.stateFacts.lastResultAt ?? "")
        components.append(detail.stateFacts.delegation?.state ?? "")
        components.append(detail.stateFacts.delegation?.observedAt ?? "")
        components.append(detail.stateFacts.delegation?.validUntil ?? "")
        components.append(String(describing: ledger(asOf: evidenceNow)))
        components.append(String(describing: realtimeConnection))
        components.append(detail.runtimeDisplay.hostState)
        components.append(detail.stateFacts.transcriptConvergence)
        return components.joined(separator: "|")
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
            resultAt: detail.stateFacts.lastResultAt,
            now: now
        )
        noticeNow = now
    }

    private var statusGeometrySignature: String {
        let state = ledger(asOf: evidenceNow)
        // Explicitly typed for the same reason as `statusSignature`: a literal
        // of optional-returning calls is inference work the CI toolchain will
        // not spend.
        let parts: [String] = [
            "\(shouldExpand)", "\(evidenceDisclosure)", "\(noticeIsVisible)",
            headline(for: state), operationLine(for: state) ?? "",
            subline(for: state, asOf: evidenceNow) ?? "",
            delegationSummaryLabel ?? "", exceptionReason(state) ?? ""
        ]
        return parts.joined(separator: "|")
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
        guard isOpen && detail.isSessionExecuting else { return nil }
        if let anchor = elapsedAnchor, anchor.key == elapsedAnchorKey {
            return anchor.start
        }
        return detail.activityStartedAt
    }

    private var isExecuting: Bool { isOpen && detail.isSessionExecuting }
    private func ledger(asOf now: Date) -> SessionLedgerEvidence {
        guard isOpen else { return .quiet }
        // No transport term here, and no grace: the viewer's socket is not
        // provider evidence, so `connecting` and `disconnected` never rewrite
        // the activity claim. They appear on the connection line below, which
        // keeps its own startup grace.
        return detail.ledgerEvidence(asOf: now)
    }


    private func delegationSummary(for facts: SessionDelegationFacts) -> String? {
        if let items = facts.items {
            guard !items.isEmpty else { return nil }
            return categorySummary(
                counts: items.reduce(into: [SessionDelegationCategory: Int]()) { counts, task in
                    counts[SessionDelegationCategory(kind: task.kind), default: 0] += 1
                },
                total: items.count
            )
        }
        if facts.state.lowercased() == "none" {
            return nil
        }
        guard let count = facts.count, count > 0 else {
            return "Background work reported"
        }
        let counts = (facts.kinds ?? [:]).reduce(into: [SessionDelegationCategory: Int]()) { result, pair in
            guard pair.value > 0 else { return }
            result[SessionDelegationCategory(kind: pair.key), default: 0] += pair.value
        }
        return categorySummary(counts: counts, total: count)
    }

    private func categorySummary(
        counts: [SessionDelegationCategory: Int],
        total: Int
    ) -> String {
        let order: [SessionDelegationCategory] = [.agents, .commands, .monitors, .other]
        let parts = order.compactMap { category -> String? in
            guard let count = counts[category], count > 0 else { return nil }
            return category.countLabel(count)
        }
        if parts.isEmpty {
            return "Background · \(total) \(total == 1 ? "task" : "tasks")"
        }
        return "Background · " + parts.joined(separator: " · ")
    }

    private func usesPrimaryDelegationHeadline(for state: SessionLedgerEvidence) -> Bool {
        detail.stateFacts.primary?.key == "delegated_work" && state != .attention
    }

    @ViewBuilder
    private func primaryHeadline(for state: SessionLedgerEvidence) -> some View {
        if usesPrimaryDelegationHeadline(for: state), let summary = delegationSummaryLabel {
            Button {
                delegationSheetPresented = true
            } label: {
                Text(summary)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(headlineColor(for: state))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .transaction { $0.animation = nil }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(summary)
            .accessibilityHint("Show named background work")
            .accessibilityIdentifier("session-runtime-background-summary")
        } else {
            Text(headline(for: state))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(headlineColor(for: state))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                .transaction { $0.animation = nil }
        }
    }

    private func statusLines(asOf now: Date) -> some View {
        let state = ledger(asOf: now)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    primaryHeadline(for: state)
                    if let operationLine = operationLine(for: state) {
                        Text(operationLine)
                            .font(.caption.monospaced())
                            .foregroundStyle(state == .uncertain ? Ember.textSecondary : Ember.text.opacity(0.82))
                            .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
                            .truncationMode(.tail)
                            .fixedSize(horizontal: false, vertical: true)
                            .transaction { $0.animation = nil }
                            .accessibilityIdentifier("session-runtime-operation")
                    }
                    if (state == .working || (state == .quiet && detail.stateFacts.lastResultAt != nil)) && typeSize.isAccessibilitySize {
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
                if (state == .working || (state == .quiet && detail.stateFacts.lastResultAt != nil)) && !typeSize.isAccessibilitySize {
                    elapsed(asOf: now, state: state)
                        .fixedSize(horizontal: true, vertical: false)
                }
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
            if !usesPrimaryDelegationHeadline(for: state), let summary = delegationSummaryLabel {
                Button {
                    delegationSheetPresented = true
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: delegationPresentation.isUnknown ? "questionmark.circle" : "arrow.triangle.branch")
                            .font(.caption2.weight(.semibold))
                        Text(summary)
                            .font(.caption.weight(.medium))
                            .lineLimit(typeSize.isAccessibilitySize ? 3 : 1)
                            .multilineTextAlignment(.leading)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.semibold))
                    }
                    .foregroundStyle(delegationPresentation.isUnknown ? Ember.textSecondary : Ember.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(summary)
                .accessibilityHint("Show named background work")
                .accessibilityIdentifier("session-runtime-background-summary")
            }
            if shouldExpand || evidenceDisclosure || noticeIsVisible {
                evidenceContext(state: state)
                    .transition(.identity)
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

    private func operationLine(for state: SessionLedgerEvidence) -> String? {
        if let tail = detail.runtimeTailLine,
           !tail.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return state == .uncertain ? "Last observed: \(tail)" : tail
        }
        // The server's normalized display label for the tool, not the raw
        // activity fact: one vocabulary with web's runtime strip.
        guard let tool = detail.runtimeDisplay.compactToolLabel,
              !tool.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return state == .uncertain ? "Last observed tool: \(tool)" : "Tool: \(tool)"
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
        case .uncertain:
            // The socket never produces this state any more, so the headline
            // names the fact that does: the window for the last work claim has
            // passed and nothing has replaced it.
            return "Activity uncertain"
        case .attention:
            if detail.activePauseRequest != nil { return "Permission needed" }
            if detail.stateFacts.primary?.key == "delegated_work",
               let summary = delegationSummaryLabel {
                return summary
            }
            return detail.runtimeHeadline
        default:
            if detail.stateFacts.primary?.key == "delegated_work",
               let summary = delegationSummaryLabel {
                return summary
            }
            return detail.runtimeHeadline
        }
    }

    private func headlineColor(for state: SessionLedgerEvidence) -> Color {
        // Uncertainty reads as quiet text, not as the attention ember. The dot
        // (`style.dot`) still carries the served tone; this only stops a
        // missing observation from looking like a fault.
        if state == .uncertain { return Ember.textSecondary }
        switch style.dot {
        case .attention: return TranscriptPalette.attention
        case .live: return Ember.text
        case .idle, .dormant: return Ember.textSecondary
        }
    }


    private func evidenceContext(state: SessionLedgerEvidence) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if noticeIsVisible, let notice = transition.notice {
                Text(notice == .finished ? "Turn finished" : "Activity evidence restored")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .transaction { $0.animation = nil }
            }
            if let reason = exceptionReason(state) {
                Text(reason)
                    .font(.caption)
                    // The sentence wears the state's tone: ember only when the
                    // user owns the state, secondary for a missing observation.
                    .foregroundStyle(
                        state == .attention ? TranscriptPalette.attention : Ember.textSecondary
                    )
                    .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
                    .transaction { $0.animation = nil }
            }
            if let pauseRequest = detail.activePauseRequest {
                Text(pauseRequest.canRespond ? "Answer in the session card below." : "Answer in the provider terminal.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if evidenceDisclosure {
                HStack(spacing: 6) {
                    Image(systemName: state == .uncertain ? "questionmark.circle" : "antenna.radiowaves.left.and.right")
                        .font(.caption)
                    Text(evidenceLabel(state))
                        .font(.caption.weight(.medium))
                }
                .foregroundStyle(.secondary)
                .transaction { $0.animation = nil }
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
                capabilityChip
            }
        }
    }


    private func exceptionReason(_ state: SessionLedgerEvidence) -> String? {
        switch state {
        case .uncertain:
            // The state now has exactly two causes -- a window the reader's
            // clock retired, and a host we observed go quiet -- and each one
            // names itself. The transport is not one of them; it is on the
            // connection line.
            let hostState = detail.runtimeDisplay.hostState
            if hostState == "offline" || hostState == "stale" {
                return "The host is \(hostState). The agent may still be working."
            }
            return "No fresh provider evidence. The agent may still be working."
        case .attention:
            return detail.activePauseRequest == nil
                ? nil
                : "A command is waiting for approval. Review it before work can continue."
        default:
            return nil
        }
    }
    private func evidenceLabel(_ state: SessionLedgerEvidence) -> String {
        switch realtimeConnection {
        case .connected:
            if state == .uncertain {
                return "Viewer is connected; the provider has not reported since the last observation."
            }
            return "Provider evidence is valid."
        case .connecting:
            return startupGraceExpired || hasObservedConnection ? "Updates connecting" : "Checking for updates…"
        case .disconnected:
            if !startupGraceExpired && !hasObservedConnection {
                return "Checking for updates…"
            }
            if state == .uncertain {
                return "Updates are unavailable; the agent may still be working."
            }
            return "Updates disconnected"
        }
    }
    private var launchSetupLine: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
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
        if state == .quiet, detail.stateFacts.lastResultAt != nil, let lastTurn = detail.lastTurn {
            elapsedText(
                RuntimeElapsed.label(seconds: Double(lastTurn.durationMs) / 1000, precise: true),
                state: state
            )
        } else if state == .working, let start = elapsedStart {
            let validUntil = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse)
            if reduceMotion || UITestHooks.holdsAmbientMotion {
                let end = RuntimeElapsed.observedEnd(validUntil: validUntil, now: now)
                elapsedText(RuntimeElapsed.label(from: start, to: end, precise: true), state: state)
            } else {
                SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { context in
                    let end = RuntimeElapsed.observedEnd(validUntil: validUntil, now: context.date)
                    elapsedText(RuntimeElapsed.label(from: start, to: end, precise: true), state: state)
                }
            }
        }
    }

    private func elapsedText(_ label: String, state: SessionLedgerEvidence) -> some View {
        NixieReadout(text: label, live: isExecuting)
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
                .foregroundStyle(style.capability == .warning ? TranscriptPalette.attention : Ember.textSecondary)
                .background(
                    Capsule(style: .continuous).strokeBorder(
                        style.capability == .warning
                            ? TranscriptPalette.attention.opacity(0.4)
                            : Ember.border,
                        lineWidth: 0.75
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
        if state == .working, let start = elapsedStart {
            let end = RuntimeElapsed.observedEnd(
                validUntil: detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse),
                now: evidenceNow
            )
            parts.append(RuntimeElapsed.label(from: start, to: end, precise: true))
        } else if state == .quiet, detail.stateFacts.lastResultAt != nil, let lastTurn = detail.lastTurn {
            parts.append(RuntimeElapsed.label(seconds: Double(lastTurn.durationMs) / 1000, precise: true))
        }
        if let detailLabel = operationLine(for: state) { parts.append(detailLabel) }
        if !usesPrimaryDelegationHeadline(for: state), let backgroundSummary = delegationSummaryLabel { parts.append(backgroundSummary) }
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
