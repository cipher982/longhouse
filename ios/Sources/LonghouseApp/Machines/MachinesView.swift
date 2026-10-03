import SwiftUI

@MainActor
struct MachinesView: View {
    @EnvironmentObject private var appState: AppState
    @State private var response: MachinesSummaryResponse?
    @State private var loading = false
    @State private var errorMessage: String?
    @State private var showingAllRecent = false
    private let previewResponse: MachinesSummaryResponse?

    init(previewResponse: MachinesSummaryResponse? = nil) {
        self.previewResponse = previewResponse
        _response = State(initialValue: previewResponse)
    }

    private var summaries: [MachineSummary] { response?.machines ?? [] }

    private var liveCount: Int {
        summaries.reduce(0) { $0 + $1.activity.liveCount }
    }

    private var summarySubtitle: String {
        if liveCount > 0 {
            let liveMachines = summaries.count(where: { $0.activity.liveCount > 0 })
            let machineWord = liveMachines == 1 ? "machine" : "machines"
            return "\(liveCount) live on \(liveMachines) \(machineWord)"
        }
        let onlineMachines = summaries.count(where: \.machine.online)
        return "\(onlineMachines) of \(summaries.count) machines online"
    }


    private var recent: [MachineSummary] {
        summaries.filter { summary in
            let status = deriveMachineStatus(
                machine: summary.machine,
                activity: summary.activity,
                sync: summary.sync
            )
            return status.text == "Offline"
                && (summary.sync == nil || summary.sync?.stale == true)
                && summary.activity.sessionsStarted == 0
        }
    }

    private var current: [MachineSummary] {
        let recentIDs = Set(recent.map { $0.machine.deviceId })
        return summaries.filter { !recentIDs.contains($0.machine.deviceId) }
    }

    var body: some View {
        Group {
            if loading && response == nil {
                ProgressView("Loading machines…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorMessage, response == nil {
                errorView(errorMessage)
            } else if summaries.isEmpty {
                emptyView
            } else {
                machineList
            }
        }
        .background { EmberHearthBackground() }
        .navigationTitle("Machines")
        .navigationBarTitleDisplayMode(.large)
        .task {
            await load()
            await pollWhileVisible()
        }
        .refreshable { await load() }
    }

    private var machineList: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 2) {
                    Text(summarySubtitle)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Ember.signalLiveText)
                }
                .padding(.horizontal, 4)
                .padding(.vertical, 3)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
            }
            if let errorMessage {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(errorMessage)
                            .font(.footnote)
                            .foregroundStyle(Ember.signalFaultText)
                        Button("Retry") { Task { await load() } }
                            .font(.subheadline.weight(.medium))
                    }
                }
                .listRowBackground(Ember.card)
            }

            if !current.isEmpty {
                Section {
                    ForEach(current, id: \.machine.deviceId) { summary in
                        NavigationLink {
                            MachineDetailView(summary: summary)
                        } label: {
                            MachineRow(
                                machine: summary.machine,
                                activity: summary.activity,
                                sync: summary.sync,
                                showsChevron: false
                            )
                        }
                        .buttonStyle(.plain)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Ember.card)
                    }
                }
                .listRowBackground(Ember.card)
            }
            if !recent.isEmpty {
                Section("Not seen recently") {
                    ForEach(
                        Array(recent.prefix(showingAllRecent ? recent.count : 2)),
                        id: \.machine.deviceId
                    ) { summary in
                        NavigationLink {
                            MachineDetailView(summary: summary)
                        } label: {
                            MachineRow(
                                machine: summary.machine,
                                activity: summary.activity,
                                sync: summary.sync,
                                showsChevron: false
                            )
                        }
                        .buttonStyle(.plain)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Ember.card)
                    }
                    if recent.count > 2 {
                        Button(showingAllRecent ? "Show fewer" : "Show all") {
                            showingAllRecent.toggle()
                        }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Ember.textSecondary)
                        .listRowBackground(Color.clear)
                    }
                }
                .listRowBackground(Ember.card)

                if recent.count > 2 && !showingAllRecent {
                    Text("\(recent.count - 2) more not seen recently")
                        .font(.caption)
                        .foregroundStyle(Ember.textMuted)
                        .listRowBackground(Color.clear)
                }
            }
        }
        .listStyle(.insetGrouped)
        .emberListGround()
    }

    private var emptyView: some View {
        VStack(spacing: 12) {
            Image(systemName: "desktopcomputer")
                .font(.system(size: 34))
                .foregroundStyle(Ember.signalQuiet)
            Text("No machines yet")
                .font(.headline)
            Text("Connect a machine from the web to see it here.")
                .font(.subheadline)
                .foregroundStyle(Ember.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 30))
                .foregroundStyle(Ember.signalFault)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(Ember.textSecondary)
                .padding(.horizontal, 28)
            Button("Retry") { Task { await load() } }
                .buttonStyle(.borderedProminent)
                .tint(Ember.goldFill)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func load() async {
        guard previewResponse == nil else { return }
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Not authenticated."
            return
        }
        loading = true
        defer { loading = false }
        do {
            response = try await api.listMachineSummaries()
            errorMessage = nil
        } catch LonghouseAPIError.unexpectedResponse(let message) {
            errorMessage = message
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Sign in again to load machines."
        } catch {
            errorMessage = "Couldn't load machines. Check your connection and retry."
        }
    }

    private func pollWhileVisible() async {
        guard previewResponse == nil else { return }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled else { return }
            await load()
        }
    }
}

#Preview("Machines · dark") {
    NavigationStack {
        MachinesView(previewResponse: MachinePreviewFixtures.response)
    }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Machines · light") {
    NavigationStack {
        MachinesView(previewResponse: MachinePreviewFixtures.response)
    }
    .environmentObject(AppState())
    .preferredColorScheme(.light)
    .emberChrome()
}
