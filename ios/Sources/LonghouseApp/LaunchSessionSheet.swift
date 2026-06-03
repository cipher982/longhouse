import SwiftUI

@MainActor
struct LaunchSessionSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss

    let onLaunched: (String) -> Void
    private let previewMachines: [MachineDirectoryEntry]?
    private let previewWorkspaces: [WorkspaceSuggestion]?

    @State private var machines: [MachineDirectoryEntry]
    @State private var loadError: String?
    @State private var loading = false
    @State private var submitting = false
    @State private var submitError: String?

    @State private var selectedDeviceId: String = ""
    @State private var selectedProvider: String = ""
    @State private var workspaces: [WorkspaceSuggestion]
    @State private var workspaceSearch: String = ""
    @State private var loadingWorkspaces = false
    @State private var workspaceError: String?
    @State private var cwd: String = ""
    @State private var displayName: String = ""
    @State private var showManualPath = false

    init(
        previewMachines: [MachineDirectoryEntry]? = nil,
        previewWorkspaces: [WorkspaceSuggestion]? = nil,
        onLaunched: @escaping (String) -> Void
    ) {
        self.previewMachines = previewMachines
        self.previewWorkspaces = previewWorkspaces
        self.onLaunched = onLaunched
        _machines = State(initialValue: previewMachines ?? [])
        _workspaces = State(initialValue: previewWorkspaces ?? [])
        if let first = previewMachines?.first(where: { $0.isLaunchable }) {
            _selectedDeviceId = State(initialValue: first.deviceId)
            _selectedProvider = State(initialValue: first.defaultProvider ?? "")
        }
        if let firstPath = previewWorkspaces?.first?.path {
            _cwd = State(initialValue: firstPath)
        }
        if previewWorkspaces?.isEmpty == true {
            _showManualPath = State(initialValue: true)
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
        !submitting && selectedMachine != nil && !selectedProvider.isEmpty && normalizedCwd.starts(with: "/")
    }

    private var filteredWorkspaces: [WorkspaceSuggestion] {
        let q = workspaceSearch.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return workspaces }
        return workspaces.filter { $0.path.lowercased().contains(q) || $0.label.lowercased().contains(q) }
    }

    private var hasWorkspaceSuggestions: Bool {
        !workspaces.isEmpty
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
                    workspaceSearch = ""
                    workspaceError = nil
                    submitError = nil
                    showManualPath = false
                    selectedProvider = selectedMachine?.defaultProvider ?? ""
                }
            }

            Section("Coding agent") {
                if let machine = selectedMachine, machine.launchableProviders.count > 1 {
                    Picker("Provider", selection: $selectedProvider) {
                        ForEach(machine.launchableProviders, id: \.self) { provider in
                            Text(provider).tag(provider)
                        }
                    }
                } else {
                    HStack(spacing: 10) {
                        Image(systemName: "terminal")
                            .foregroundStyle(.secondary)
                        Text(selectedProvider.isEmpty ? "codex" : selectedProvider)
                    }
                }
            }

            Section("Recent workspaces") {
                if loadingWorkspaces {
                    ProgressView("Loading recent workspaces...")
                }

                if !workspaces.isEmpty {
                    TextField("Filter workspaces", text: $workspaceSearch)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled(true)
                    WorkspaceSuggestionList(workspaces: filteredWorkspaces, selectedPath: normalizedCwd) { path in
                        cwd = path
                        showManualPath = false
                        submitError = nil
                    }
                } else if !loadingWorkspaces {
                    Text(workspaceError ?? "No recent workspaces found for this machine.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Manual path") {
                DisclosureGroup(isExpanded: $showManualPath) {
                    TextField("Absolute path", text: $cwd)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled(true)

                    if let pathValidationMessage {
                        Text(pathValidationMessage)
                            .font(.caption)
                            .foregroundStyle(.red)
                    } else {
                        Text("Existing absolute directory on the target machine.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } label: {
                    Label("Use a different path", systemImage: "keyboard")
                }
                .onChange(of: cwd) { _, _ in
                    submitError = nil
                }

                if !hasWorkspaceSuggestions && !loadingWorkspaces {
                    Text("Use this when the workspace has not appeared in recent sessions yet.")
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
                selectedProvider = first.defaultProvider ?? ""
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
        // Render cached workspaces instantly, then revalidate.
        if let cached = WorkspaceSuggestionsCacheStore.load(serverURL: appState.serverURL, deviceId: deviceId) {
            workspaces = cached
            if normalizedCwd.isEmpty, let first = cached.first?.path {
                cwd = first
            }
        }
        loadingWorkspaces = true
        defer { loadingWorkspaces = false }
        workspaceError = nil
        do {
            let suggestions = try await api.workspaceSuggestions(deviceId: deviceId)
            workspaces = suggestions
            if normalizedCwd.isEmpty, let first = suggestions.first?.path {
                cwd = first
            }
            showManualPath = suggestions.isEmpty
            WorkspaceSuggestionsCacheStore.save(workspaces: suggestions, serverURL: appState.serverURL, deviceId: deviceId)
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                return
            }
            if workspaces.isEmpty {
                workspaceError = "Recent workspaces unavailable."
                showManualPath = true
            }
        }
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
                provider: selectedProvider,
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
        let message = response.launchErrorMessage?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let message, !message.isEmpty { return message }
        let code = response.launchErrorCode?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let code, !code.isEmpty { return code }
        if response.launchState == .unknown {
            return "Launch state was not recognized by this app build."
        }
        return "Launch failed."
    }
}

/// Persists the launch-picker workspace list so the sheet renders instantly
/// on open, then revalidates from the server. Mirrors ``TimelineCacheStore``
/// but is keyed per (serverURL, identity, deviceId) so switching machines
/// never shows another machine's paths.
enum WorkspaceSuggestionsCacheStore {
    private static let cacheKey = "longhouse.launch.workspaces.cache.v1"
    private static let version = 1
    private static let maxItems = 24
    private static let defaultMaxAge: TimeInterval = 24 * 60 * 60

    private struct Payload: Codable {
        let version: Int
        let serverURL: String
        let identity: String?
        let deviceId: String
        let savedAt: Date
        let workspaces: [WorkspaceSuggestion]
    }

    static func save(
        workspaces: [WorkspaceSuggestion],
        serverURL: String,
        deviceId: String,
        identity: String? = nil,
        defaults: UserDefaults = .standard,
        now: Date = Date()
    ) {
        let normalizedServer = normalize(serverURL)
        guard !normalizedServer.isEmpty, !deviceId.isEmpty, !workspaces.isEmpty else { return }
        let payload = Payload(
            version: version,
            serverURL: normalizedServer,
            identity: normalizedIdentity(identity),
            deviceId: deviceId,
            savedAt: now,
            workspaces: Array(workspaces.prefix(maxItems))
        )
        guard let data = try? JSONEncoder().encode(payload) else { return }
        defaults.set(data, forKey: cacheKey)
    }

    static func load(
        serverURL: String,
        deviceId: String,
        identity: String? = nil,
        defaults: UserDefaults = .standard,
        now: Date = Date(),
        maxAge: TimeInterval = defaultMaxAge
    ) -> [WorkspaceSuggestion]? {
        guard let data = defaults.data(forKey: cacheKey),
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            return nil
        }
        guard payload.version == version else { return nil }
        guard payload.serverURL == normalize(serverURL) else { return nil }
        guard payload.identity == normalizedIdentity(identity) else { return nil }
        guard payload.deviceId == deviceId else { return nil }
        guard now.timeIntervalSince(payload.savedAt) <= maxAge else { return nil }
        guard !payload.workspaces.isEmpty else { return nil }
        return payload.workspaces
    }

    private static func normalize(_ serverURL: String) -> String {
        var value = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") {
            value.removeLast()
        }
        return value
    }

    private static func normalizedIdentity(_ identity: String?) -> String? {
        let value = identity?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }
}

private struct WorkspaceSuggestionList: View {
    let workspaces: [WorkspaceSuggestion]
    let selectedPath: String
    let onSelect: (String) -> Void

    var body: some View {
        VStack(spacing: 0) {
            ForEach(workspaces) { workspace in
                Button {
                    onSelect(workspace.path)
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: workspace.path == selectedPath ? "checkmark.circle.fill" : iconName(workspace))
                            .foregroundStyle(workspace.path == selectedPath ? Color.accentColor : Color.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(workspace.label)
                                .font(.body)
                                .lineLimit(1)
                            Text(LonghouseAPI.compactWorkspacePath(workspace.path))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(workspace.path == selectedPath ? .isSelected : [])
                .padding(.vertical, 7)
            }
        }
    }

    private func iconName(_ workspace: WorkspaceSuggestion) -> String {
        workspace.gitRepo != nil ? "arrow.triangle.branch" : "folder"
    }
}

private func previewMachine(providers: [String] = ["claude", "codex", "opencode"]) -> MachineDirectoryEntry {
    MachineDirectoryEntry(
        deviceId: "cinder",
        machineName: "cinder",
        online: true,
        controlChannelStatus: "connected",
        supports: ["codex.launch", "codex.send", "claude.launch"],
        canLaunchCodex: true,
        launchableProviders: providers,
        launchBlockedBy: nil,
        lastSeenAt: "2026-05-24T00:00:00Z",
        engineBuild: "dev"
    )
}

#Preview("Launch session") {
    LaunchSessionSheet(
        previewMachines: [previewMachine()],
        previewWorkspaces: [
            WorkspaceSuggestion(
                path: "/Users/example/git/zerg/longhouse",
                label: "longhouse (main)",
                gitRepo: "git@github.com:cipher982/longhouse.git",
                gitBranch: "main",
                score: 22590,
                sessionCount: 422
            ),
            WorkspaceSuggestion(path: "/Users/example/git/zerg", label: "zerg", score: 12590, sessionCount: 390),
            WorkspaceSuggestion(path: "/Users/example", label: "~", score: 5310, sessionCount: 120),
            WorkspaceSuggestion(
                path: "/Users/example/git/agent-observatory",
                label: "agent-observatory (ne-epic)",
                gitRepo: "git@github.com:cipher982/agent-observatory.git",
                gitBranch: "ne-epic",
                score: 2890,
                sessionCount: 31
            ),
        ]
    ) { _ in }
    .environmentObject(AppState())
}

#Preview("Launch session without recent workspaces") {
    LaunchSessionSheet(
        previewMachines: [previewMachine(providers: ["codex"])],
        previewWorkspaces: []
    ) { _ in }
    .environmentObject(AppState())
}
