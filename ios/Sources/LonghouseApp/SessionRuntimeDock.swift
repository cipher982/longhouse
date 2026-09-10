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

/// The one continuous Balanced surface shared by status and composer. Meaning
/// comes from served facts immediately; only these background layers settle.
enum SessionSignalMaterialKind: Equatable {
    case working
    case exception
    case settled
}

struct SessionSignalField<Content: View>: View {
    let detail: SessionDetail
    @ObservedObject var activity: ActivityPulseStore
    let realtimeConnection: SessionRealtimeConnection
    let content: Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @State private var fieldNow = Date()
    @State private var receiptTask: Task<Void, Never>?
    @State private var receiptActive = false
    @State private var receiptOpacity = 0.0
    @State private var lastObservedPulseAt: Date?

    init(
        detail: SessionDetail,
        activity: ActivityPulseStore,
        realtimeConnection: SessionRealtimeConnection,
        @ViewBuilder content: () -> Content
    ) {
        self.detail = detail
        self.activity = activity
        self.realtimeConnection = realtimeConnection
        self.content = content()
    }

    private var materialKind: SessionSignalMaterialKind {
        switch detail.ledgerEvidence(connection: realtimeConnection, asOf: fieldNow) {
        case .working: return .working
        case .attention, .uncertain: return .exception
        case .quiet: return .settled
        }
    }

    private var holdMotion: Bool { reduceMotion || UITestHooks.holdsAmbientMotion }

    private var activityDeadlineKey: String {
        "\(detail.id):\(detail.stateFacts.activityValidUntil ?? "")"
    }

