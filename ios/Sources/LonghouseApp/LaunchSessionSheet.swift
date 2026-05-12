import SwiftUI

/// Phase 3 of the remote-session-launch epic. Mirrors the web launch sheet:
/// list enrolled machines, require one that advertises codex.launch and is
/// online, take an absolute cwd, POST /api/sessions/launch, and hand back the
/// new session id for navigation.
@MainActor
struct LaunchSessionSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss

    let onLaunched: (String) -> Void

    @State private var machines: [MachineDirectoryEntry] = []
    @State private var loadError: String?
    @State private var loading = false
    @State private var submitting = false
    @State private var submitError: String?

    @State private var selectedDeviceId: String = ""
    @State private var cwd: String = ""
    @State private var displayName: String = ""

    private var launchable: [MachineDirectoryEntry] {
        machines.filter { $0.isLaunchable }
    }

    private var canSubmit: Bool {
        !submitting && !selectedDeviceId.isEmpty && !cwd.trimmingCharacters(in: .whitespaces).isEmpty
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView("Loading machines…")
                } else if let loadError {
                    errorView(loadError)
                } else if launchable.isEmpty {
                    emptyView
                } else {
                    formView
                }
            }
            .navigationTitle("Start session")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
        .task { await loadMachines() }
    }

    private var formView: some View {
        Form {
            Section("Machine") {
                Picker("Target", selection: $selectedDeviceId) {
                    ForEach(launchable, id: \.deviceId) { m in
                        Text(m.machineName).tag(m.deviceId)
                    }
                }
            }
            Section("Workspace") {
                TextField("/Users/you/git/your-repo", text: $cwd)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                Text("Must be absolute and under $HOME on the target machine.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Optional") {
                TextField("Display name", text: $displayName)
                    .textInputAutocapitalization(.never)
            }
            if let submitError {
                Section {
                    Text(submitError)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
            Section {
                Button {
                    Task { await submit() }
                } label: {
                    if submitting {
                        ProgressView().frame(maxWidth: .infinity)
                    } else {
                        Text("Start").frame(maxWidth: .infinity)
                    }
                }
                .disabled(!canSubmit)
            }
        }
    }

    private var emptyView: some View {
        VStack(spacing: 12) {
            Image(systemName: "desktopcomputer")
                .font(.system(size: 42))
                .foregroundStyle(.secondary)
            if machines.isEmpty {
                Text("No enrolled machines yet.")
                    .font(.headline)
                Text("Install Longhouse on a machine with `longhouse connect` first.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
            } else {
                Text("No machines are online with Codex support right now.")
                    .font(.headline)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
                ForEach(machines, id: \.deviceId) { m in
                    HStack {
                        Text(m.machineName).font(.footnote)
                        Spacer()
                        Text(m.online ? "online" : "offline")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 24)
                }
            }
        }
    }

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 32))
                .foregroundStyle(.red)
            Text(message)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button("Retry") {
                Task { await loadMachines() }
            }
        }
    }

    private func loadMachines() async {
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            loadError = "Not authenticated."
            return
        }
        loading = true
        loadError = nil
        do {
            let result = try await api.listMachines()
            machines = result
            if selectedDeviceId.isEmpty, let first = result.first(where: { $0.isLaunchable }) {
                selectedDeviceId = first.deviceId
            }
        } catch {
            loadError = (error as? LocalizedError)?.errorDescription ?? "Could not load machines."
        }
        loading = false
    }

    private func submit() async {
        guard canSubmit, let api = LonghouseAPI(host: appState.serverURL) else { return }
        submitting = true
        submitError = nil
        defer { submitting = false }
        let clientRequestId = "launch-\(selectedDeviceId)-\(Int(Date().timeIntervalSince1970 * 1000))"
        do {
            let response = try await api.launchRemoteSession(
                deviceId: selectedDeviceId,
                cwd: cwd.trimmingCharacters(in: .whitespaces),
                displayName: displayName.trimmingCharacters(in: .whitespaces).isEmpty ? nil : displayName,
                clientRequestId: clientRequestId
            )
            onLaunched(response.sessionId)
        } catch let LonghouseAPIError.structured(_, _, message) {
            submitError = message.isEmpty ? "Launch failed." : message
        } catch {
            submitError = (error as? LocalizedError)?.errorDescription ?? "Launch failed."
        }
    }
}
