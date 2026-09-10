import SwiftUI

/// The mutually-exclusive states the session transcript surface can be in.
///
/// M3 consolidation: previously these were implied by stacked `if` conditionals
/// inside `SessionView.transcript` (isInitialLoading / errorMessage+empty /
/// refreshErrorMessage+content). Centralizing them as one enum makes the
/// taxonomy explicit, unit-testable, and impossible to render in contradictory
/// combinations (e.g. a full-screen error on top of a populated transcript).
///
/// Scope is intentionally the transcript surface only — not an app-wide error
/// component.
enum TranscriptDisplayState: Equatable {
    /// First load with nothing cached yet. Full-screen spinner.
    case loading
    /// Loaded successfully but the session genuinely has no events.
    case empty
    /// Loaded successfully but the session is empty and its refresh failed.
    /// Keep the native empty surface and expose a retry instead of hiding the
    /// failure behind "No messages yet".
    case emptyWithRefreshError(String)
    /// The archive is still converging and its refresh failed. Keep the native
    /// syncing surface while exposing the retry.
    case syncingWithRefreshError(String)
    /// The session is live/catalog-visible but its durable transcript is still
    /// converging. Keep the transcript mounted, but do not claim "No messages
    /// yet" while the archive catches up.
    case syncing
    /// Content is on screen but the latest refresh failed. Non-destructive
    /// banner over the transcript; never erases content.
    case contentWithRefreshError(String)
    /// Content is on screen and healthy.
    case content
    /// Cached/network content exists, but WebKit has not presented its first
    /// frame and the latest attempt failed. Keep stale DOM covered by an opaque
    /// retry surface until this session has a valid frame.
    case restoringWithError(String)
    /// Cached/network content exists, but WebKit has not presented its first
    /// frame. Keep the renderer mounted behind an honest native surface.
    case restoring

    /// Cold load failed with nothing cached. Full-screen, actionable error.
    case hardError(String)
    /// Derive the state from the raw view-model flags. Order matters: a
    /// blocking load takes precedence, then "do we have anything to show",
    /// then refresh health.
    static func derive(
        isInitialLoading: Bool,
        hasContent: Bool,
        errorMessage: String?,
        refreshErrorMessage: String?,
        isSyncing: Bool = false,
        rendererReady: Bool = true,
        rendererErrorMessage: String? = nil
    ) -> TranscriptDisplayState {
        if isInitialLoading {
            return .loading
        }
        if hasContent {
            if let rendererErrorMessage, !rendererReady {
                return .restoringWithError(rendererErrorMessage)
            }
            if let rendererErrorMessage {
                return .contentWithRefreshError(rendererErrorMessage)
            }
            if !rendererReady {
                return .restoring
            }
            if let refreshErrorMessage {
                return .contentWithRefreshError(refreshErrorMessage)
            }
            return .content
        }
        // A cold-load failure remains authoritative until a valid transcript
        // arrives. Realtime retries can populate refreshErrorMessage after the
        // first tail failed; that must not turn a hard failure into "No
        // messages yet".
        if let errorMessage {
            return .hardError(errorMessage)
        }
        if let refreshErrorMessage {
            return isSyncing
                ? .syncingWithRefreshError(refreshErrorMessage)
                : .emptyWithRefreshError(refreshErrorMessage)
        }
        if isSyncing {
            return .syncing
        }
        return .empty
    }

    /// True when real transcript content requires WebKit. Empty and syncing
    /// sessions stay native so a new Console never pays WebKit process startup
    /// before its composer can accept input.
    var showsTranscript: Bool {
        switch self {
        case .loading, .empty, .emptyWithRefreshError, .syncing, .syncingWithRefreshError, .hardError:
            return false
        case .content, .contentWithRefreshError, .restoring, .restoringWithError:
            return true
    }
    }
}

/// Single shared overlay for every transcript load state. Replaces the stacked
/// `if isInitialLoading / else if errorMessage / if refreshError` conditionals
/// that previously lived inline in `SessionView`. Renders nothing for the
/// healthy `.content` state (the WebKit transcript shows through).
struct TranscriptStateOverlay: View {
    let state: TranscriptDisplayState
    let onRetry: () -> Void

    var body: some View {
        switch state {
        case .loading:
            VStack(spacing: 12) {
                ProgressView()
                    .controlSize(.large)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(.systemBackground))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Loading transcript")
            .accessibilityIdentifier("session-transcript-loading")
        case .restoringWithError(let message):
            restoringWithError(message)
        case .restoring:
            restoring
        case .hardError(let message):
            hardError(message)
        case .syncing:
            syncing
        case .syncingWithRefreshError(let message):
            syncingWithRefreshError(message)
        case .contentWithRefreshError(let message):
            VStack {
                refreshBanner(message)
                Spacer(minLength: 0)
            }
        case .empty:
            Text("No messages yet")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("session-transcript-empty")
        case .emptyWithRefreshError(let message):
            emptyWithRefreshError(message)
        case .content:
            EmptyView()
        }
    }

    private func emptyWithRefreshError(_ message: String) -> some View {
        VStack(spacing: 12) {
            Text("No messages yet")
                .font(.callout)
                .foregroundStyle(.secondary)
            refreshBanner(message)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("session-transcript-empty-refresh-error")
    }

    private func syncingWithRefreshError(_ message: String) -> some View {
        VStack(spacing: 10) {
            ProgressView()
                .controlSize(.regular)
            Text("Syncing transcript…")
                .font(.callout)
                .foregroundStyle(.secondary)
            refreshBanner(message)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("session-transcript-syncing-refresh-error")
    }

    private var restoring: some View {
        VStack(spacing: 10) {
            ProgressView()
                .controlSize(.regular)
            Text("Restoring transcript…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
        .accessibilityIdentifier("session-transcript-restoring")
    }

    private func restoringWithError(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "arrow.clockwise.circle")
                .font(.system(size: 32))
                .foregroundStyle(.secondary)
            Text("Transcript unavailable")
                .font(.headline)
            Text(message)
                .font(.callout)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Try again", action: onRetry)
                .buttonStyle(.borderedProminent)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
        .accessibilityIdentifier("session-transcript-restoring-error")
    }

    private var syncing: some View {
        VStack(spacing: 10) {
            ProgressView()
                .controlSize(.regular)
            Text("Syncing transcript…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("session-transcript-syncing")
    }

    /// Cold load failed, nothing cached. Full-screen, readable, actionable —
    /// the antithesis of the near-invisible lone triangle this epic started on.
    private func hardError(_ message: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 40))
                .foregroundStyle(.orange)
            Text(message)
                .font(.callout)
                .multilineTextAlignment(.center)
                .foregroundStyle(.primary)
            Button("Try again", action: onRetry)
                .buttonStyle(.borderedProminent)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("session-transcript-hard-error")
    }

    /// Refresh failed but cached content is on screen. Thin, non-destructive.
    private func refreshBanner(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.caption)
            Text(message)
                .font(.caption)
                .lineLimit(2)
            Spacer(minLength: 8)
            Button(action: onRetry) {
                Text("Retry").font(.caption.weight(.semibold))
            }
        }
        .foregroundStyle(.orange)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .accessibilityIdentifier("session-refresh-banner")
    }
}
