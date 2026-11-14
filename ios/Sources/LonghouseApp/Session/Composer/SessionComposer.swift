import SwiftUI

/// The capability-derived decisions used by both the live composer and its
/// preview. Keeping these decisions next to the controls prevents previews
/// from quietly growing a second send/queue policy.
enum SessionComposerControlState {
    /// How long a first Stop tap stays armed before it quietly disarms.
    static let stopConfirmWindow: TimeInterval = 3

    static func isVisible(for detail: SessionDetail) -> Bool {
        detail.activePauseRequest != nil || detail.canSendLive || detail.canDraftBeforeSendReady
    }

    static func primaryIntent(for detail: SessionDetail, asOf now: Date = Date()) -> String {
        guard detail.isSessionExecuting, detail.stateFacts.activityEvidenceIsLive(asOf: now) else { return "auto" }
        if detail.defaultInputIntent != "auto" { return detail.defaultInputIntent }
        if detail.canSteerActiveTurn { return "steer" }
        if detail.canQueueNextInput { return "queue" }
        return "auto"
    }

    static func showsSecondaryQueueAction(for detail: SessionDetail, asOf now: Date = Date()) -> Bool {
        detail.isSessionExecuting && detail.stateFacts.activityEvidenceIsLive(asOf: now)
            && detail.canSteerActiveTurn && detail.canQueueNextInput
    }

    static func sendIcon(for detail: SessionDetail, asOf now: Date = Date()) -> String {
        primaryIntent(for: detail, asOf: now) == "queue" ? "clock.arrow.circlepath" : "arrow.up"
    }

    static func sendAccessibilityLabel(for detail: SessionDetail, asOf now: Date = Date()) -> String {
        guard detail.canSendLive else { return detail.controlHealthMessage ?? "Send unavailable" }
        switch primaryIntent(for: detail, asOf: now) {
        case "steer": return "Send to this turn"
        case "queue": return "Queue"
        default: return "Send"
        }
    }

    static func stopActionLabel(for detail: SessionDetail) -> String? {
        guard detail.stateFacts.mode == "console",
              detail.stateFacts.interrupt.isAvailable else { return nil }
        if detail.stateFacts.runLifecycle == "ended",
           detail.stateFacts.delegation?.state == "pending",
           (detail.stateFacts.delegation?.count ?? 0) > 0 {
            return "Stop background work"
        }
        return "Stop"
    }

    /// The send slot becomes Stop while a stop is available and there is
    /// nothing to send. An armed Stop keeps the slot even with a draft, so a
    /// Stop armed from the + menu has a visible second tap.
    static func sendSlotShowsStop(stopAvailable: Bool, hasContent: Bool, armed: Bool) -> Bool {
        stopAvailable && (!hasContent || armed)
    }

    /// One status line above the input, most urgent first. A pending
    /// turn-ended decision is its own row and is not part of this choice.
    enum StatusNotice: Equatable {
        case stopError
        case failed(count: Int)
        case queued(total: Int, elsewhere: Int)
        case sent
    }

    static func statusNotice(
        stopError: String?,
        failedCount: Int,
        queuedCount: Int,
        queuedElsewhereCount: Int,
        sent: Bool
    ) -> StatusNotice? {
        if stopError != nil { return .stopError }
        if failedCount > 0 { return .failed(count: failedCount) }
        if queuedCount > 0 { return .queued(total: queuedCount, elsewhere: queuedElsewhereCount) }
        if sent { return .sent }
        return nil
    }

    /// The queue is shared by every sender into a session. The phone shows a
    /// bubble only for what it sent, so a message another sender parked is named
    /// rather than counted silently ("two queued" beside one bubble read as a bug).
    static func queuedIndicatorText(total: Int, elsewhere: Int) -> String {
        let base = total == 1 ? "1 message queued" : "\(total) messages queued"
        let origin: String
        if elsewhere <= 0 {
            origin = ""
        } else if elsewhere >= total {
            origin = " (from another sender)"
        } else {
            origin = " (\(elsewhere) from another sender)"
        }
        return "\(base)\(origin) — will send at next turn boundary."
    }

