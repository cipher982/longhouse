import SwiftUI

@MainActor
struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @State private var showingServerSheet = false
    @State private var showingSignOutConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    HStack {
                        Text(displayServer)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        Button("Change") { showingServerSheet = true }
                            .font(.callout)
                    }
                }

                Section {
                    Button(role: .destructive) {
                        showingSignOutConfirm = true
                    } label: {
                        Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                }

                Section {
                    LabeledContent("Version", value: appVersion)
                } footer: {
                    Text("Longhouse — native iOS pager")
                        .font(.caption2)
                }
            }
            .navigationTitle("Settings")
            .sheet(isPresented: $showingServerSheet) {
                ServerConfigSheet()
            }
            .confirmationDialog(
                "Sign out of Longhouse?",
                isPresented: $showingSignOutConfirm,
                titleVisibility: .visible
            ) {
                Button("Sign out", role: .destructive) { appState.signOut() }
                Button("Cancel", role: .cancel) { }
            }
        }
    }

    private var displayServer: String {
        let trimmed = appState.serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "Not configured" : trimmed
    }

    private var appVersion: String {
        let short = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
        return "\(short) (\(build))"
    }
}
