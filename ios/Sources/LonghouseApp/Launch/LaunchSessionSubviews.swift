import OSLog
import SwiftUI

enum LaunchStatusStyle {
    case ready
    case offline
    case warning
    case repair

    var color: Color {
        switch self {
        case .ready: return Ember.signalLive
        case .offline: return Ember.signalQuiet
        case .warning: return Ember.signalAttention
        case .repair: return Ember.signalFault
        }
    }
}

struct LaunchCard<Content: View>: View {
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
struct ProviderSignInRow: View {
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

struct LaunchSummaryRow: View {
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

struct MachineSelectionView: View {
    @Environment(\.dismiss) private var dismiss

    let machines: [MachineDirectoryEntry]
    let selectedDeviceId: String
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
                            MachineRow(
                                machine: machine,
                                activity: nil,
                                sync: nil,
                                showsChevron: false,
                                showsCheckmark: machine.deviceId == selectedDeviceId
                            )
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("launch-machine-row-\(machine.deviceId)")
                        .accessibilityLabel("\(machine.machineName), \(machineStatus(machine: machine).text)")
                        .accessibilityAddTraits(machine.deviceId == selectedDeviceId ? .isSelected : [])
                        .listRowInsets(EdgeInsets())
                    }
                }
                .listRowBackground(Ember.card)
            }

            if !unavailable.isEmpty {
                Section("Unavailable") {
                    ForEach(unavailable, id: \.deviceId) { machine in
                        MachineRow(
                            machine: machine,
                            activity: nil,
                            sync: nil,
                            showsChevron: false
                        )
                        .accessibilityIdentifier("launch-machine-row-\(machine.deviceId)")
                        .accessibilityLabel("\(machine.machineName), \(machineStatus(machine: machine).text), Not available")
                        .listRowInsets(EdgeInsets())
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

struct ProviderSelectionView: View {
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
struct ModelSelectionView: View {
    @Environment(\.dismiss) private var dismiss

    // Bound, not copied: a pushed destination keeps the values it was built
    // with, so opening the picker before the async load finished froze it
    // on an empty list with no spinner.
    @Binding var models: [RecentModel]
    let selectedModel: String?
    @Binding var loading: Bool
    @Binding var errorMessage: String?
    let onSelect: (String?) -> Void

    @State private var manualModel = ""

    private var normalizedManualModel: String? {
        let value = manualModel.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    private func relativeLastUsed(_ value: String?) -> String? {
        guard let value, let date = LonghouseDateParser.parse(value) else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    var body: some View {
        List {
            Button {
                onSelect(nil)
                dismiss()
            } label: {
                HStack {
                    Text("Default").foregroundStyle(Ember.text)
                    Spacer()
                    if selectedModel == nil {
                        Image(systemName: "checkmark")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("launch-model-row-default")
            .accessibilityAddTraits(selectedModel == nil ? .isSelected : [])
            .listRowBackground(Ember.card)

            if loading {
                ProgressView("Loading recent models…")
                    .listRowBackground(Ember.card)
            }
            if let errorMessage, models.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.secondary)
                    .listRowBackground(Ember.card)
            }
            if !models.isEmpty {
                Section("Recent") {
                    ForEach(models) { recent in
                        Button {
                            onSelect(recent.model)
                            dismiss()
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(recent.model)
                                        .foregroundStyle(Ember.text)
                                        .lineLimit(1)
                                    if let lastUsed = relativeLastUsed(recent.lastUsedAt) {
                                        Text("Last used \(lastUsed)")
                                            .font(.caption)
                                            .foregroundStyle(Ember.textMuted)
                                    }
                                }
                                Spacer()
                                if recent.model == selectedModel {
                                    Image(systemName: "checkmark")
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("launch-model-row-\(recent.model)")
                        .accessibilityAddTraits(recent.model == selectedModel ? .isSelected : [])
                    }
                }
                .listRowBackground(Ember.card)
            }

            Section("Other") {
                TextField("Other model id…", text: $manualModel)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                Button("Use this model id") {
                    onSelect(normalizedManualModel)
                    dismiss()
                }
                .foregroundStyle(normalizedManualModel == nil ? Ember.textMuted : Ember.gold)
                .accessibilityIdentifier("launch-model-row-other")
            }
            .listRowBackground(Ember.card)
        }
        .emberListGround()
        .navigationTitle("Choose Model")
        .navigationBarTitleDisplayMode(.inline)
    }
}


struct WorkspaceSelectionView: View {
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
