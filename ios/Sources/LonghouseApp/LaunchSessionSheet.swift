import OSLog
import SwiftUI

enum WorkspaceSelectionSource: Equatable {
    case implicitDefault
    case explicitUserChoice
}


struct ConsoleLaunchSelection: Sendable {
    let sessionId: String
    let deviceId: String
    let provider: String
    let cwd: String
}
struct WorkspaceSelectionResolution: Equatable {
    let path: String
    let source: WorkspaceSelectionSource
}

func resolveFreshWorkspaceSelection(
    currentPath: String,
    source: WorkspaceSelectionSource,
    suggestions: [WorkspaceSuggestion]
) -> WorkspaceSelectionResolution {
    let normalized = currentPath.trimmingCharacters(in: .whitespacesAndNewlines)
    if source == .explicitUserChoice, normalized.starts(with: "/") {
        return WorkspaceSelectionResolution(path: normalized, source: .explicitUserChoice)
    }
    return WorkspaceSelectionResolution(
        path: suggestions.first?.path ?? "",
        source: .implicitDefault
    )
}

@MainActor
struct LaunchSessionSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss
    let onLaunched: (String) -> Void
    let onLaunchSelection: ((ConsoleLaunchSelection) -> Void)?
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
    @State private var workspaceSelectionSource: WorkspaceSelectionSource = .implicitDefault
    @State private var displayName: String = ""
    @State private var sessionId = UUID().uuidString
    @State private var threadId = UUID().uuidString
    @State private var launchIdentityKey: String?
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "LaunchSession")
    init(
        previewMachines: [MachineDirectoryEntry]? = nil,
        previewWorkspaces: [WorkspaceSuggestion]? = nil,
        onLaunchSelection: ((ConsoleLaunchSelection) -> Void)? = nil,
        onLaunched: @escaping (String) -> Void
    ) {
        self.previewMachines = previewMachines
        self.previewWorkspaces = previewWorkspaces
        self.onLaunchSelection = onLaunchSelection
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
        selectedMachine?.consoleLaunchProviders ?? []
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
                } else if launchableMachines.isEmpty && !machines.contains(where: Self.needsProviderSignIn) {
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
                    .accessibilityIdentifier("launch-machine-picker")
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
                        .accessibilityIdentifier("launch-provider-picker")
                    } else {
                        LaunchSummaryRow(
                            title: providerDisplayName(selectedProvider),
                            subtitle: "Coding agent"
                        )
                    }

                    // Signed-out or missing providers are never offered, but a
                    // user who expected Claude here needs to know why it is not.
                    ForEach(selectedMachine?.launch.unavailableProviders ?? [], id: \.provider) { item in
                        ProviderSignInRow(
                            deviceId: selectedDeviceId,
                            machineName: selectedMachine?.machineName ?? selectedDeviceId,
                            item: item,
                            displayName: providerDisplayName(item.provider),
                            canRelay: item.reason == "not_authenticated"
                                && (selectedMachine?.supports.contains("\(item.provider).sign_in") ?? false),
                            makeAPI: { LonghouseAPI(host: appState.serverURL) },
                            refreshMachines: refreshMachinesQuietly
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
                            workspaceSelectionSource = .explicitUserChoice
                            submitError = nil
                            logger.info("workspace selection committed path=\(path, privacy: .public)")
                        }
                    } label: {
                        LaunchSummaryRow(
                            title: selectedWorkspaceTitle,
                            subtitle: selectedWorkspaceSubtitle,
                            showsChevron: true
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("launch-workspace-picker")
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
                    .tint(Ember.textSecondary)
                }

                if let submitError {
                    Text(submitError)
                        .font(.footnote)
                        .foregroundStyle(Ember.ember)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 18)
            .padding(.bottom, 24)
        }
        .background(Ember.page)
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
            .buttonStyle(EmberPrimaryButtonStyle())
            .disabled(!canSubmit)
            .accessibilityIdentifier("launch-submit")
            .padding(.horizontal, 20)
            .padding(.vertical, 12)
            .background {
                Ember.page
                    .overlay(alignment: .top) { Ember.hairline.frame(height: 0.75) }
                    .ignoresSafeArea()
            }
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
                .foregroundStyle(Ember.ember)
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
               let first = result.first(where: { Self.canStartInteractiveSession($0) })
                ?? result.first(where: Self.needsProviderSignIn)
                ?? result.first {
                selectedDeviceId = first.deviceId
                selectedProvider = first.defaultProvider ?? ""
                cwd = ""
                workspaceSelectionSource = .implicitDefault
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
        workspaceSelectionSource = .implicitDefault
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
            workspaceSelectionSource = .implicitDefault
            workspaceError = nil
            return
        }
        let startedAt = Date()
        var cacheHit = false
        // Render cached workspaces instantly, then revalidate.
        if let cached = WorkspaceSuggestionsCacheStore.load(serverURL: appState.serverURL, deviceId: deviceId) {
            cacheHit = true
            workspaces = cached
            if workspaceSelectionSource == .implicitDefault, normalizedCwd.isEmpty, let first = cached.first?.path {
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
        logger.info("workspace suggestions load started device=\(deviceId, privacy: .public) cache_hit=\(cacheHit, privacy: .public) cached_count=\(self.workspaces.count, privacy: .public)")
        do {
            let suggestions = try await api.workspaceSuggestions(deviceId: deviceId)
            guard !Task.isCancelled, deviceId == selectedDeviceId else { return }
            workspaces = suggestions
            let selection = resolveFreshWorkspaceSelection(
                currentPath: normalizedCwd,
                source: workspaceSelectionSource,
                suggestions: suggestions
            )
            cwd = selection.path
            workspaceSelectionSource = selection.source
            WorkspaceSuggestionsCacheStore.save(workspaces: suggestions, serverURL: appState.serverURL, deviceId: deviceId)
            logger.info("workspace suggestions load finished device=\(deviceId, privacy: .public) count=\(suggestions.count, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                return
            }
            guard deviceId == selectedDeviceId else { return }
            if workspaces.isEmpty {
                workspaceError = "Recent workspaces unavailable."
            }
            logger.error("workspace suggestions load failed device=\(deviceId, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }
    private func submit() async {
        guard canSubmit, let api = LonghouseAPI(host: appState.serverURL) else { return }
        submitting = true
        submitError = nil
        defer { submitting = false }
        let identityKey = [selectedDeviceId, selectedProvider, normalizedCwd].joined(separator: "\u{1F}")
        let requestSessionID: String
        let requestThreadID: String
        if launchIdentityKey == identityKey {
            requestSessionID = sessionId
            requestThreadID = threadId
        } else {
            requestSessionID = UUID().uuidString
            requestThreadID = UUID().uuidString
            sessionId = requestSessionID
            threadId = requestThreadID
            launchIdentityKey = identityKey
        }
        let trimmedDisplayName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let response = try await api.createConsoleSession(
                deviceId: selectedDeviceId,
                provider: selectedProvider,
                cwd: normalizedCwd,
                displayName: trimmedDisplayName.isEmpty ? nil : trimmedDisplayName,
                sessionId: requestSessionID,
                threadId: requestThreadID
            )
            onLaunchSelection?(
                ConsoleLaunchSelection(
                    sessionId: response.sessionId,
                    deviceId: selectedDeviceId,
                    provider: selectedProvider,
                    cwd: normalizedCwd
                )
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

    /// Online, and blocked only because its providers are signed out: the
    /// launch form stays reachable so the user can sign in from here.
    private static func needsProviderSignIn(_ machine: MachineDirectoryEntry) -> Bool {
        machine.launch.blockedBy == "providers_not_ready"
    }

    /// Re-read machines without the full-screen loading state, so an
    /// in-progress sign-in row stays on screen while the machine confirms.
    private func refreshMachinesQuietly() async {
        guard !usesPreviewData, let api = LonghouseAPI(host: appState.serverURL),
              let result = try? await api.listMachines() else { return }
        machines = result
        if let machine = selectedMachine, !machine.consoleLaunchProviders.contains(selectedProvider) {
            selectedProvider = machine.defaultProvider ?? ""
        }
    }

    private func launchBlockedLabel(_ machine: MachineDirectoryEntry) -> String {
        switch machine.launch.blockedBy {
        case "control_down":
            return lastSeenLabel(machine)
        case "no_launch_support":
            return "Console launch unavailable"
        case "engine_too_old":
            return "Update required"
        case "auth_failed":
            return "Needs repair"
        case "runtime_unreachable":
            return "Needs repair"
        case "providers_not_ready":
            let items = machine.launch.unavailableProviders
            if items.count == 1, let remediation = items[0].remediation { return remediation }
            return "Sign in to \(items.map(\.provider).sorted().joined(separator: " or ")) on this machine"
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
        EmberSectionHeader(title: title, count: nil, size: 20)
    }

    private func providerDisplayName(_ provider: String) -> String {
        ProviderBrands.displayName(provider)
    }

}

private enum LaunchStatusStyle {
    case ready
    case offline
    case warning
    case repair

    var color: Color {
        switch self {
        case .ready: Ember.sage
        case .offline: .secondary
        case .warning: Ember.flame
        case .repair: Ember.ember
        }
    }
}

private struct LaunchCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Ember.hairline, lineWidth: 0.75)
            }
    }
}

/// A provider the machine can drive but cannot run yet. When the engine can
/// relay the provider's own login, "Sign in" runs it on the machine and shows
/// its URL/code here; the credential stays on that machine. While an attempt
/// is open the sheet re-reads machines, so a confirmed sign-in moves the
/// provider into the launchable list and this row disappears.
private struct ProviderSignInRow: View {
    let deviceId: String
    let machineName: String
    let item: MachineLaunchUnavailableProvider
    let displayName: String
    let canRelay: Bool
    let makeAPI: () -> LonghouseAPI?
    let refreshMachines: () async -> Void

    @State private var attempt: ProviderSignInStart?
    /// Previews only: render a row with a sign-in already in progress.
    var previewAttempt: ProviderSignInStart?
    @State private var busy = false
    @State private var errorText: String?
    @State private var code = ""
    @State private var codeSent = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(displayName)
                        .font(.body)
                        .foregroundStyle(Ember.textSecondary)
                    Text(item.remediation ?? (item.reason == "cli_missing" ? "Not installed on this machine" : "Sign in required on this machine"))
                        .font(.subheadline)
                        .foregroundStyle(Ember.textMuted)
                }
                Spacer(minLength: 8)
                if canRelay && attempt == nil {
                    Button(busy ? "Starting…" : "Sign in") { Task { await start() } }
                        .buttonStyle(.bordered)
                        .disabled(busy)
                        .accessibilityIdentifier("launch-signin-\(item.provider)")
                }
            }
            if let attempt {
                if let prerequisite = attempt.prerequisite {
                    Text(prerequisite).font(.footnote).foregroundStyle(Ember.textSecondary)
                }
                if let url = URL(string: attempt.verificationUrl) {
                    Link("Open \(displayName) sign-in", destination: url)
                        .font(.body.weight(.semibold))
                }
                if attempt.flow == "device_code", let userCode = attempt.userCode {
                    HStack(spacing: 10) {
                        Text("Code").foregroundStyle(Ember.textSecondary)
                        Text(userCode)
                            .font(.system(.title3, design: .monospaced))
                            .foregroundStyle(Ember.text)
                            .textSelection(.enabled)
                        Button {
                            UIPasteboard.general.string = userCode
                        } label: {
                            Image(systemName: "doc.on.doc")
                        }
                        .accessibilityLabel("Copy code")
                    }
                }
                if attempt.flow == "paste_code" && !codeSent {
                    HStack(spacing: 8) {
                        TextField("Paste the code shown after signing in", text: $code)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .textFieldStyle(.roundedBorder)
                        Button("Submit") { Task { await sendCode() } }
                            .disabled(busy || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
                Text(codeSent || attempt.flow == "device_code"
                     ? "Waiting for \(machineName) to confirm the sign-in…"
                     : "The code appears on the page after you approve.")
                    .font(.footnote)
                    .foregroundStyle(Ember.textMuted)
                Button("Cancel", role: .cancel) { Task { await cancel() } }
                    .font(.footnote)
            }
            if let errorText {
                Text(errorText).font(.footnote).foregroundStyle(Ember.ember)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.bottom, 10)
        .accessibilityIdentifier("launch-unavailable-provider-\(item.provider)")
        .onAppear { if attempt == nil, let previewAttempt { attempt = previewAttempt } }
        .task(id: attempt?.attemptId) {
            guard attempt != nil else { return }
            // Poll until the machine reports the provider ready (this row then
            // disappears) or the attempt's own lifetime runs out.
            let deadline = Date().addingTimeInterval(TimeInterval(attempt?.expiresInSecs ?? 900))
            while !Task.isCancelled, Date() < deadline {
                try? await Task.sleep(for: .seconds(3))
                await refreshMachines()
            }
        }
    }

    private func start() async {
        guard let api = makeAPI() else { return }
        busy = true
        errorText = nil
        defer { busy = false }
        do {
            attempt = try await api.startProviderSignIn(deviceId: deviceId, provider: item.provider)
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? "Could not start sign-in."
        }
    }

    private func sendCode() async {
        guard let api = makeAPI(), let attempt else { return }
        busy = true
        errorText = nil
        defer { busy = false }
        do {
            try await api.submitProviderSignInCode(
                deviceId: deviceId,
                attemptId: attempt.attemptId,
                code: code.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            codeSent = true
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? "Could not send the code."
        }
    }

    private func cancel() async {
        if let api = makeAPI(), let attempt {
            try? await api.cancelProviderSignIn(deviceId: deviceId, attemptId: attempt.attemptId)
        }
        attempt = nil
        code = ""
        codeSent = false
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
                    .foregroundStyle(Ember.text)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Ember.textSecondary)
                }
            }
            Spacer(minLength: 12)
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Ember.textMuted)
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
                    .foregroundStyle(Ember.ember)
            default:
                Image(systemName: "info.circle.fill")
                    .foregroundStyle(Ember.flame)
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
                .listRowBackground(Ember.card)
            }
            if !ready.isEmpty {
                Section("Available") {
                    ForEach(ready, id: \.deviceId) { machine in
                        Button {
                            onSelect(machine)
                            dismiss()
                        } label: {
                            HStack(spacing: 12) {
                                Circle().fill(Ember.sage).frame(width: 10, height: 10)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(machine.machineName).foregroundStyle(.primary)
                                    Text("Ready").font(.subheadline).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if machine.deviceId == selectedDeviceId {
                                    Image(systemName: "checkmark").fontWeight(.semibold)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 5)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("launch-machine-row-\(machine.deviceId)")
                        .accessibilityLabel("\(machine.machineName), Ready")
                        .accessibilityAddTraits(machine.deviceId == selectedDeviceId ? .isSelected : [])
                    }
                }
                .listRowBackground(Ember.card)
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
                .listRowBackground(Ember.card)
            }
        }
        .emberListGround()
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
                HStack(spacing: 12) {
                    Text(displayName(provider)).foregroundStyle(Ember.text)
                    Spacer(minLength: 12)
                    if provider == selectedProvider { Image(systemName: "checkmark") }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("launch-provider-row-\(provider)")
            .accessibilityAddTraits(provider == selectedProvider ? .isSelected : [])
            .listRowBackground(Ember.card)
        }
        .emberListGround()
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
    @State private var selectionStartedAt: Date?
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "LaunchSession")

    private var filtered: [WorkspaceSuggestion] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return workspaces }
        return workspaces.filter { $0.label.lowercased().contains(query) || $0.path.lowercased().contains(query) }
    }

    private var normalizedManualPath: String { manualPath.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        List {
            if loading {
                ProgressView("Loading recent workspaces…")
                    .listRowBackground(Ember.card)
            }
            if let errorMessage, workspaces.isEmpty {
                Text(errorMessage).foregroundStyle(.secondary)
                    .listRowBackground(Ember.card)
            }
            if !filtered.isEmpty {
                Section("Recent") {
                    ForEach(filtered) { workspace in
                        Button {
                            selectionStartedAt = Date()
                            logger.info("workspace row tapped path=\(workspace.path, privacy: .public) loading=\(loading, privacy: .public) visible_count=\(filtered.count, privacy: .public)")
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
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("launch-workspace-row-\(workspace.path)")
                    }
                }
                .listRowBackground(Ember.card)
            }
            Section("Other") {
                TextField("Absolute path", text: $manualPath)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                Button("Use this path") {
                    onSelect(normalizedManualPath)
                    dismiss()
                }
                .foregroundStyle(normalizedManualPath.starts(with: "/") ? Ember.gold : Ember.textMuted)
                .disabled(!normalizedManualPath.starts(with: "/"))
            }
            .listRowBackground(Ember.card)
        }
        .searchable(text: $search, prompt: "Filter workspaces")
        .emberListGround()
        .navigationTitle("Choose Workspace")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            logger.info("workspace picker appeared count=\(workspaces.count, privacy: .public) loading=\(loading, privacy: .public)")
        }
        .onDisappear {
            if let selectionStartedAt {
                logger.info("workspace picker dismissed after_selection=true elapsed_ms=\(Int(Date().timeIntervalSince(selectionStartedAt) * 1000), privacy: .public)")
            } else {
                logger.info("workspace picker dismissed after_selection=false")
            }
        }
        .task { await monitorMainActorStalls() }
    }

    private func monitorMainActorStalls() async {
        let intervalNanoseconds: UInt64 = 250_000_000
        while !Task.isCancelled {
            let startedAt = Date()
            try? await Task.sleep(nanoseconds: intervalNanoseconds)
            if Task.isCancelled { return }
            let elapsedMs = Int(Date().timeIntervalSince(startedAt) * 1000)
            if elapsedMs >= 750 {
                logger.error("workspace picker main actor stall elapsed_ms=\(elapsedMs, privacy: .public)")
            }
        }
    }
}