    static func placeholder(for detail: SessionDetail, asOf now: Date) -> String {
        detail.stateFacts.activityEvidenceIsLive(asOf: now) ? detail.composerPlaceholder : "Message"
    }
}

/// What the composer hands its action menu. The menu owns the attachment,
/// model and Stop entry points, but the composer owns the gates behind them.
struct SessionComposerMenuContext {
    let model: String?
    let onModelTap: (() -> Void)?
    let stopLabel: String?
    /// Arms the two-step Stop in the send slot; it never stops on its own.
    let armStop: () -> Void
}

/// The attachment/secondary-action control at the leading edge of the
/// composer. The live view supplies the photo-picker action; previews supply
/// an inert closure, but use this same control and capability gate.
struct SessionComposerActionMenu: View {
    let detail: SessionDetail
    let attachmentSlotsLeft: Int
    let isProcessing: Bool
    let isSending: Bool
    let onAttach: () -> Void
    var model: String? = nil
    var onModelTap: (() -> Void)? = nil
    var stopLabel: String? = nil
    var onArmStop: (() -> Void)? = nil

    var body: some View {
        // Picking an image only prepares it on this device; whether it can be
        // sent right now is the send button's concern, not the picker's.
        let canAttachImages = detail.attachImagesEnabled
            && attachmentSlotsLeft > 0
            && !isProcessing
            && !isSending

        // A menu with no items opens nothing, which reads as a dead button.
        // When images cannot be attached, keep the item and say why.
        let unavailableReason: String? = {
            if !detail.attachImagesEnabled {
                return detail.canSendLive ? "Not available for this session" : "Waiting for live control"
            }
            if attachmentSlotsLeft <= 0 { return "Attachment limit reached" }
            return nil
        }()

        return Menu {
            Button(action: onAttach) {
                Text("Attach images")
                if let unavailableReason { Text(unavailableReason) }
                Image(systemName: "paperclip")
            }
            .disabled(!canAttachImages)
            .accessibilityIdentifier(canAttachImages ? "session-chat-attach" : "session-chat-attach-unavailable")

            if let onModelTap {
                Button(action: onModelTap) {
                    Label("Model \(model ?? "Default")", systemImage: "cpu")
                }
                .accessibilityIdentifier("session-chat-model-picker")
            }

            if let stopLabel, let onArmStop {
                Button(role: .destructive, action: onArmStop) {
                    Label(stopLabel, systemImage: "stop.fill")
                }
                .accessibilityIdentifier("session-chat-stop-menu")
            }
        } label: {
            Group {
                if isProcessing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "plus")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 32, height: 32)
            .contentShape(Rectangle())
        }
        .tint(Ember.textSecondary)
        // Not disabled while sending: Stop lives here too, and attach already
        // has its own !isSending gate.
        .accessibilityLabel("Message actions")
        .accessibilityIdentifier("session-chat-compose-actions")
    }
}

