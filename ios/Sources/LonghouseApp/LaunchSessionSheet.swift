import SwiftUI

@MainActor
struct LaunchSessionSheet: View {
    private static let provider = "codex"

    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss

    let onLaunched: (String) -> Void
    private let previewMachines: [MachineDirectoryEntry]?
    private let previewWorkspacePaths: [String]?

    @State private var machines: [MachineDirectoryEntry]
    @State private var loadError: String?
    @State private var loading = false
    @State private var submitting = false
    @State private var submitError: String?

    @State private var selectedDeviceId: String = ""
    @State private var workspacePaths: [String]
    @State private var loadingWorkspaces = false
    @State private var workspaceError: String?
    @State private var cwd: String = ""
    @State private var displayName: String = ""

    init(
        previewMachines: [MachineDirectoryEntry]? = nil,
        previewWorkspacePaths: [String]? = nil,
        onLaunched: @escaping (String) -> Void
    ) {
        self.previewMachines = previewMachines
        self.previewWorkspacePaths = previewWorkspacePaths
        self.onLaunched = onLaunched
        _machines = State(initialValue: previewMachines ?? [])
        _workspacePaths = State(initialValue: previewWorkspacePaths ?? [])
        if let first = previewMachines?.first(where: { $0.isLaunchable }) {
            _selectedDeviceId = State(initialValue: first.deviceId)
        }
        if let firstPath = previewWorkspacePaths?.first {
            _cwd = State(initialValue: firstPath)
        }
    }

    private var launchable: [MachineDirectoryEntry] {
        machines.filter { $0.isLaunchable }
    }

    private var selectedMachine: MachineDirectoryEntry? {
        launchable.first { $0.deviceId == selectedDeviceId }
    }

