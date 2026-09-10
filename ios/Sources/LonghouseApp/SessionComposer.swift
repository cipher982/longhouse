import SwiftUI

/// The capability-derived decisions used by both the live composer and its
/// preview. Keeping these decisions next to the controls prevents previews
/// from quietly growing a second send/queue policy.
enum SessionComposerControlState {
    static func isVisible(for detail: SessionDetail) -> Bool {
        detail.activePauseRequest != nil || detail.canSendLive || detail.canDraftBeforeSendReady
    }

    static func primaryIntent(for detail: SessionDetail) -> String {
        if detail.defaultInputIntent != "auto" { return detail.defaultInputIntent }
        guard detail.isSessionExecuting else { return "auto" }
        if detail.canSteerActiveTurn { return "steer" }
        if detail.canQueueNextInput { return "queue" }
        return "auto"
    }

    static func attachmentInputEnabled(for detail: SessionDetail) -> Bool {
        detail.attachImagesEnabled && primaryIntent(for: detail) == "auto"
    }

    static func showsSecondaryQueueAction(for detail: SessionDetail) -> Bool {
        detail.isSessionExecuting && detail.canSteerActiveTurn && detail.canQueueNextInput
    }

    static func sendIcon(for detail: SessionDetail) -> String {
        primaryIntent(for: detail) == "queue" ? "clock.arrow.circlepath" : "arrow.up"
    }

    static func sendAccessibilityLabel(for detail: SessionDetail) -> String {
        guard detail.canSendLive else { return detail.controlHealthMessage ?? "Send unavailable" }
        switch primaryIntent(for: detail) {
        case "steer": return "Send update mid-turn"
        case "queue": return "Queue for next turn"
        default: return "Send reply"
        }
    }
}

/// The attachment/secondary-action control at the leading edge of the
/// composer. The live view supplies the photo-picker action; previews supply
/// an inert closure, but use this same control and capability gate.
struct SessionComposerActionMenu: View {
    let detail: SessionDetail
    let attachmentSlotsLeft: Int
    let attachmentInputEnabled: Bool
    let isProcessing: Bool
    let isSending: Bool
    let onAttach: () -> Void

    var body: some View {
        let canAttachImages = attachmentInputEnabled
            && attachmentSlotsLeft > 0
            && !isProcessing
            && !isSending

        return Menu {
            if detail.attachImagesEnabled {
                Button(action: onAttach) {
                    Label("Attach images", systemImage: "paperclip")
                }
                .disabled(!canAttachImages)
                .accessibilityIdentifier("session-chat-attach")
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
        .disabled(isSending)
        .accessibilityLabel("Message actions")
        .accessibilityIdentifier("session-chat-compose-actions")
    }
}

/// Production composer controls with injectable actions for previews. The
/// pause card, draft/queue affordance, attachment tray, and send row all share
/// the same visibility and capability decisions in every surface.
struct SessionComposer<ActionMenu: View, AttachmentTray: View>: View {
    let detail: SessionDetail
    @Binding var text: String
    @FocusState.Binding var focused: Bool
    @Environment(\.dynamicTypeSize) private var typeSize
    let failedInputCount: Int
    let queuedInputCount: Int
    let lastSendOutcome: SessionInputOutcome?
    let isSending: Bool
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
    let actionMenu: ActionMenu
    let attachmentTray: AttachmentTray

    init(
        detail: SessionDetail,
        text: Binding<String>,
        focused: FocusState<Bool>.Binding,
        failedInputCount: Int = 0,
        queuedInputCount: Int = 0,
        lastSendOutcome: SessionInputOutcome? = nil,
        isSending: Bool = false,
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
        @ViewBuilder actionMenu: () -> ActionMenu,
        @ViewBuilder attachmentTray: () -> AttachmentTray
    ) {
        self.detail = detail
        _text = text
        _focused = focused
        self.failedInputCount = failedInputCount
        self.queuedInputCount = queuedInputCount
        self.lastSendOutcome = lastSendOutcome
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
        self.actionMenu = actionMenu()
        self.attachmentTray = attachmentTray()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if failedInputCount > 0 {
                Text(failedInputCount == 1
                     ? "1 queued message failed to send."
                     : "\(failedInputCount) queued messages failed to send.")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("session-chat-queued-failed")
            }

            if queuedInputCount > 0 {
                Text(queuedInputCount == 1
                     ? "1 message queued — will send at next turn boundary."
                     : "\(queuedInputCount) messages queued — will send at next turn boundary.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("session-chat-queued-indicator")
            } else if lastSendOutcome == .sent {
                Text("Sent.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let turnEndedDraft {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Active turn ended")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(turnEndedDraft)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    HStack(spacing: 8) {
                        Button("Queue instead") {
                            Task { await onQueueInstead() }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        Button("Dismiss", action: onDismissTurnEnded)
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
                .padding(8)
                .background(Color.orange.opacity(0.08))
                .cornerRadius(8)
                .accessibilityIdentifier("session-chat-turn-ended")
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

            if detail.activePauseRequest == nil || focused || !text.isEmpty || !attachmentIsEmpty {
                let hasContent = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachmentIsEmpty
                let sendIsEnabled = detail.canSendLive
                    && detail.activePauseRequest == nil
                    && hasContent
                    && !isSending
                    && !attachmentIsProcessing
                    && !isLoadingPickerItems
                    && !(attachmentIsEmpty == false && SessionComposerControlState.primaryIntent(for: detail) != "auto")
                if typeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 6) {
                        draftEditor
                        HStack {
                            actionMenu
                            Spacer()
                            sendButton(enabled: sendIsEnabled)
                        }
                    }
                } else {
                    HStack(alignment: .bottom, spacing: 8) {
                        actionMenu
                        draftEditor
                        sendButton(enabled: sendIsEnabled)
                    }
                }
            }
        }
    }

    private var draftEditor: some View {
        TextField(detail.composerPlaceholder, text: $text, axis: .vertical)
            .lineLimit(1...(typeSize.isAccessibilitySize ? 3 : 6))
            .focused($focused)
            .autocorrectionDisabled(true)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity)
            .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .accessibilityIdentifier("session-chat-composer")
    }

    private func sendButton(enabled: Bool) -> some View {
        Button {
            Task { await onSend(nil) }
        } label: {
            if isSending {
                ProgressView()
                    .frame(width: 30, height: 30)
            } else {
                Image(systemName: SessionComposerControlState.sendIcon(for: detail))
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(enabled ? Color(.systemBackground) : Color(.systemGray))
                    .frame(width: 30, height: 30)
                    .background(
                        Circle().fill(enabled
                            ? AnyShapeStyle(Color.primary)
                            : AnyShapeStyle(Color(.tertiarySystemFill)))
                    )
            }
        }
        .frame(minWidth: 44, minHeight: 44)
        .contentShape(Rectangle())
        .disabled(!enabled)
        .accessibilityLabel(detail.activePauseRequest == nil
            ? SessionComposerControlState.sendAccessibilityLabel(for: detail)
            : "Answer the pending request before sending")
        .accessibilityIdentifier("session-chat-send")
        .contextMenu {
            if detail.activePauseRequest == nil && SessionComposerControlState.showsSecondaryQueueAction(for: detail) && attachmentIsEmpty {
                Button {
                    Task { await onSend("steer") }
                } label: {
                    Label("Send update now", systemImage: "arrow.up.circle")
                }
                Button {
                    Task { await onSend("queue") }
                } label: {
                    Label("Queue for next turn", systemImage: "clock.arrow.circlepath")
                }
            }
        }
    }
}
