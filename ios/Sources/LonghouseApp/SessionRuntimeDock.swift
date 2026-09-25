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
    let content: Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.scenePhase) private var scenePhase
    @State private var fieldNow = Date()
    @State private var receiptTask: Task<Void, Never>?
    @State private var receiptActive = false
    @State private var receiptOpacity = 0.0
    @State private var lastObservedPulseAt: Date?

    init(
        detail: SessionDetail,
        activity: ActivityPulseStore,
        @ViewBuilder content: () -> Content
    ) {
        self.detail = detail
        self.activity = activity
        self.content = content()
    }

    private var materialKind: SessionSignalMaterialKind {
        switch detail.ledgerEvidence(asOf: fieldNow) {
        case .working: return .working
        // Exception material is reserved for a state the user owns, and the only
        // one the ledger raises that way is an unresolved interaction: a
        // question or an approval. A stalled turn, an auth requirement and a
        // control fault keep the served tone on the headline and dot instead, and
        // "no current observation" is information rather than an alarm.
        case .attention: return .exception
        case .uncertain, .quiet: return .settled
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
                        // Flame is far more saturated than the old pale green;
                        // at half strength the bars stay texture under the text.
                        tone: signalTone.opacity(0.5),
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
                .id(reduceMotion)
                .clipped()
            }
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay { BrassCornerPoints(inset: 9) }
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
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { fieldNow = Date() }
            }
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

    /// Live work reads as flame, the web composer's running glow.
    private var signalTone: Color { Ember.flame }

    private var sheenColor: Color {
        colorScheme == .dark ? Ember.uiHex(0xFFB25A) : Ember.uiHex(0xFFE2B8)
    }

    private var workMaterial: LinearGradient {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Ember.uiHex(0x2A1B12), Ember.uiHex(0x1A120E)]
                : [Ember.uiHex(0xFFF6E8), Ember.uiHex(0xFBEBD4)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var exceptionMaterial: LinearGradient {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Ember.uiHex(0x2C1510), Ember.uiHex(0x1B0F0C)]
                : [Ember.uiHex(0xFCEDE3), Ember.uiHex(0xF6DECF)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var settledMaterial: Color { Ember.card }

    private func borderColor(for kind: SessionSignalMaterialKind) -> Color {
        switch kind {
        case .working: return signalTone.opacity(0.28)
        case .exception: return TranscriptPalette.attention.opacity(0.48)
        case .settled: return Ember.border.opacity(0.9)
        }
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
        "\(detail.id):\(detail.stateFacts.activityValidUntil ?? "")"
    }
    private var statusSignature: String {
        let state = ledger(asOf: evidenceNow)
        // Typed explicitly, and with the state rendered outside the literal: as
        // one inferred 15-element expression this exceeded the type-checker's
        // budget on the CI toolchain (Xcode 16.4), which fails the build with
        // "unable to type-check this expression in reasonable time".
        let parts: [String] = [
            detail.id,
            detail.stateFacts.primary?.key ?? "",
            detail.stateFacts.activityState,
            detail.stateFacts.activityTool ?? "",
            detail.stateFacts.activitySource ?? "",
            detail.stateFacts.activityObservedAt ?? "",
            detail.stateFacts.activityValidUntil ?? "",
            detail.stateFacts.lastResultAt ?? "",
            detail.stateFacts.delegation?.state ?? "",
            detail.stateFacts.delegation?.observedAt ?? "",
            detail.stateFacts.delegation?.validUntil ?? "",
            String(describing: state),
            String(describing: realtimeConnection),
            detail.runtimeDisplay.hostState,
            detail.stateFacts.transcriptConvergence
        ]
        return parts.joined(separator: "|")
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
        guard isOpen else { return nil }
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
        detail.stateFacts.primary?.key == "delegated_work" && state != .working
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
        if let backgroundSummary = delegationSummaryLabel { parts.append(backgroundSummary) }
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

struct SessionDelegationTaskSheet: View {
    let facts: SessionDelegationFacts?
    let onOpenSubagent: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var sheetNow: Date

    init(
        facts: SessionDelegationFacts?,
        asOf: Date,
        onOpenSubagent: @escaping (String) -> Void
    ) {
        self.facts = facts
        self.onOpenSubagent = onOpenSubagent
        _sheetNow = State(initialValue: asOf)
    }

    private struct TaskGroup: Identifiable {
        let key: String
        let title: String
        let tasks: [SessionDelegationTask]

        var id: String { key }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if let observationLine {
                        Text(observationLine)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                            .accessibilityIdentifier("session-runtime-background-observed")
                    }
                    content
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 18)
            }
            .background(Ember.page)
            .navigationTitle("Background work")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task(id: freshnessTaskKey) {
            while !Task.isCancelled {
                let remaining = facts?.validUntil
                    .flatMap(LonghouseDateParser.parse)?
                    .timeIntervalSinceNow
                    ?? 30
                if remaining <= 0 {
                    sheetNow = Date()
                    return
                }
                let delay = min(30, max(1, remaining))
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                if !Task.isCancelled {
                    sheetNow = Date()
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private var freshnessTaskKey: String {
        "\(facts?.observedAt ?? ""):\(facts?.validUntil ?? "")"
    }

    private var observationLine: String? {
        guard let observedAt = facts?.observedAt,
              let date = LonghouseDateParser.parse(observedAt),
              let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) else {
            return nil
        }
        return "Observed \(age)"
    }

    @ViewBuilder
    private var content: some View {
        if let facts, facts.isValid(asOf: sheetNow), facts.state.lowercased() != "unknown" {
            if let items = facts.items {
                if items.isEmpty {
                    emptyState
                } else {
                    taskGroups(items)
                }
            } else {
                aggregateOnlyState(facts)
            }
        } else {
            unknownState
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("No named background work", systemImage: "checkmark.circle")
                .font(.headline)
            Text("The provider reported an empty task list for this observation.")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityIdentifier("session-runtime-background-empty")
    }

    private func aggregateOnlyState(_ facts: SessionDelegationFacts) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Named task details unavailable", systemImage: "list.bullet.rectangle")
                .font(.headline)
            if let count = facts.count, count > 0 {
                Text("\(count) background \(count == 1 ? "task was" : "tasks were") reported, without provider task details.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("This observation contains aggregate background-work evidence only.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityIdentifier("session-runtime-background-aggregate-only")
    }

    private var unknownState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Background work status unknown", systemImage: "questionmark.circle")
                .font(.headline)
            Text("The provider's background-work evidence is missing or expired. No task is treated as completed.")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityIdentifier("session-runtime-background-unknown")
    }

    private func taskGroups(_ tasks: [SessionDelegationTask]) -> some View {
        ForEach(groups(for: tasks)) { group in
            VStack(alignment: .leading, spacing: 8) {
                Text(group.title)
                    .font(.headline)
                    .foregroundStyle(Ember.text)
                ForEach(group.tasks) { task in
                    taskRow(task)
                }
            }
        }
    }

    @ViewBuilder
    private func taskRow(_ task: SessionDelegationTask) -> some View {
        let title = taskTitle(task)
        let sessionId = task.sessionId?.trimmingCharacters(in: .whitespacesAndNewlines)
        let row = VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Ember.text)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if sessionId != nil {
                    Image(systemName: "arrow.up.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            Text("Status: \(task.status)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let timing = timingLine(for: task) {
                Text(timing)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Ember.card)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(Ember.border, lineWidth: 0.75)
        }
        if let sessionId {
            Button {
                onOpenSubagent(sessionId)
            } label: {
                row
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("session-runtime-background-task-\(task.id)")
            .accessibilityHint("Open the child transcript")
        } else {
            row
        }
    }

    private func groups(for tasks: [SessionDelegationTask]) -> [TaskGroup] {
        let order: [SessionDelegationCategory] = [.agents, .commands, .monitors, .other]
        let grouped = Dictionary(grouping: tasks) {
            SessionDelegationCategory(kind: $0.kind)
        }
        return order.compactMap { category in
            guard let tasks = grouped[category], !tasks.isEmpty else { return nil }
            return TaskGroup(key: category.rawValue, title: category.title, tasks: tasks)
        }
    }

    private func taskTitle(_ task: SessionDelegationTask) -> String {
        if let description = task.description {
            let trimmed = description.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
        return readableKind(task.kind)
    }

    private func readableKind(_ rawKind: String) -> String {
        let kind = rawKind.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !kind.isEmpty else { return "Background task" }
        return kind.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func timingLine(for task: SessionDelegationTask) -> String? {
        var parts: [String] = []
        if let startedAt = task.startedAt,
           let date = LonghouseDateParser.parse(startedAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Started \(age)")
        } else if let firstObservedAt = task.firstObservedAt,
                  let date = LonghouseDateParser.parse(firstObservedAt),
                  let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("First observed \(age)")
        }
        if let lastActivityAt = task.lastActivityAt,
           let date = LonghouseDateParser.parse(lastActivityAt),
           let age = RuntimeElapsed.ageLabel(from: date, to: sheetNow) {
            parts.append("Last activity \(age)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
