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
    @State private var loadingWorkspaces = false
    @State private var workspaceError: String?
    @State private var cwd: String = ""
    @State private var displayName: String = ""

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
        if let first = previewMachines?.first(where: { Self.canStartInteractiveSession($0) }) {
            let provider = first.defaultProvider ?? ""
            _selectedDeviceId = State(initialValue: first.deviceId)
            _selectedProvider = State(initialValue: provider)
        } else if let first = previewMachines?.first {
            _selectedDeviceId = State(initialValue: first.deviceId)
        }
        if let firstPath = previewWorkspaces?.first?.path {
            _cwd = State(initialValue: firstPath)
        }
    }

    private var selectedMachine: MachineDirectoryEntry? {
        machines.first { $0.deviceId == selectedDeviceId }
    }

    private var launchableMachines: [MachineDirectoryEntry] {
        machines.filter(Self.canStartInteractiveSession)
    }

    private var unavailableMachines: [MachineDirectoryEntry] {
        machines.filter { !Self.canStartInteractiveSession($0) }
    }

    private var normalizedCwd: String {
        cwd.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var availableProviders: [String] {
        selectedMachine?.remoteLaunchProviders ?? []
    }

    private var canSubmit: Bool {
        !submitting
            && (selectedMachine?.isLaunchable ?? false)
            && !selectedProvider.isEmpty
            && normalizedCwd.starts(with: "/")
            && availableProviders.contains(selectedProvider)
    }

    private var usesPreviewData: Bool {
        previewMachines != nil
    }

    private var selectedWorkspaceTitle: String {
        if let workspace = workspaces.first(where: { $0.path == normalizedCwd }) {
            return workspace.label
        }
        guard !normalizedCwd.isEmpty else { return loadingWorkspaces ? "Loading workspaces…" : "Choose a workspace" }
        return URL(fileURLWithPath: normalizedCwd).lastPathComponent
    }

    private var selectedWorkspaceSubtitle: String {
        guard !normalizedCwd.isEmpty else { return "Workspace" }
        return "Workspace · \(LonghouseAPI.compactWorkspacePath(normalizedCwd))"
    }

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView("Loading machines...")
                } else if let loadError {
                    errorView(loadError)
                } else if machines.isEmpty {
                    emptyView
                } else if launchableMachines.isEmpty {
                    MachineSelectionView(
                        machines: machines,
                        selectedDeviceId: selectedDeviceId,
                        statusText: launchBlockedLabel,
                        onSelect: selectMachine
                    )
                } else {
                    formView
                }
            }
            .navigationTitle("New Session")
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
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                launchSectionTitle("Machine")
                LaunchCard {
                    NavigationLink {
                        MachineSelectionView(
                            machines: machines,
                            selectedDeviceId: selectedDeviceId,
                            statusText: launchBlockedLabel,
                            onSelect: selectMachine
                        )
                    } label: {
                        LaunchSummaryRow(
                            title: selectedMachine?.machineName ?? "Choose a machine",
                            subtitle: selectedMachine.map { Self.canStartInteractiveSession($0) ? "Ready" : launchBlockedLabel($0) },
                            status: selectedMachine.map(machineStatusStyle),
                            showsChevron: true
                        )
                    }
                    .buttonStyle(.plain)
                }

                launchSectionTitle("Session")
                LaunchCard {
                    if availableProviders.count > 1 {
                        NavigationLink {
                            ProviderSelectionView(
                                providers: availableProviders,
                                selectedProvider: selectedProvider,
                                displayName: providerDisplayName
                            ) { provider in
                                selectedProvider = provider
                                submitError = nil
                            }
                        } label: {
                            LaunchSummaryRow(
                                title: providerDisplayName(selectedProvider),
                                subtitle: "Coding agent",
                                showsChevron: true
                            )
                        }
                        .buttonStyle(.plain)
                    } else {
                        LaunchSummaryRow(
                            title: providerDisplayName(selectedProvider),
                            subtitle: "Coding agent"
                        )
                    }

                    Divider().padding(.leading, 16)

                    NavigationLink {
                        WorkspaceSelectionView(
                            workspaces: workspaces,
                            selectedPath: normalizedCwd,
                            loading: loadingWorkspaces,
                            errorMessage: workspaceError
                        ) { path in
                            cwd = path
                            submitError = nil
                        }
                    } label: {
                        LaunchSummaryRow(
                            title: selectedWorkspaceTitle,
                            subtitle: selectedWorkspaceSubtitle,
                            showsChevron: true
                        )
                    }
                    .buttonStyle(.plain)
                }

                LaunchCard {
                    DisclosureGroup("Advanced options") {
                        VStack(alignment: .leading, spacing: 14) {
                            TextField("Session name (optional)", text: $displayName)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled(true)
                        }
                        .padding(.top, 14)
                    }
                    .padding(16)
                    .tint(.primary)
                }

                if let submitError {
                    Text(submitError)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 18)
            .padding(.bottom, 24)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Button {
                Task { await submit() }
            } label: {
                if submitting {
                    ProgressView().frame(maxWidth: .infinity)
                } else {
                    Text("Start session")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!canSubmit)
            .padding(.horizontal, 20)
            .padding(.vertical, 12)
            .background(.bar)
        }
    }

    private var emptyView: some View {
        VStack(spacing: 12) {
            Image(systemName: "desktopcomputer")
                .font(.system(size: 42))
                .foregroundStyle(.secondary)
            Text("No enrolled machines yet.")
                .font(.headline)
            Text("Install Longhouse on a machine with `longhouse connect` first.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal)
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
            let selectedStillExists = result.contains { $0.deviceId == selectedDeviceId }
            if (selectedDeviceId.isEmpty || !selectedStillExists),
               let first = result.first(where: { Self.canStartInteractiveSession($0) }) ?? result.first {
                selectedDeviceId = first.deviceId
                selectedProvider = first.defaultProvider ?? ""
            }
        } catch {
            loadError = (error as? LocalizedError)?.errorDescription ?? "Could not load machines."
        }
        loading = false
    }

    private func selectMachine(_ machine: MachineDirectoryEntry) {
        selectedDeviceId = machine.deviceId
        selectedProvider = machine.defaultProvider ?? ""
        cwd = ""
        workspaceError = nil
        submitError = nil
    }

    private func loadWorkspaceSuggestions(for deviceId: String) async {
        guard !usesPreviewData, !deviceId.isEmpty, let api = LonghouseAPI(host: appState.serverURL) else {
            return
        }
        guard deviceId == selectedDeviceId else { return }
        guard Self.canStartInteractiveSession(machines.first(where: { $0.deviceId == deviceId })) else {
            workspaces = []
            cwd = ""
            workspaceError = nil
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
        defer {
            if deviceId == selectedDeviceId {
                loadingWorkspaces = false
            }
        }
        workspaceError = nil
        do {
            let suggestions = try await api.workspaceSuggestions(deviceId: deviceId)
            guard !Task.isCancelled, deviceId == selectedDeviceId else { return }
            workspaces = suggestions
            if normalizedCwd.isEmpty, let first = suggestions.first?.path {
                cwd = first
            }
            WorkspaceSuggestionsCacheStore.save(workspaces: suggestions, serverURL: appState.serverURL, deviceId: deviceId)
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                return
            }
            guard deviceId == selectedDeviceId else { return }
            if workspaces.isEmpty {
                workspaceError = "Recent workspaces unavailable."
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
            let response = try await api.createConsoleSession(
                deviceId: selectedDeviceId,
                provider: selectedProvider,
                cwd: normalizedCwd,
                displayName: trimmedDisplayName.isEmpty ? nil : trimmedDisplayName
            )
            onLaunched(response.sessionId)
        } catch let LonghouseAPIError.structured(_, _, message) {
            submitError = message.isEmpty ? "Launch failed." : message
        } catch {
            submitError = (error as? LocalizedError)?.errorDescription ?? "Launch failed."
        }
    }

    private static func canStartInteractiveSession(_ machine: MachineDirectoryEntry?) -> Bool {
        machine?.isLaunchable ?? false
    }

    private func launchBlockedLabel(_ machine: MachineDirectoryEntry) -> String {
        switch machine.launch.blockedBy {
        case "control_down":
            return lastSeenLabel(machine)
        case "no_codex_support":
            return "Console launch unavailable"
        case "no_launch_support":
            return "Console launch unavailable"
        case "engine_too_old":
            return "Update required"
        case "auth_failed":
            return "Needs repair"
        case "runtime_unreachable":
            return "Needs repair"
        default:
            return machine.online ? "Console launch unavailable" : lastSeenLabel(machine)
        }
    }

    private func lastSeenLabel(_ machine: MachineDirectoryEntry) -> String {
        guard let raw = machine.lastSeenAt else { return "Offline" }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = fractional.date(from: raw) ?? ISO8601DateFormatter().date(from: raw) else { return "Offline" }
        guard date <= Date() else { return "Offline" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return "Offline · Last seen \(formatter.localizedString(for: date, relativeTo: Date()))"
    }

    private func machineStatusStyle(_ machine: MachineDirectoryEntry) -> LaunchStatusStyle {
        if Self.canStartInteractiveSession(machine) { return .ready }
        switch machine.launch.blockedBy {
        case "control_down": return .offline
        case "auth_failed", "runtime_unreachable": return .repair
        default: return .warning
        }
    }

    @ViewBuilder
    private func launchSectionTitle(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 2)
            .accessibilityAddTraits(.isHeader)
    }

    private func providerDisplayName(_ provider: String) -> String {
        switch provider {
        case "codex": "Codex"
        case "claude": "Claude"
        case "opencode": "OpenCode"
        case "cursor": "Cursor"
        case "antigravity": "Antigravity"
        default: provider
        }
    }

}

private enum LaunchStatusStyle {
    case ready
    case offline
    case warning
    case repair

    var color: Color {
        switch self {
        case .ready: .green
        case .offline: .secondary
        case .warning: .orange
        case .repair: .red
        }
    }
}

private struct LaunchCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct LaunchSummaryRow: View {
    let title: String
    let subtitle: String?
    var status: LaunchStatusStyle?
    var showsChevron = false

    var body: some View {
        HStack(spacing: 12) {
            if let status {
                ZStack {
                    if status == .offline {
                        Circle().stroke(status.color, lineWidth: 2)
                    } else {
                        Circle().fill(status.color)
                    }
                }
                .frame(width: 10, height: 10)
                .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.body)
                    .foregroundStyle(.primary)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 12)
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
        }
        .frame(minHeight: 48)
        .padding(.horizontal, 16)
        .padding(.vertical, 9)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

private struct MachineAvailabilityIcon: View {
    let machine: MachineDirectoryEntry

    var body: some View {
        Group {
            switch machine.launch.blockedBy {
            case "control_down":
                Circle().stroke(Color.secondary, lineWidth: 2)
            case "auth_failed", "runtime_unreachable":
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
            default:
                Image(systemName: "info.circle.fill")
                    .foregroundStyle(.orange)
            }
        }
        .frame(width: 14, height: 14)
        .accessibilityHidden(true)
    }
}

private struct MachineSelectionView: View {
    @Environment(\.dismiss) private var dismiss

    let machines: [MachineDirectoryEntry]
    let selectedDeviceId: String
    let statusText: (MachineDirectoryEntry) -> String
    let onSelect: (MachineDirectoryEntry) -> Void

    private var ready: [MachineDirectoryEntry] { machines.filter(\.isLaunchable) }
    private var unavailable: [MachineDirectoryEntry] { machines.filter { !$0.isLaunchable } }

    var body: some View {
        List {
            if ready.isEmpty {
                Section {
                    Text("No machines ready to launch")
                        .font(.headline)
                    Text("Your machines remain listed below and will become available when their Console connection returns.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            if !ready.isEmpty {
                Section("Available") {
                    ForEach(ready, id: \.deviceId) { machine in
                        Button {
                            onSelect(machine)
                            dismiss()
                        } label: {
                            HStack(spacing: 12) {
                                Circle().fill(Color.green).frame(width: 10, height: 10)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(machine.machineName).foregroundStyle(.primary)
                                    Text("Ready").font(.subheadline).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if machine.deviceId == selectedDeviceId {
                                    Image(systemName: "checkmark").fontWeight(.semibold)
                                }
                            }
                            .padding(.vertical, 5)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("\(machine.machineName), Ready")
                        .accessibilityAddTraits(machine.deviceId == selectedDeviceId ? .isSelected : [])
                    }
                }
            }

            if !unavailable.isEmpty {
                Section("Unavailable") {
                    ForEach(unavailable, id: \.deviceId) { machine in
                        HStack(spacing: 12) {
                            MachineAvailabilityIcon(machine: machine)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(machine.machineName).foregroundStyle(.primary)
                                Text(statusText(machine)).font(.subheadline).foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                        .padding(.vertical, 5)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("\(machine.machineName), \(statusText(machine)), Not available")
                    }
                }
            }
        }
        .navigationTitle("Choose Machine")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct ProviderSelectionView: View {
    @Environment(\.dismiss) private var dismiss

    let providers: [String]
    let selectedProvider: String
    let displayName: (String) -> String
    let onSelect: (String) -> Void

    var body: some View {
        List(providers, id: \.self) { provider in
            Button {
                onSelect(provider)
                dismiss()
            } label: {
                HStack {
                    Text(displayName(provider)).foregroundStyle(.primary)
                    Spacer()
                    if provider == selectedProvider { Image(systemName: "checkmark") }
                }
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(provider == selectedProvider ? .isSelected : [])
        }
        .navigationTitle("Choose Agent")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct WorkspaceSelectionView: View {
    @Environment(\.dismiss) private var dismiss

    let workspaces: [WorkspaceSuggestion]
    let selectedPath: String
    let loading: Bool
    let errorMessage: String?
    let onSelect: (String) -> Void

    @State private var search = ""
    @State private var manualPath = ""

    private var filtered: [WorkspaceSuggestion] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return workspaces }
        return workspaces.filter { $0.label.lowercased().contains(query) || $0.path.lowercased().contains(query) }
    }

    private var normalizedManualPath: String { manualPath.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        List {
            if loading { ProgressView("Loading recent workspaces…") }
            if let errorMessage, workspaces.isEmpty {
                Text(errorMessage).foregroundStyle(.secondary)
            }
            if !filtered.isEmpty {
                Section("Recent") {
                    ForEach(filtered) { workspace in
                        Button {
                            onSelect(workspace.path)
                            dismiss()
                        } label: {
                            HStack(spacing: 12) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(workspace.label).foregroundStyle(.primary).lineLimit(1)
                                    Text(LonghouseAPI.compactWorkspacePath(workspace.path))
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                }
                                Spacer()
                                if workspace.path == selectedPath { Image(systemName: "checkmark") }
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            Section("Other") {
                TextField("Absolute path", text: $manualPath)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                Button("Use this path") {
                    onSelect(normalizedManualPath)
                    dismiss()
                }
                .disabled(!normalizedManualPath.starts(with: "/"))
            }
        }
        .searchable(text: $search, prompt: "Filter workspaces")
        .navigationTitle("Choose Workspace")
        .navigationBarTitleDisplayMode(.inline)
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

private func previewMachine(
    deviceId: String = "cinder",
    machineName: String = "cinder",
    online: Bool = true,
    controlChannelStatus: String? = "connected",
    providers: [String] = ["claude", "codex", "opencode"],
    launchBlockedBy: String? = nil,
    lastSeenAt: String? = nil
) -> MachineDirectoryEntry {
    let launchProviders = online
        ? providers.map { MachineLaunchProviderOption(provider: $0) }
        : []
    return MachineDirectoryEntry(
        deviceId: deviceId,
        machineName: machineName,
        online: online,
        controlChannelStatus: controlChannelStatus,
        supports: ["codex.launch", "codex.run_once", "codex.send", "claude.launch"],
        controlOperationsByProvider: ["codex": ["launch", "run_once", "send"], "claude": ["launch"]],
        canLaunchCodex: true,
        launchableProviders: providers,
        launchBlockedBy: launchBlockedBy,
        lastSeenAt: lastSeenAt,
        engineBuild: "dev",
        launch: MachineLaunchProjection(
            blockedBy: launchProviders.isEmpty ? (launchBlockedBy ?? (online ? "no_launch_support" : "control_down")) : nil,
            providers: launchProviders,
            defaultProvider: launchProviders.isEmpty ? nil : (providers.contains("codex") ? "codex" : providers.first)
        )
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
    .preferredColorScheme(.dark)
}

#Preview("Launch session without recent workspaces") {
    LaunchSessionSheet(
        previewMachines: [previewMachine(providers: ["codex"])],
        previewWorkspaces: []
    ) { _ in }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
}

#Preview("Launch session offline machine") {
    LaunchSessionSheet(
        previewMachines: [
            previewMachine(
                online: false,
                controlChannelStatus: "disconnected",
                providers: ["codex"],
                launchBlockedBy: "control_down"
            )
        ],
        previewWorkspaces: []
    ) { _ in }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
}

#Preview("Launch machine chooser") {
    NavigationStack {
        MachineSelectionView(
            machines: [
                previewMachine(),
                previewMachine(
                    deviceId: "cube-canary",
                    machineName: "cube",
                    online: false,
                    controlChannelStatus: "disconnected",
                    providers: [],
                    launchBlockedBy: "control_down"
                ),
                previewMachine(
                    deviceId: "old-engine",
                    machineName: "studio mac",
                    providers: [],
                    launchBlockedBy: "no_launch_support"
                ),
                previewMachine(
                    deviceId: "repair-host",
                    machineName: "lab",
                    providers: [],
                    launchBlockedBy: "auth_failed"
                ),
            ],
            selectedDeviceId: "cinder",
            statusText: { machine in
                switch machine.launch.blockedBy {
                case "auth_failed", "runtime_unreachable": "Needs repair"
                case "control_down": "Offline · Last seen 2 days ago"
                default: "Console launch unavailable"
                }
            },
            onSelect: { _ in }
        )
    }
    .preferredColorScheme(.dark)
}
