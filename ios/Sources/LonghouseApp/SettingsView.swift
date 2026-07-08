import SwiftUI

@MainActor
struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var showingServerSheet = false
    @State private var showingSignOutConfirm = false
    @State private var apnsEnabled = true
    @State private var notificationsLoaded = false
    @State private var notificationsError: String?
    @State private var isSavingNotifications = false

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
                    Toggle("Attention alerts", isOn: Binding(
                        get: { apnsEnabled },
                        set: { newValue in
                            let previousValue = apnsEnabled
                            apnsEnabled = newValue
                            Task { await updateNotificationPreference(newValue, previousValue: previousValue) }
                        }
                    ))
                    .disabled(!notificationsLoaded || isSavingNotifications)

                    if let notificationsError {
                        Text(notificationsError)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Notifications")
                } footer: {
                    Text("Alerts fire when a session is waiting for you or needs permission. The app asks for iOS notification permission the first time it opens.")
                        .font(.caption2)
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
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
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
            .task(id: appState.serverURL) {
                await loadNotificationSettings()
            }
        }
    }

    private var displayServer: String {
        let trimmed = appState.serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "Not configured" : trimmed
    }

    private var appVersion: String {
        switch BuildIdentityLoader.loadFromMainBundle() {
        case .success(let identity):
            return identity.qualifiedVersion
        case .failure(.resourceMissing):
            return "build identity missing"
        case .failure(.decodeFailed):
            return "build identity: decode failed"
        case .failure(.invalidPayload(let reason)):
            return "build identity: invalid (\(reason))"
        }
    }

    private func loadNotificationSettings() async {
        notificationsLoaded = false
        notificationsError = nil

        guard let api = LonghouseAPI(host: appState.serverURL) else {
            notificationsError = "Invalid server URL"
            return
        }

        do {
            let settings = try await api.notificationSettings()
            apnsEnabled = settings.apnsEnabled
        } catch LonghouseAPIError.notAuthenticated {
            notificationsError = "Sign in again to manage notifications."
        } catch {
            notificationsError = "Couldn't load notification settings."
        }

        notificationsLoaded = true
    }

    private func updateNotificationPreference(_ enabled: Bool, previousValue: Bool) async {
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            apnsEnabled = previousValue
            notificationsError = "Invalid server URL"
            return
        }

        isSavingNotifications = true
        defer { isSavingNotifications = false }

        do {
            let settings = try await api.updateNotificationSettings(apnsEnabled: enabled)
            apnsEnabled = settings.apnsEnabled
            notificationsError = nil
        } catch LonghouseAPIError.notAuthenticated {
            apnsEnabled = previousValue
            notificationsError = "Sign in again to manage notifications."
        } catch {
            apnsEnabled = previousValue
            notificationsError = "Couldn't save notification settings."
        }
    }
}

#Preview("Settings sheet") {
    SettingsView()
        .environmentObject(AppState())
}