/// Production composer controls with injectable actions for previews. The
/// pause card, draft/queue affordance, attachment tray, and send row all share
/// the same visibility and capability decisions in every surface.
struct SessionComposer<ActionMenu: View, AttachmentTray: View>: View {
    let detail: SessionDetail
    let model: String?
    let onModelTap: () -> Void
    @Binding var text: String
    @FocusState.Binding var focused: Bool
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var evidenceNow = Date()
    /// Set by the first Stop tap; nil whenever Stop is not armed.
    @State private var stopArmedAt: Date?
    /// The provider run the arm was made against. A confirm for a different
    /// run must not interrupt it, even inside the window.
    @State private var stopArmedRunId: String?
    @State private var stopRingProgress: Double = 1
    @State private var stopArmTick = 0
    @State private var stopConfirmTick = 0
    let failedInputCount: Int
    let queuedInputCount: Int
    let queuedElsewhereCount: Int
    let lastSendOutcome: SessionInputOutcome?
    let isSending: Bool
    let isStopping: Bool
    let stopErrorMessage: String?
    let attachmentIsEmpty: Bool
    let attachmentIsProcessing: Bool
    let isLoadingPickerItems: Bool
    let turnEndedDraft: String?
    let onQueueInstead: () async -> Void
    let onDismissTurnEnded: () -> Void
    let pauseIsResponding: Bool
    let pauseErrorMessage: String?
    let onPauseRespond: (
        _ decision: String,
        _ answers: [String: [String]]?,
        _ content: String?,
        _ message: String?
    ) async -> Bool
    let onSend: (_ intent: String?) async -> Void
    let onStop: () async -> Void
    let actionMenu: (_ context: SessionComposerMenuContext) -> ActionMenu
    let attachmentTray: AttachmentTray

    init(
        detail: SessionDetail,
        text: Binding<String>,
        focused: FocusState<Bool>.Binding,
        model: String? = nil,
        onModelTap: @escaping () -> Void = {},
        failedInputCount: Int = 0,
        queuedInputCount: Int = 0,
        queuedElsewhereCount: Int = 0,
        lastSendOutcome: SessionInputOutcome? = nil,
        isSending: Bool = false,
        isStopping: Bool = false,
        stopErrorMessage: String? = nil,
        attachmentIsEmpty: Bool = true,
        attachmentIsProcessing: Bool = false,
        isLoadingPickerItems: Bool = false,
        turnEndedDraft: String? = nil,
        onQueueInstead: @escaping () async -> Void,
        onDismissTurnEnded: @escaping () -> Void,
        pauseIsResponding: Bool = false,
        pauseErrorMessage: String? = nil,
        onPauseRespond: @escaping (
            _ decision: String,
            _ answers: [String: [String]]?,
            _ content: String?,
            _ message: String?
        ) async -> Bool,
        onSend: @escaping (_ intent: String?) async -> Void,
        onStop: @escaping () async -> Void = {},
        @ViewBuilder actionMenu: @escaping (_ context: SessionComposerMenuContext) -> ActionMenu,
        @ViewBuilder attachmentTray: () -> AttachmentTray
    ) {
        self.detail = detail
        self.model = model
        self.onModelTap = onModelTap
        _text = text
        _focused = focused
        self.failedInputCount = failedInputCount
        self.queuedInputCount = queuedInputCount
        self.queuedElsewhereCount = queuedElsewhereCount
        self.lastSendOutcome = lastSendOutcome
        self.isStopping = isStopping
        self.stopErrorMessage = stopErrorMessage
        self.isSending = isSending
        self.attachmentIsEmpty = attachmentIsEmpty
        self.attachmentIsProcessing = attachmentIsProcessing
        self.isLoadingPickerItems = isLoadingPickerItems
        self.turnEndedDraft = turnEndedDraft
        self.onQueueInstead = onQueueInstead
        self.onDismissTurnEnded = onDismissTurnEnded
        self.pauseIsResponding = pauseIsResponding
        self.pauseErrorMessage = pauseErrorMessage
        self.onPauseRespond = onPauseRespond
        self.onSend = onSend
        self.onStop = onStop
        self.actionMenu = actionMenu
        self.attachmentTray = attachmentTray()
    }

