import SwiftUI

/// The four mutually-exclusive states the session transcript surface can be in.
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
    /// Content is on screen but the latest refresh failed. Non-destructive
    /// banner over the transcript; never erases content.
    case contentWithRefreshError(String)
    /// Content is on screen and healthy.
    case content
    /// Cold load failed with nothing cached. Full-screen, actionable error.
    case hardError(String)

    /// Derive the state from the raw view-model flags. Order matters: a
    /// blocking load takes precedence, then "do we have anything to show",
    /// then refresh health.
    static func derive(
        isInitialLoading: Bool,
        hasContent: Bool,
        errorMessage: String?,
        refreshErrorMessage: String?
    ) -> TranscriptDisplayState {
        if isInitialLoading {
            return .loading
        }
        if hasContent {
            if let refreshErrorMessage {
                return .contentWithRefreshError(refreshErrorMessage)
            }
            return .content
        }
        // Nothing on screen.
        if let errorMessage {
            return .hardError(errorMessage)
        }
        return .empty
    }

    /// True when the WebKit transcript should be visible/interactive underneath
    /// any overlay. The transcript stays mounted for `.content`,
    /// `.contentWithRefreshError`, and `.empty` (so its own "No messages yet"
    /// renders); it is hidden during blocking load and hard error.
    var showsTranscript: Bool {
        switch self {
        case .loading, .hardError:
            return false
        case .empty, .content, .contentWithRefreshError:
            return true
        }
    }
}

/// Single shared overlay for every transcript load state. Replaces the stacked
/// `if isInitialLoading / else if errorMessage / if refreshError` conditionals
/// that previously lived inline in `SessionView`. Renders nothing for the
/// healthy `.content` and `.empty` states (the WebKit transcript shows through).
struct TranscriptStateOverlay: View {
    let state: TranscriptDisplayState
    let onRetry: () -> Void

    var body: some View {
        switch state {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .hardError(let message):
            hardError(message)
        case .contentWithRefreshError(let message):
            VStack {
                refreshBanner(message)
                Spacer(minLength: 0)
            }
        case .content, .empty:
            EmptyView()
        }
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