/// Persists the launch-picker workspace list so the sheet renders instantly
/// on open, then revalidates from the server. Mirrors ``TimelineCacheStore``
/// but is keyed per (serverURL, identity, deviceId) so switching machines
/// never shows another machine's paths.
enum WorkspaceSuggestionsCacheStore {
    private static let cacheKey = "longhouse.launch.workspaces.cache.v2"
    private static let version = 2
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
    lastSeenAt: String? = nil,
    unavailableProviders: [MachineLaunchUnavailableProvider] = []
) -> MachineDirectoryEntry {
    let launchProviders = online
        ? providers.map { MachineLaunchProviderOption(provider: $0) }
        : []
    return MachineDirectoryEntry(
        deviceId: deviceId,
        machineName: machineName,
        online: online,
        controlChannelStatus: controlChannelStatus,
        supports: ["codex.turn_start", "codex.send", "claude.turn_start"],
        controlOperationsByProvider: ["codex": ["turn_start", "send"], "claude": ["turn_start"]],
        lastSeenAt: lastSeenAt,
        engineBuild: "dev",
        launch: MachineLaunchProjection(
            blockedBy: launchProviders.isEmpty ? (launchBlockedBy ?? (online ? "no_launch_support" : "control_down")) : nil,
            providers: launchProviders,
            defaultProvider: launchProviders.isEmpty ? nil : (providers.contains("codex") ? "codex" : providers.first),
            unavailableProviders: unavailableProviders
        )
    )
}

