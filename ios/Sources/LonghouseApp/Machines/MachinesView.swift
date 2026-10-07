import SwiftUI

@MainActor
struct MachinesView: View {
    @EnvironmentObject private var appState: AppState
    @State private var response: MachinesSummaryResponse?
    @State private var directory: [MachineDirectoryEntry] = []
    @State private var directoryLoaded = false
    @State private var loading = false
    @State private var summaryErrorMessage: String?
    @State private var directoryErrorMessage: String?
    @State private var summaryLastUpdated: Date?
    @State private var directoryLastUpdated: Date?
    @State private var showingAllRecent = false
    private let previewResponse: MachinesSummaryResponse?
    private let previewDirectory: [MachineDirectoryEntry]?
    private let previewSummaryUnavailable: Bool

    init(
        previewResponse: MachinesSummaryResponse? = nil,
        previewDirectory: [MachineDirectoryEntry]? = nil,
        previewSummaryUnavailable: Bool = false
    ) {
        self.previewResponse = previewResponse
        self.previewDirectory = previewDirectory
        self.previewSummaryUnavailable = previewSummaryUnavailable
        _response = State(initialValue: previewResponse)
        _directory = State(initialValue: previewDirectory ?? [])
        _directoryLoaded = State(initialValue: previewDirectory != nil)
        _summaryErrorMessage = State(
            initialValue: previewSummaryUnavailable ? "Activity is unavailable." : nil
        )
    }

    private var summaries: [MachineSummary] { response?.machines ?? [] }


    /// Merge the independently readable directory with the optional activity
    /// snapshot. A directory-only item intentionally keeps its summary nil:
    /// constructing ``MachineActivity()`` here would turn an unavailable read
    /// into a false "zero sessions" observation.
    private var machineItems: [MachineListItem] {
        let summaryByID = Dictionary(uniqueKeysWithValues: summaries.lazy.map { ($0.machine.deviceId, $0) })
        var seen = Set<String>()
        var result: [MachineListItem] = []
        for machine in directory {
            guard seen.insert(machine.deviceId).inserted else { continue }
            result.append(MachineListItem(machine: machine, summary: summaryByID[machine.deviceId]))
        }
        for summary in summaries {
            guard seen.insert(summary.machine.deviceId).inserted else { continue }
            result.append(MachineListItem(machine: summary.machine, summary: summary))
        }
        return result
    }

    private var liveCount: Int {
        machineItems.reduce(0) { $0 + ($1.summary?.activity.liveCount ?? 0) }
    }

    private var summarySubtitle: String {
        if liveCount > 0 {
            let liveMachines = machineItems.count(where: { ($0.summary?.activity.liveCount ?? 0) > 0 })
            let machineWord = liveMachines == 1 ? "machine" : "machines"
            return "\(liveCount) live on \(liveMachines) \(machineWord)"
        }
        let onlineMachines = machineItems.count(where: \.machine.online)
        return "\(onlineMachines) of \(machineItems.count) machines online"
    }

    private var recent: [MachineListItem] {
        machineItems.filter { item in
            // The server folds an offline machine that started nothing.
            item.summary?.status?.quiet == true
        }
    }

    private var current: [MachineListItem] {
        let recentIDs = Set(recent.map(\.id))
        return machineItems.filter { !recentIDs.contains($0.id) }
    }

    private var cachedSummaryDate: Date? {
        if let generatedAt = response?.generatedAt,
           let date = LonghouseAPI.parseServerDate(generatedAt) {
            return date
        }
        return summaryLastUpdated
    }

    private var activityNotice: String? {
        guard summaryErrorMessage != nil else { return nil }
        if response != nil {
            let asOf = cachedSummaryDate.map {
                $0.formatted(date: .abbreviated, time: .shortened)
            } ?? "earlier"
            let directorySuffix = directoryErrorMessage == nil
                ? ""
                : " Machine connection details may also be stale."
            return "Activity unavailable. Showing cached activity from \(asOf).\(directorySuffix)"
        }
        return directoryErrorMessage == nil
            ? "Activity unavailable. Machine connection and launch readiness are still available."
            : "Activity unavailable. Machine connection and launch information is also last known."
    }

    private var machineDataNotice: String? {
        if let activityNotice { return activityNotice }
        guard directoryErrorMessage != nil else { return nil }
        let asOf = directoryLastUpdated.map {
            $0.formatted(date: .abbreviated, time: .shortened)
        } ?? "earlier"
        return "Could not refresh machine connections. Showing last-known connection details from \(asOf)."
    }
    private var loadErrorMessage: String? {
        guard machineItems.isEmpty else { return nil }
        if let summaryErrorMessage, !directoryLoaded {
            return summaryErrorMessage
        }
        if let directoryErrorMessage {
            return directoryErrorMessage
        }
        return nil
    }

