import SwiftUI

/// Pick up an ended session from the phone.
///
/// Resume hands back a shell command, which is the right answer standing at the
/// machine and useless everywhere else — including the device this app runs on.
/// Branching is the same continuation shaped as a text box: it starts a new
/// session that forks the provider's conversation, leaving the original exactly
/// as it was.
struct BranchComposerCard: View {
    let available: Bool
    let unavailableReason: String?
    @Binding var message: String
    let isSubmitting: Bool
    let errorMessage: String?
    let submit: () -> Void

    private var canSubmit: Bool {
        !isSubmitting && !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        if available {
            VStack(alignment: .leading, spacing: 8) {
                Text("Pick up where this left off")
                    .font(.subheadline.weight(.semibold))
                TextField("What should it do next?", text: $message, axis: .vertical)
                    .lineLimit(2...5)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isSubmitting)
                    .accessibilityIdentifier("session-branch-input")
                if let errorMessage {
                    Text(errorMessage)
                        .font(.caption)
                        .foregroundStyle(Ember.ember)
                }
                Button(action: submit) {
                    Label(isSubmitting ? "Starting…" : "Continue here", systemImage: "arrow.branch")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(!canSubmit)
                .accessibilityIdentifier("session-branch-button")
            }
        } else if let note = branchReasonLabel(unavailableReason) {
            Text(note)
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("session-branch-unavailable")
        }
    }
}

/// What to say about a branch that is not on offer, or nil to say nothing.
///
/// A reason gets words only when it tells the reader something the rest of the
/// screen does not. Resume's own blockers are explained once, in the Resume
/// footer; a provider that cannot fork yet is a roadmap fact nobody can act on
/// and showed on most ended sessions. The approval reasons are about this
/// session, and the answer is to resume it at the machine. Same rule and same
/// words as the web's `branchUnavailableNote`.
func branchReasonLabel(_ reason: String?) -> String? {
    switch reason {
    case "permission_mode_unknown":
        return "Longhouse couldn't verify this session's approval settings, so it can't be branched. Resume it in the terminal instead."
    case "permission_mode_unsupported":
        return "A branch runs without approval prompts and this session ran with them. Resume it in the terminal instead."
    default: return nil
    }
}

#Preview("Branch · Available") {
    BranchComposerCard(
        available: true,
        unavailableReason: nil,
        message: .constant(""),
        isSubmitting: false,
        errorMessage: nil,
        submit: {}
    )
    .padding()
}

#Preview("Branch · Submitting with a draft") {
    BranchComposerCard(
        available: true,
        unavailableReason: nil,
        message: .constant("finish the migration and run the tests"),
        isSubmitting: true,
        errorMessage: nil,
        submit: {}
    )
    .padding()
}

#Preview("Branch · Failed, draft kept") {
    BranchComposerCard(
        available: true,
        unavailableReason: nil,
        message: .constant("finish the migration"),
        isSubmitting: false,
        errorMessage: "Couldn't start the branch. Try again.",
        submit: {}
    )
    .padding()
}

#Preview("Branch · Session ran with approvals") {
    BranchComposerCard(
        available: false,
        unavailableReason: "permission_mode_unsupported",
        message: .constant(""),
        isSubmitting: false,
        errorMessage: nil,
        submit: {}
    )
    .padding()
}