#Preview("Sign-in relay · device code and paste-back") {
    ScrollView {
        LaunchCard {
            ProviderSignInRow(
                deviceId: "workbench",
                machineName: "workbench",
                item: MachineLaunchUnavailableProvider(provider: "codex", reason: "not_authenticated", remediation: "Sign in to codex on this machine"),
                displayName: "Codex",
                canRelay: true,
                makeAPI: { nil },
                refreshMachines: {},
                previewAttempt: try? JSONDecoder().decode(ProviderSignInStart.self, from: Data("""
                {"attempt_id":"a","provider":"codex","flow":"device_code","verification_url":"https://auth.openai.com/codex/device","user_code":"VWSN-8F9KZ","prerequisite":"Enable device code authorization in ChatGPT > Settings > Security first.","expires_in_secs":900}
                """.utf8))
            )
            ProviderSignInRow(
                deviceId: "workbench",
                machineName: "workbench",
                item: MachineLaunchUnavailableProvider(provider: "claude", reason: "not_authenticated", remediation: "Sign in to claude on this machine"),
                displayName: "Claude",
                canRelay: true,
                makeAPI: { nil },
                refreshMachines: {},
                previewAttempt: try? JSONDecoder().decode(ProviderSignInStart.self, from: Data("""
                {"attempt_id":"b","provider":"claude","flow":"paste_code","verification_url":"https://claude.com/cai/oauth/authorize","user_code":null,"prerequisite":null,"expires_in_secs":900}
                """.utf8))
            )
        }
        .padding(16)
    }
    .background(Ember.page)
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Launch session · sign-in required") {
    LaunchSessionSheet(
        previewMachines: [
            previewMachine(
                deviceId: "workbench",
                machineName: "workbench",
                providers: ["omp"],
                unavailableProviders: [
                    MachineLaunchUnavailableProvider(provider: "claude", reason: "not_authenticated", remediation: "Sign in to claude on this machine"),
                    MachineLaunchUnavailableProvider(provider: "codex", reason: "not_authenticated", remediation: "Sign in to codex on this machine"),
                ]
            ),
        ],
        previewWorkspaces: [WorkspaceSuggestion(path: "/Users/example/git/longhouse", label: "longhouse", score: 100, sessionCount: 3)]
    ) { _ in }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
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
    .emberChrome()
}

#Preview("Launch session without recent workspaces") {
    LaunchSessionSheet(
        previewMachines: [previewMachine(providers: ["codex"])],
        previewWorkspaces: []
    ) { _ in }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
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
    .emberChrome()
}

#if DEBUG
struct LaunchSessionUITestFixtureView: View {
    var body: some View {
        LaunchSessionSheet(
            previewMachines: [previewMachine()],
            previewWorkspaces: [
                WorkspaceSuggestion(path: "/Users/example/git/longhouse", label: "longhouse", score: 100, sessionCount: 20),
                WorkspaceSuggestion(path: "/Users/example/git/g55", label: "g55", score: 90, sessionCount: 12),
            ],
            onLaunched: { _ in }
        )
    }
}
#endif

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
    .emberChrome()
}