    var body: some View {
        Group {
            if loading && machineItems.isEmpty {
                ProgressView("Loading machines…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadErrorMessage {
                errorView(loadErrorMessage)
            } else if machineItems.isEmpty {
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
                    if summaryErrorMessage != nil {
                        Text("Activity and sync unavailable")
                            .font(.caption)
                            .foregroundStyle(Ember.signalAttentionText)
                    }
                }
                .padding(.horizontal, 4)
                .padding(.vertical, 3)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
            }
            if let machineDataNotice {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(machineDataNotice)
                            .font(.footnote)
                            .foregroundStyle(Ember.signalAttentionText)
                        Button("Retry") { Task { await load() } }
                            .font(.subheadline.weight(.medium))
                    }
                }
                .listRowBackground(Ember.card)
            }

            if !current.isEmpty {
                Section {
                    ForEach(current) { item in
                        NavigationLink {
                            machineDetail(for: item)
                        } label: {
                            MachineRow(
                                machine: item.machine,
                                activity: item.summary?.activity,
                                sync: item.summary?.sync,
                                summaryStatus: item.summary?.status,
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
                        Array(recent.prefix(showingAllRecent ? recent.count : 2))
                    ) { item in
                        NavigationLink {
                            machineDetail(for: item)
                        } label: {
                            MachineRow(
                                machine: item.machine,
                                activity: item.summary?.activity,
                                sync: item.summary?.sync,
                                summaryStatus: item.summary?.status,
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

    private func machineDetail(for item: MachineListItem) -> MachineDetailView {
        let notice = machineDataNotice
        if let summary = item.summary {
            return MachineDetailView(
                summary: summary,
                machine: item.machine,
                summaryNotice: notice,
                summaryDate: cachedSummaryDate,
                directoryDate: directoryLastUpdated
            )
        }
        return MachineDetailView(
            machine: item.machine,
            summaryNotice: notice,
            directoryDate: directoryLastUpdated
        )
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
            if let machineDataNotice {
                Text(machineDataNotice)
                    .font(.caption)
                    .foregroundStyle(Ember.signalAttentionText)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
                Button("Retry") { Task { await load() } }
                    .buttonStyle(.bordered)
            }
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

    private var isPreview: Bool {
        previewResponse != nil || previewDirectory != nil || previewSummaryUnavailable
    }

    private func errorMessage(for error: Error) -> String {
        if let apiError = error as? LonghouseAPIError {
            switch apiError {
            case .unexpectedResponse(let message):
                return message
            case .notAuthenticated:
                return "Sign in again to load machines."
            default:
                break
            }
        }
        return "Couldn't load machines. Check your connection and retry."
    }

    private func load() async {
        guard !isPreview else { return }
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            summaryErrorMessage = "Not authenticated."
            directoryErrorMessage = "Not authenticated."
            return
        }
        loading = true
        defer { loading = false }

        async let directoryRequest = api.listMachines()
        async let summaryRequest = api.listMachineSummaries()

        do {
            directory = try await directoryRequest
            directoryLoaded = true
            directoryLastUpdated = Date()
            directoryErrorMessage = nil
        } catch {
            directoryErrorMessage = errorMessage(for: error)
        }

        do {
            response = try await summaryRequest
            summaryLastUpdated = Date()
            summaryErrorMessage = nil
        } catch {
            summaryErrorMessage = errorMessage(for: error)
        }
    }

    private func pollWhileVisible() async {
        guard !isPreview else { return }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled else { return }
            await load()
        }
    }
}

private struct MachineListItem: Identifiable {
    let machine: MachineDirectoryEntry
    let summary: MachineSummary?

    var id: String { machine.deviceId }
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

#Preview("Machines · directory only · dark") {
    NavigationStack {
        MachinesView(
            previewDirectory: MachinePreviewFixtures.directoryMachines,
            previewSummaryUnavailable: true
        )
    }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Machines · directory only · light") {
    NavigationStack {
        MachinesView(
            previewDirectory: MachinePreviewFixtures.directoryMachines,
            previewSummaryUnavailable: true
        )
    }
    .environmentObject(AppState())
    .preferredColorScheme(.light)
    .emberChrome()
}