    var body: some View {
        let kind = materialKind
        content
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                ZStack(alignment: .top) {
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(workMaterial)
                        .opacity(kind == .working ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(exceptionMaterial)
                        .opacity(kind == .exception ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(settledMaterial)
                        .opacity(kind == .settled ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    workSheen(kind: kind)
                    ActivityReceiptTrail(
                        store: activity,
                        tone: signalTone,
                        evidenceLive: kind == .working
                    )
                    .opacity(kind == .working ? 1 : 0)
                    .animation(
                        reduceMotion ? nil : .linear(duration: 0.12),
                        value: kind
                    )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(
                            RadialGradient(
                                colors: [signalTone.opacity(0.16), .clear],
                                center: UnitPoint(x: 0.72, y: 0.54),
                                startRadius: 2,
                                endRadius: 240
                            )
                        )
                        .opacity(receiptOpacity)
                }
                .clipped()
            }
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .strokeBorder(borderColor(for: kind), lineWidth: 0.75)
                    .animation(
                        reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                        value: kind
                    )
            )
            .shadow(color: .black.opacity(colorScheme == .dark ? 0.28 : 0.12), radius: 16, y: 5)
            .task(id: activityDeadlineKey) {
                fieldNow = Date()
                guard let deadline = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse) else {
                    return
                }
                let remaining = deadline.timeIntervalSinceNow
                if remaining > 0 {
                    try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
                }
                if !Task.isCancelled { fieldNow = Date() }
            }
            .onAppear {
                fieldNow = Date()
                lastObservedPulseAt = activity.latestPulseAt
            }
            .onChange(of: activity.latestPulseAt) { _, latest in
                guard latest != lastObservedPulseAt else { return }
                lastObservedPulseAt = latest
                startReceiptAccent()
            }
            .onChange(of: realtimeConnection) { _, _ in fieldNow = Date() }
            .onChange(of: materialKind) { _, kind in
                fieldNow = Date()
                guard kind != .working, receiptActive else { return }
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                withAnimation(reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.12)) {
                    receiptOpacity = 0
                }
            }
            .onChange(of: detail.id) { _, _ in
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
                lastObservedPulseAt = activity.latestPulseAt
            }
            .onDisappear {
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
            }
            .onChange(of: reduceMotion) { _, reduced in
                guard reduced else { return }
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
            }
            .accessibilityElement(children: .contain)
    }

    private func workSheen(kind: SessionSignalMaterialKind) -> some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: kind != .working || holdMotion)) { context in
            let phase = context.date.timeIntervalSinceReferenceDate
                .truncatingRemainder(dividingBy: 2.8) / 2.8
            let intensity = holdMotion ? 0.08 : 0.09 + 0.07 * (0.5 + 0.5 * sin(phase * 2 * .pi))
            RoundedRectangle(cornerRadius: 24, style: .continuous)
                .fill(RadialGradient(
                    colors: [sheenColor.opacity(intensity), .clear],
                    center: UnitPoint(x: 0.65, y: 0),
                    startRadius: 0,
                    endRadius: 260
                ))
        }
        .opacity(kind == .working ? 1 : 0)
        .animation(
            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.12),
            value: kind
        )
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
    private func startReceiptAccent() {
        guard materialKind == .working, !receiptActive else { return }
        receiptActive = true
        receiptTask?.cancel()
        withAnimation(reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.156)) {
            receiptOpacity = 1
        }
        receiptTask = Task { @MainActor in
            if reduceMotion {
                try? await Task.sleep(nanoseconds: 1_300_000_000)
            } else {
                try? await Task.sleep(nanoseconds: 156_000_000)
                guard !Task.isCancelled else { return }
                withAnimation(.timingCurve(0.2, 0.8, 0.2, 1, duration: 1.144)) {
                    receiptOpacity = 0
                }
                try? await Task.sleep(nanoseconds: 1_144_000_000)
            }
            guard !Task.isCancelled else { return }
            receiptOpacity = 0
            receiptActive = false
            receiptTask = nil
        }
    }

    private var signalTone: Color {
        colorScheme == .dark
            ? Color(red: 0.72, green: 0.86, blue: 0.77)
            : Color(red: 0.20, green: 0.47, blue: 0.31)
    }

    private var sheenColor: Color {
        colorScheme == .dark ? Color(red: 0.72, green: 1.0, blue: 0.82) : Color.white
    }

    private var workMaterial: LinearGradient {
        if colorScheme == .dark {
            return LinearGradient(
                colors: [Color(red: 0.09, green: 0.21, blue: 0.16), Color(red: 0.06, green: 0.14, blue: 0.11)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
        return LinearGradient(
            colors: [Color(red: 0.91, green: 0.97, blue: 0.93), Color(red: 0.82, green: 0.93, blue: 0.86)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var exceptionMaterial: LinearGradient {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Color(red: 0.22, green: 0.18, blue: 0.12), Color(red: 0.13, green: 0.11, blue: 0.08)]
                : [Color(red: 0.99, green: 0.95, blue: 0.87), Color(red: 0.96, green: 0.90, blue: 0.79)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var settledMaterial: Color {
        colorScheme == .dark
            ? Color(red: 0.09, green: 0.13, blue: 0.10)
            : Color(.secondarySystemBackground)
    }

    private func borderColor(for kind: SessionSignalMaterialKind) -> Color {
        switch kind {
        case .working: return signalTone.opacity(0.28)
        case .exception: return TranscriptPalette.attention.opacity(0.48)
        case .settled: return Color.secondary.opacity(0.22)
        }
    }
}

/// The integrated Ledger status row of the control card: provider headline,
/// elapsed observation and scoped stream state. Receipt history belongs to the
/// enclosing Balanced signal field; literal tool/context details stay behind
/// deliberate disclosure.
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

    private var statusGeometrySignature: String {
        let state = ledger(asOf: evidenceNow)
        return [
            "\(shouldExpand)", "\(evidenceDisclosure)", "\(noticeIsVisible)",
            headline(for: state), operationLine(for: state) ?? "",
            subline(for: state, asOf: evidenceNow) ?? "", exceptionReason(state) ?? ""
        ].joined(separator: "|")
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


    private func statusLines(asOf now: Date) -> some View {
        let state = ledger(asOf: now)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline(for: state))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(headlineColor(for: state))
                        .lineLimit(2)
                        .transaction { $0.animation = nil }
                    if let operationLine = operationLine(for: state) {
                        Text(operationLine)
                            .font(.caption.monospaced())
                            .foregroundStyle(state == .uncertain ? Color.secondary : Color.primary.opacity(0.78))
                            .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
                            .truncationMode(.middle)
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
            if shouldExpand || evidenceDisclosure || noticeIsVisible {
                evidenceContext(state: state)
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
        guard let tool = detail.stateFacts.activityTool,
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
            return realtimeConnection == .disconnected ? "Updates interrupted" : "Activity uncertain"
        case .attention:
            return detail.activePauseRequest != nil ? "Permission needed" : detail.runtimeHeadline
        default:
            return detail.runtimeHeadline
        }
    }


    private func headlineColor(for state: SessionLedgerEvidence) -> Color {
        if state == .uncertain { return TranscriptPalette.attention }
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
                    .transaction { $0.animation = nil }
            }
            if let reason = exceptionReason(state) {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(TranscriptPalette.attention)
                    .lineLimit(typeSize.isAccessibilitySize ? 3 : 2)
                    .transaction { $0.animation = nil }
            }
            if let pauseRequest = detail.activePauseRequest {
                Text(pauseRequest.canRespond ? "Answer in the session card below." : "Answer in the provider terminal.")
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
            return realtimeConnection == .disconnected
                ? "Connection lost. The agent may still be working."
                : "No fresh provider evidence. The agent may still be working."
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
                return "Viewer is connected, but provider activity is unconfirmed."
            }
            return "Provider evidence is valid."
        case .connecting:
            return startupGraceExpired || hasObservedConnection ? "Updates connecting" : "Checking for updates…"
        case .disconnected:
            if !startupGraceExpired && !hasObservedConnection {
                return "Checking for updates…"
            }
            if state == .uncertain {
                return "Viewer updates are unavailable; the agent may still be working."
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
