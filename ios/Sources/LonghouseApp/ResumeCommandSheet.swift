import SwiftUI
import UIKit

struct ResumeCommandSheet: View {
    let intent: SessionResumeIntent
    let unexpectedStop: Bool
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                Text("Continue this Helm in a terminal with the same session and provider thread. Longhouse starts a new Helm run.")
                    .foregroundStyle(.secondary)
                if unexpectedStop {
                    Label(
                        "This Helm stopped unexpectedly. Resume continues from the provider's last recorded event.",
                        systemImage: "exclamationmark.triangle"
                    )
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.orange)
                }
                if intent.available, let command = intent.command {
                    Text(command)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                    Button {
                        UIPasteboard.general.string = command
                        copied = true
                    } label: {
                        Label(copied ? "Copied" : "Copy command", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Text("Resume is no longer available: \(resumeReasonLabel(intent.reason)).")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding()
            .navigationTitle("Resume on \(intent.machineLabel ?? intent.machineId ?? "its machine")")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium])
    }
}

func isUnexpectedResumeStop(_ reason: String?) -> Bool {
    reason == "provider_exit" || reason == "process_gone"
}

func resumeReasonLabel(_ reason: String?) -> String {
    switch reason {
    case "run_active": return "the Helm is still running; use Reattach"
    case "machine_offline": return "the machine is offline"
    case "machine_unknown": return "the machine has not reported current state"
    case "contract_missing": return "the retained launch contract is missing"
    case "contract_invalid": return "the retained launch contract is invalid"
    case "provider_state_missing": return "the provider's saved thread state is missing"
    case "workspace_mismatch", "workspace_missing": return "the original workspace is unavailable"
    case "provider_identity_mismatch": return "the retained provider thread no longer matches"
    case "provider_incompatible": return "the installed provider CLI no longer matches"
    case "not_helm": return "this was not a Helm session"
    case "unsupported": return "this provider does not support native Helm Resume"
    default: return "eligibility could not be confirmed"
    }
}
