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
                        .foregroundStyle(.red)
                }
                Button(action: submit) {
                    Label(isSubmitting ? "Starting…" : "Continue here", systemImage: "arrow.branch")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canSubmit)
                .accessibilityIdentifier("session-branch-button")
            }
        } else if let unavailableReason {
            // Silence would make a session Longhouse cannot continue look
            // identical to one it simply has not been asked to.
            Text(branchReasonLabel(unavailableReason))
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("session-branch-unavailable")
        }
    }
}

/// Why a branch is not on offer, in the user's terms rather than the
/// projector's. The two reasons branching adds over Resume are the ones it can
/// refuse for on its own: the provider cannot fork, or the parent's approvals
/// are not ones a headless turn can carry.
func branchReasonLabel(_ reason: String?) -> String {
    switch reason {
    case "fork_unsupported": return "Longhouse can't branch this provider yet."
    case "permission_mode_unknown", "permission_mode_unsupported":
        return "This session ran with approvals a branch can't carry."
    default: return "Can't continue this session here: \(resumeReasonLabel(reason))."
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

#Preview("Branch · Provider cannot fork") {
    BranchComposerCard(
        available: false,
        unavailableReason: "fork_unsupported",
        message: .constant(""),
        isSubmitting: false,
        errorMessage: nil,
        submit: {}
    )
    .padding()
}
