import SwiftUI

// Mirrors `bottomChrome`: the card rides in a translucent rounded surface
// pinned to the bottom with transcript space above, so previews read the way
// the screen actually looks instead of floating in black.
private struct PauseRequestPreviewChrome<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            VStack(alignment: .leading, spacing: 8) {
                content
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .fill(.ultraThinMaterial)
                    .overlay(
                        RoundedRectangle(cornerRadius: 24, style: .continuous)
                            .strokeBorder(.white.opacity(0.10), lineWidth: 0.75)
                    )
            )
            .shadow(color: .black.opacity(0.28), radius: 16, y: 5)
            .padding(.horizontal, 12)
            .padding(.bottom, 10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
        .preferredColorScheme(.dark)
    }
}

#Preview("Session pause request") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-preview",
                sessionId: "session-preview",
                runtimeKey: "codex:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "codex",
                canRespond: true,
                title: "Choose storage",
                summary: "Codex needs a storage decision before it can continue.",
                toolName: "requestUserInput",
                questions: [
                    SessionPauseQuestion(
                        id: "storage",
                        header: "Storage",
                        question: "Which storage backend should I implement?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "SQLite", description: "Keep it local and simple.", value: "sqlite"),
                            SessionPauseQuestionOption(label: "Postgres", description: "Use managed database features.", value: "postgres"),
                        ]
                    )
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}

#Preview("Session pause request · multi-question pager") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-multi-preview",
                sessionId: "session-preview",
                runtimeKey: "claude:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "claude",
                canRespond: true,
                title: "Wait model",
                summary: "Waiting for your answer.",
                toolName: "AskUserQuestion",
                questions: [
                    SessionPauseQuestion(
                        id: "wait_model",
                        header: "Wait model",
                        question: "Your phone's in your pocket — replies can lag 30-60 min, well past a sane block window. How should the agent behave when it asks for approval?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "Async grant: request, don't block, resume on reply", description: "Agent sends the SMS and returns 'pending' immediately (no long block). Your YES — whenever it lands — creates a standing time-boxed grant. The agent checks back and proceeds the moment the grant exists.", value: "async"),
                            SessionPauseQuestionOption(label: "Long block with grace, then convert to async", description: "Block for a modest window (e.g. 10 min) for the common quick-reply case; if it times out, DON'T discard — leave the request standing so a later reply still grants access.", value: "grace"),
                            SessionPauseQuestionOption(label: "Approve-ahead / batch", description: "Agent lists everything it'll need up front, sends ONE approval, you reply once, and it proceeds through all of them. Fewer texts, front-loads the wait.", value: "batch"),
                        ]
                    ),
                    SessionPauseQuestion(
                        id: "late_reply",
                        header: "Late reply",
                        question: "When a YES finally lands after the agent moved on, what should happen?",
                        multiSelect: false,
                        options: [
                            SessionPauseQuestionOption(label: "Stand as a grant for a window", description: "A late YES creates a time-boxed grant (e.g. valid 1h). Next time anything needs that cred within the window, it proceeds with NO new SMS. (Recommended)", value: "grant"),
                            SessionPauseQuestionOption(label: "Notify + resume the paused task", description: "A late YES actively pings the waiting agent/task to wake up and continue right then. More 'live' but needs a running listener + task-resume plumbing.", value: "resume"),
                            SessionPauseQuestionOption(label: "Just record it, require fresh request", description: "Late YES is logged but does nothing on its own; the agent must re-request next time. Simplest, but wastes your reply.", value: "record"),
                        ]
                    ),
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}

#Preview("Session pause request · terminal only") {
    PauseRequestPreviewChrome {
        SessionPauseRequestCard(
            pauseRequest: SessionPauseRequest(
                id: "pause-terminal-preview",
                sessionId: "session-preview",
                runtimeKey: "claude:session-preview",
                kind: "structured_question",
                status: "pending",
                provider: "claude",
                canRespond: false,
                title: "Claude needs an answer",
                summary: "Answer this in the original terminal.",
                toolName: "AskUserQuestion",
                questions: [
                    SessionPauseQuestion(
                        id: "terminal_answer",
                        header: nil,
                        question: "Claude is waiting for an interactive answer in the terminal.",
                        multiSelect: false,
                        options: []
                    )
                ],
                occurredAt: nil,
                lastSeenAt: nil,
                resolvedAt: nil,
                expiresAt: nil
            ),
            isResponding: false,
            errorMessage: nil,
            onRespond: { _, _, _, _ in true }
        )
    }
}