    var body: some View {
        let stopLabel = SessionComposerControlState.stopActionLabel(for: detail)
        let hasContent = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachmentIsEmpty
        let inputRowVisible = detail.activePauseRequest == nil || focused || !text.isEmpty || !attachmentIsEmpty
        let notice = SessionComposerControlState.statusNotice(
            stopError: stopErrorMessage,
            failedCount: failedInputCount,
            queuedCount: queuedInputCount,
            queuedElsewhereCount: queuedElsewhereCount,
            sent: lastSendOutcome == .sent
        )
        VStack(alignment: .leading, spacing: 6) {
            // One status line, most urgent first. While a pause hides the input
            // row, Stop rides here so it stays reachable.
            if notice != nil || (!inputRowVisible && stopLabel != nil) {
                HStack(spacing: 8) {
                    noticeText(notice)
                    Spacer(minLength: 0)
                    if !inputRowVisible, let stopLabel {
                        stopControl(label: stopLabel)
                    }
                }
            }

            if let turnEndedDraft {
                turnEndedRow(turnEndedDraft)
            }

            if let pauseRequest = detail.activePauseRequest {
                SessionPauseRequestCard(
                    pauseRequest: pauseRequest,
                    isResponding: pauseIsResponding,
                    errorMessage: pauseErrorMessage,
                    onRespond: onPauseRespond
                )
            } else if detail.shouldShowAttentionFallback {
                SessionAttentionFallbackCard(detail: detail)
            }

            if detail.attachImagesEnabled && (detail.activePauseRequest == nil || !attachmentIsEmpty) {
                attachmentTray
            }

            if inputRowVisible {
                let sendIsEnabled = detail.canSendLive
                    && detail.activePauseRequest == nil
                    && hasContent
                    && !isSending
                    && !attachmentIsProcessing
                    && !isLoadingPickerItems
                let menu = SessionComposerMenuContext(
                    model: model,
                    onModelTap: detail.stateFacts.mode == "console" ? onModelTap : nil,
                    stopLabel: stopLabel,
                    armStop: armStop
                )
                if typeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 6) {
                        draftEditor
                        HStack {
                            actionMenu(menu)
                            Spacer()
                            actionSlot(stopLabel: stopLabel, hasContent: hasContent, sendIsEnabled: sendIsEnabled)
                        }
                    }
                } else {
                    HStack(alignment: .bottom, spacing: 8) {
                        actionMenu(menu)
                        draftEditor
                        actionSlot(stopLabel: stopLabel, hasContent: hasContent, sendIsEnabled: sendIsEnabled)
                    }
                }
            }
        }
        .task(id: detail.stateFacts.activityValidUntil) {
            evidenceNow = Date()
            guard let deadline = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse) else { return }
            let remaining = deadline.timeIntervalSinceNow
            if remaining > 0 {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            }
            if !Task.isCancelled { evidenceNow = Date() }
        }
        .task(id: stopArmedAt) {
            // The ring drains over the whole window, then the arm lapses by itself.
            guard let armedAt = stopArmedAt else { return }
            let remaining = armedAt.addingTimeInterval(SessionComposerControlState.stopConfirmWindow)
                .timeIntervalSinceNow
            guard remaining > 0 else {
                disarmStop()
                return
            }
            stopRingProgress = 1
            try? await Task.sleep(nanoseconds: 50_000_000)
            guard !Task.isCancelled else { return }
            withAnimation(reduceMotion ? nil : .linear(duration: remaining)) { stopRingProgress = 0 }
            try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            if !Task.isCancelled { disarmStop() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { evidenceNow = Date() } else { disarmStop() }
        }
        // An armed Stop belongs to one session, one provider run and one draft.
        // Any of these ends it. Turn observation is deliberately not a trigger:
        // a streaming turn refreshes its observed_at with every delta.
        .onChange(of: text) { _, _ in disarmStop() }
        .onChange(of: attachmentIsEmpty) { _, _ in disarmStop() }
        .onChange(of: detail.id) { _, _ in disarmStop() }
        .onChange(of: detail.stateFacts.runId) { _, _ in disarmStop() }
        .onChange(of: stopLabel == nil) { _, unavailable in
            if unavailable { disarmStop() }
        }
        .onChange(of: isStopping) { _, stopping in
            if stopping { disarmStop() }
        }
    }

    @ViewBuilder
    private func noticeText(_ notice: SessionComposerControlState.StatusNotice?) -> some View {
        switch notice {
        case .stopError:
            Text(stopErrorMessage ?? "")
                .font(.caption)
                .foregroundStyle(Ember.ember)
                .accessibilityIdentifier("session-chat-stop-error")
        case .failed(let count):
            Text(count == 1
                 ? "1 queued message failed to send."
                 : "\(count) queued messages failed to send.")
                .font(.caption)
                .foregroundStyle(Ember.ember)
                .accessibilityIdentifier("session-chat-queued-failed")
        case .queued(let total, let elsewhere):
            Text(SessionComposerControlState.queuedIndicatorText(total: total, elsewhere: elsewhere))
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("session-chat-queued-indicator")
        case .sent:
            Text("Sent.")
                .font(.caption)
                .foregroundStyle(.secondary)
        case nil:
            EmptyView()
        }
    }

    private func turnEndedRow(_ draft: String) -> some View {
        HStack(spacing: 8) {
            Text("Active turn ended")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Ember.flame)
            Text(draft)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
            Button("Queue instead") {
                Task { await onQueueInstead() }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            Button("Dismiss", action: onDismissTurnEnded)
                .buttonStyle(.bordered)
                .controlSize(.small)
        }
        .padding(8)
        .background(Ember.flame.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .accessibilityIdentifier("session-chat-turn-ended")
    }

    private var draftEditor: some View {
        TextField(SessionComposerControlState.placeholder(for: detail, asOf: evidenceNow), text: $text, axis: .vertical)
            .lineLimit(1...(typeSize.isAccessibilitySize ? 3 : 6))
            .focused($focused)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity)
            .background(Ember.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(Ember.hairline, lineWidth: 0.75)
            }
            .accessibilityIdentifier("session-chat-composer")
            // Hardware keyboard only: Return sends (Shift-Return keeps the
            // newline), Tab queues behind a running turn.
            .onKeyPress(keys: [.return], phases: .down) { press in
                guard !press.modifiers.contains(.shift), detail.canSendLive, detail.activePauseRequest == nil else { return .ignored }
                Task { await onSend(nil) }
                return .handled
            }
            .onKeyPress(keys: [.tab], phases: .down) { _ in
                guard detail.activePauseRequest == nil,
                      SessionComposerControlState.showsSecondaryQueueAction(for: detail, asOf: evidenceNow) else { return .ignored }
                Task { await onSend("queue") }
                return .handled
            }
    }

    @ViewBuilder
    private func actionSlot(stopLabel: String?, hasContent: Bool, sendIsEnabled: Bool) -> some View {
        if let stopLabel, SessionComposerControlState.sendSlotShowsStop(
            stopAvailable: true,
            hasContent: hasContent,
            armed: stopArmedAt != nil
        ) {
            stopControl(label: stopLabel)
        } else {
            sendButton(enabled: sendIsEnabled)
        }
    }

    // MARK: Two-step Stop

    private func armStop() {
        guard !isStopping, SessionComposerControlState.stopActionLabel(for: detail) != nil else { return }
        stopArmTick += 1
        stopArmedRunId = detail.stateFacts.runId
        withAnimation(reduceMotion ? nil : .smooth(duration: 0.28)) { stopArmedAt = Date() }
    }

    private func confirmStop() {
        // Recheck the window, the run and the capability on the confirming tap:
        // a stale arm must not interrupt a turn the user is no longer looking at.
        guard let armedAt = stopArmedAt,
              !isStopping,
              stopArmedRunId == detail.stateFacts.runId,
              SessionComposerControlState.stopActionLabel(for: detail) != nil,
              Date().timeIntervalSince(armedAt) < SessionComposerControlState.stopConfirmWindow else {
            disarmStop()
            return
        }
        stopConfirmTick += 1
        withAnimation(reduceMotion ? nil : .smooth(duration: 0.2)) { stopArmedAt = nil }
        Task { await onStop() }
    }

    private func disarmStop() {
        stopArmedRunId = nil
        guard stopArmedAt != nil else { return }
        withAnimation(reduceMotion ? nil : .smooth(duration: 0.2)) { stopArmedAt = nil }
    }

    private func stopControl(label: String) -> some View {
        Button {
            if stopArmedAt == nil { armStop() } else { confirmStop() }
        } label: {
            stopControlLabel(label: label)
        }
        .buttonStyle(.plain)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .disabled(isStopping)
        .sensoryFeedback(.warning, trigger: stopArmTick)
        .sensoryFeedback(.impact(weight: .heavy), trigger: stopConfirmTick)
        .accessibilityLabel(stopArmedAt == nil ? label : "Confirm \(label.lowercased())")
        .accessibilityHint(stopArmedAt == nil ? "" : "Tap again to stop. Cancels in a few seconds.")
        .accessibilityIdentifier("session-chat-stop")
    }

    @ViewBuilder
    private func stopControlLabel(label: String) -> some View {
        if isStopping {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Stopping")
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(Ember.textSecondary)
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(Ember.well, in: Capsule())
            .overlay { Capsule().strokeBorder(Ember.hairline, lineWidth: 0.75) }
        } else if stopArmedAt != nil {
            HStack(spacing: 8) {
                ZStack {
                    Circle()
                        .stroke(Ember.flame.opacity(0.22), lineWidth: 2)
                    Circle()
                        .trim(from: 0, to: stopRingProgress)
                        .stroke(Ember.flame, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 18, height: 18)
                Text("Tap to stop")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Ember.flame)
            }
            .padding(.horizontal, 14)
            .frame(height: 40)
            .background(Ember.flame.opacity(0.14), in: Capsule())
            .overlay { Capsule().strokeBorder(Ember.flame.opacity(0.55), lineWidth: 1) }
            .transition(.scale(scale: 0.9).combined(with: .opacity))
        } else {
            Image(systemName: "stop.fill")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(Ember.flame)
                .frame(width: 34, height: 34)
                .background(Circle().fill(Ember.flame.opacity(0.14)))
                .overlay { Circle().strokeBorder(Ember.flame.opacity(0.5), lineWidth: 1) }
                .transition(.scale(scale: 0.9).combined(with: .opacity))
        }
    }

    // MARK: Send

    private func sendButton(enabled: Bool) -> some View {
        Button {
            Task { await onSend(nil) }
        } label: {
            if isSending {
                ProgressView()
                    .frame(width: 30, height: 30)
            } else {
                Image(systemName: SessionComposerControlState.sendIcon(for: detail, asOf: evidenceNow))
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(enabled ? Ember.gold : Ember.textMuted)
                    .frame(width: 34, height: 34)
                    .background(
                        Circle().fill(enabled ? Ember.goldFill.opacity(0.18) : Ember.well)
                    )
                    .overlay {
                        Circle().strokeBorder(enabled ? Ember.gold.opacity(0.6) : Ember.hairline, lineWidth: 1)
                    }
            }
        }
        .frame(minWidth: 44, minHeight: 44)
        .contentShape(Rectangle())
        .disabled(!enabled)
        .accessibilityLabel(detail.activePauseRequest == nil
            ? SessionComposerControlState.sendAccessibilityLabel(for: detail, asOf: evidenceNow)
            : "Answer the pending request before sending")
        .accessibilityIdentifier("session-chat-send")
        .contextMenu {
            // Tap already steers; long-press offers the other choice.
            if detail.activePauseRequest == nil
                && SessionComposerControlState.showsSecondaryQueueAction(for: detail, asOf: evidenceNow) {
                Button {
                    Task { await onSend("queue") }
                } label: {
                    Label("Queue after this turn", systemImage: "clock.arrow.circlepath")
                }
            }
        }
    }
}