    private var normalizedCwd: String {
        cwd.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSubmit: Bool {
        !submitting && selectedMachine != nil && normalizedCwd.starts(with: "/")
    }

    private var pathValidationMessage: String? {
        if normalizedCwd.isEmpty { return nil }
        if normalizedCwd.starts(with: "/") { return nil }
        if normalizedCwd.starts(with: "~") {
            return "Use the full absolute path for the target machine."
        }
        return "Path must start with /."
    }

    private var usesPreviewData: Bool {
        previewMachines != nil
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView("Loading machines...")
                } else if let loadError {
                    errorView(loadError)
                } else if launchable.isEmpty {
                    emptyView
                } else {
                    formView
                }
            }
            .navigationTitle("Start Session")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
        .task { await loadMachines() }
        .task(id: selectedDeviceId) {
            await loadWorkspaceSuggestions(for: selectedDeviceId)
        }
    }

    private var formView: some View {
        Form {
            Section("Machine") {
                Picker("Target", selection: $selectedDeviceId) {
                    ForEach(launchable, id: \.deviceId) { machine in
                        Text(machineLabel(machine)).tag(machine.deviceId)
                    }
                }
                .onChange(of: selectedDeviceId) { _, _ in
                    cwd = ""
                    workspaceError = nil
                    submitError = nil
                }
            }

            Section("Coding agent") {
                Picker("Agent", selection: .constant(Self.provider)) {
                    Text("Codex").tag(Self.provider)
                }
                Text("Runs codex app-server on \(selectedMachine?.machineName ?? "the selected machine") through the Longhouse Machine Agent.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Workspace") {
                if loadingWorkspaces {
                    ProgressView("Loading recent workspaces...")
                }

                if !workspacePaths.isEmpty {
                    WorkspacePathGrid(paths: workspacePaths, selectedPath: normalizedCwd) { path in
                        cwd = path
                        submitError = nil
                    }
                }

                TextField("Absolute path", text: $cwd)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                    .textContentType(.URL)

                if let workspaceError {
                    Text(workspaceError)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if let pathValidationMessage {
                    Text(pathValidationMessage)
                        .font(.caption)
                        .foregroundStyle(.red)
                } else {
                    Text("Existing absolute directory on the target machine.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Optional") {
                TextField("Display name", text: $displayName)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
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
                Text("No machine can start Codex right now.")
                    .font(.headline)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
                ForEach(machines, id: \.deviceId) { machine in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(machine.machineName).font(.footnote)
                            Text(launchBlockedLabel(machine))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
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
        if usesPreviewData {
            return
        }
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

    private func loadWorkspaceSuggestions(for deviceId: String) async {
        guard !usesPreviewData, !deviceId.isEmpty, let api = LonghouseAPI(host: appState.serverURL) else {
            return
        }
        loadingWorkspaces = true
        defer { loadingWorkspaces = false }
        workspaceError = nil
        do {
            let paths = try await api.recentWorkspacePaths(deviceId: deviceId)
            workspacePaths = paths
            if cwd.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, let first = paths.first {
                cwd = first
            }
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                return
            }
            workspacePaths = []
            workspaceError = "Recent workspaces unavailable."
        }
        loadingWorkspaces = false
    }

    private func submit() async {
        guard canSubmit, let api = LonghouseAPI(host: appState.serverURL) else { return }
        submitting = true
        submitError = nil
        defer { submitting = false }
        let trimmedDisplayName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let response = try await api.launchRemoteSession(
                deviceId: selectedDeviceId,
                provider: Self.provider,
                cwd: normalizedCwd,
                displayName: trimmedDisplayName.isEmpty ? nil : trimmedDisplayName,
                clientRequestId: "launch-\(UUID().uuidString)"
            )
            if response.launchState == .launchFailed || response.launchState == .launchOrphaned || response.launchState == .unknown {
                submitError = formatLaunchFailure(response)
                return
            }
            onLaunched(response.sessionId)
        } catch let LonghouseAPIError.structured(_, _, message) {
            submitError = message.isEmpty ? "Launch failed." : message
        } catch {
            submitError = (error as? LocalizedError)?.errorDescription ?? "Launch failed."
        }
    }

    private func machineLabel(_ machine: MachineDirectoryEntry) -> String {
        if let engineBuild = machine.engineBuild, !engineBuild.isEmpty {
            return "\(machine.machineName) (\(engineBuild))"
        }
        return machine.machineName
    }

    private func launchBlockedLabel(_ machine: MachineDirectoryEntry) -> String {
        switch machine.launchBlockedBy {
        case "control_down":
            return "control channel disconnected"
        case "no_codex_support":
            return "Codex launch is not advertised"
        case "engine_too_old":
            return "engine too old for Codex launch"
        case "auth_failed":
            return "control channel auth failed"
        case "runtime_unreachable":
            return "runtime host unreachable"
        default:
            return machine.online ? "launch unavailable" : "control channel disconnected"
        }
    }

    private func formatLaunchFailure(_ response: RemoteSessionLaunchResponse) -> String {
        let code = response.launchErrorCode?.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = response.launchErrorMessage?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let code, !code.isEmpty {
            let friendlyPrefix: String? = switch code {
            case "cwd_not_found", "cwd_not_allowed":
                "Check the workspace path"
            case "machine_offline":
                "Machine is offline"
            case "provider_unsupported":
                "Codex launch is unavailable on this machine"
            case "device_not_enrolled":
                "Machine is not enrolled"
            case "provider_launch_failed":
                "Codex failed to start"
            default:
                nil
            }
            if let friendlyPrefix, let message, !message.isEmpty {
                return "\(friendlyPrefix): \(message)"
            }
            if let friendlyPrefix { return friendlyPrefix }
            if let message, !message.isEmpty { return message }
            return code
        }
        if let message, !message.isEmpty { return message }
        if response.launchState == .unknown {
            return "Launch state was not recognized by this app build."
        }
        return "Launch failed."
    }
}

private struct WorkspacePathGrid: View {
    let paths: [String]
    let selectedPath: String
    let onSelect: (String) -> Void

    private let columns = [GridItem(.adaptive(minimum: 140), spacing: 8)]

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 8) {
            ForEach(paths, id: \.self) { path in
                Button {
                    onSelect(path)
                } label: {
                    Text(LonghouseAPI.compactWorkspacePath(path))
                        .font(.caption)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(path == selectedPath ? .accentColor : .secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

#Preview("Launch session") {
    LaunchSessionSheet(
        previewMachines: [
            MachineDirectoryEntry(
                deviceId: "cinder",
                machineName: "cinder",
                online: true,
                controlChannelStatus: "connected",
                supports: ["codex.launch", "codex.send"],
                canLaunchCodex: true,
                launchBlockedBy: nil,
                lastSeenAt: "2026-05-24T00:00:00Z",
                engineBuild: "dev"
            ),
        ],
        previewWorkspacePaths: [
            "/Users/davidrose/git/zerg/longhouse",
            "/Users/davidrose/git/zerg",
            "/Users/davidrose/git/me",
        ]
    ) { _ in }
    .environmentObject(AppState())
}
