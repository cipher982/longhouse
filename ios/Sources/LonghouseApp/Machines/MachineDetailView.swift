import SwiftUI

@MainActor
struct MachineDetailView: View {
    @EnvironmentObject private var appState: AppState
    @State private var showingLaunchSheet = false
    @State private var openedSession: SessionRoute?
    @State private var summaryRefreshNotice: String?
    /// A summary is optional because the independently readable directory can
    /// still open this detail while activity and sync are unavailable.
    private let incoming: MachineSummary?
    private let incomingMachine: MachineDirectoryEntry
    private let incomingSummaryDate: Date?
    private let incomingNotice: String?
    private let incomingDirectoryDate: Date?
    /// A fresher copy fetched here (after a provider sign-in), until the list's next update.
    @State private var refreshed: MachineSummary?
    @State private var refreshedAt: Date?
    @State private var refreshedDirectory: MachineDirectoryEntry?

    private var summary: MachineSummary? { refreshed ?? incoming }
    private var machine: MachineDirectoryEntry { refreshedDirectory ?? refreshed?.machine ?? incomingMachine }
    private var activity: MachineActivity? { summary?.activity }
    private var sync: MachineSync? { summary?.sync }

    init(summary: MachineSummary, machine: MachineDirectoryEntry? = nil, summaryNotice: String? = nil, summaryDate: Date? = nil, directoryDate: Date? = nil) {
        incoming = summary
        incomingMachine = machine ?? summary.machine
        incomingSummaryDate = summaryDate
        incomingDirectoryDate = directoryDate
        incomingNotice = summaryNotice
    }

    init(machine: MachineDirectoryEntry, summaryNotice: String? = nil, summaryDate: Date? = nil, directoryDate: Date? = nil) {
        incoming = nil
        incomingMachine = machine
        incomingSummaryDate = summaryDate
        incomingDirectoryDate = directoryDate
        incomingNotice = summaryNotice
    }

    private var status: MachineStatus {
        deriveMachineStatus(machine: machine, activity: activity, sync: sync)
    }

    private var canLaunch: Bool { machine.isLaunchable }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                if let notice = summaryRefreshNotice ?? incomingNotice {
                    machineAvailabilityNotice(notice)
                }
                HStack(spacing: 10) {
                    NavigationLink {
                        TimelineView(initialDeviceId: machine.deviceId)
                    } label: {
                        Label("Open sessions", systemImage: "rectangle.stack")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("machine-open-sessions")
                    if canLaunch {
                        Button("New session here") { showingLaunchSheet = true }
                            .buttonStyle(EmberPrimaryButtonStyle())
                            .accessibilityIdentifier("machine-new-session")
                    }
                }
                activityCard
                if activity?.liveSessions.isEmpty == false {
                    liveNowSection
                }
                agentsSection
                if let sync {
                    syncSection(sync)
                } else {
                    syncUnavailableSection
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
            .padding(.bottom, 28)
        }
        .background { EmberHearthBackground() }
        .navigationTitle(machine.machineName)
        .navigationBarTitleDisplayMode(.inline)
        .onChange(of: incomingSummaryDate) { _, _ in
            refreshed = nil
            refreshedAt = nil
            refreshedDirectory = nil
            summaryRefreshNotice = nil
        }
        .onChange(of: incomingDirectoryDate) { _, _ in
            refreshed = nil
            refreshedAt = nil
            refreshedDirectory = nil
            summaryRefreshNotice = nil
        }
        .task {
            guard appState.isAuthenticated else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard !Task.isCancelled else { return }
                if let updatedAt = refreshedAt ?? incomingSummaryDate,
                   Date().timeIntervalSince(updatedAt) < 15 { continue }
                await refreshSummary()
            }
        }
        .refreshable { await refreshSummary() }
        .navigationDestination(item: $openedSession) { route in
            SessionView(
                sessionId: route.sessionId,
                fallbackTitle: route.fallbackTitle,
                fallbackSubtitle: route.fallbackSubtitle
            )
        }
        .sheet(isPresented: $showingLaunchSheet) {
            LaunchSessionSheet(
                preselectedDeviceId: machine.deviceId,
                onLaunchSelection: nil
            ) { sessionID in
                showingLaunchSheet = false
                openedSession = SessionRoute(
                    sessionId: sessionID,
                    fallbackTitle: "New session",
                    fallbackSubtitle: machine.machineName
                )
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Circle()
                    .fill(status.role.dotColor)
                    .frame(width: 10, height: 10)
                Text(machine.machineName)
                    .font(Ember.serif(30, relativeTo: .largeTitle, bold: true))
                    .foregroundStyle(Ember.text)
            }
            HStack(spacing: 6) {
                Text(status.text)
                    .foregroundStyle(status.role.textColor)
                if let connection = connectionLabel {
                    Text("·")
                        .foregroundStyle(Ember.textMuted)
                    Text(connection)
                        .foregroundStyle(Ember.textSecondary)
                }
            }
            .font(.subheadline)
            if let detail = status.detail, detail != connectionLabel {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(status.role.textColor.opacity(0.9))
            }
        }
    }

    private var connectionLabel: String? {
        guard machine.online else {
            return machineRelativeTime(machine.lastSeenAt).map { "last seen \($0)" } ?? "offline"
        }
        guard let raw = machine.connectedSince,
              let date = LonghouseDateParser.parse(raw) else {
            return "online"
        }
        let minutes = max(0, Int(Date().timeIntervalSince(date) / 60))
        if minutes < 1 {
            return "online now"
        }
        let hours = minutes / 60
        let remainingMinutes = minutes % 60
        if hours > 0 {
            return remainingMinutes > 0
                ? "online \(hours)h \(remainingMinutes)m"
                : "online \(hours)h"
        }
        return "online \(minutes)m"
    }

    @ViewBuilder
    private var activityCard: some View {
        if let activity {
            VStack(alignment: .leading, spacing: 10) {
                machineSectionTitle("Last 14 days · \(activity.sessionsStarted) sessions")
                MachineActivityBars(days: activity.daily)
                    .frame(height: 96)
                    .padding(.horizontal, 14)
                    .padding(.top, 10)
                    .padding(.bottom, 18)
                    .machineSurfaceCard()
            }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                machineSectionTitle("Activity")
                Text("Activity unavailable")
                    .font(.headline)
                    .foregroundStyle(Ember.text)
                Text("The activity summary could not be refreshed. Machine connection and launch readiness are still available.")
                    .font(.subheadline)
                    .foregroundStyle(Ember.textSecondary)
                Button("Retry activity") {
                    Task { await refreshSummary() }
                }
                .font(.subheadline.weight(.medium))
                .accessibilityIdentifier("machine-retry-activity")
            }
            .padding(14)
            .machineSurfaceCard()
        }
    }

    @ViewBuilder
    private var liveNowSection: some View {
        if let activity {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    machineSectionTitle("Live now")
                    Spacer()
                    if activity.liveCount > activity.liveSessions.count {
                        NavigationLink {
                            TimelineView(initialDeviceId: machine.deviceId)
                        } label: {
                            Text("All \(activity.liveCount)")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(Ember.textSecondary)
                        }
                        .accessibilityIdentifier("machine-open-all-live")
                    }
                }
                VStack(spacing: 0) {
                    ForEach(activity.liveSessions) { session in
                        NavigationLink {
                            SessionView(
                                sessionId: session.sessionId,
                                fallbackTitle: session.title,
                                fallbackSubtitle: session.project
                            )
                        } label: {
                            MachineLiveSessionRow(session: session)
                        }
                        .buttonStyle(.plain)
                        if session.id != activity.liveSessions.last?.id {
                            Divider().overlay(Ember.hairline)
                        }
                    }
                }
                .machineSurfaceCard()
            }
        }
    }

    private var agentsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Agents")
            VStack(spacing: 0) {
                ForEach(machine.launch.providers, id: \.provider) { provider in
                    MachineAgentRow(provider: provider.provider, state: "Ready")
                    if provider.provider != machine.launch.providers.last?.provider {
                        Divider().overlay(Ember.hairline)
                    }
                }
                ForEach(machine.launch.unavailableProviders, id: \.provider) { item in
                    if item.reason == "not_authenticated" {
                        ProviderSignInRow(
                            deviceId: machine.deviceId,
                            machineName: machine.machineName,
                            item: item,
                            displayName: ProviderBrands.displayName(item.provider),
                            canRelay: machine.supports.contains("\(item.provider).sign_in"),
                            makeAPI: { LonghouseAPI(host: appState.serverURL) },
                            refreshMachines: { await refreshSummary() }
                        )
                    } else {
                        MachineAgentRow(
                            provider: item.provider,
                            state: item.reason == "cli_missing" ? "Not installed" : "Signed out"
                        )
                    }
                }
                if machine.launch.providers.isEmpty && machine.launch.unavailableProviders.isEmpty {
                    Text("Provider readiness is unavailable.")
                        .font(.subheadline)
                        .foregroundStyle(Ember.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                }
            }
            .machineSurfaceCard()
        }
    }

    private var staleRefreshNotice: String {
        let asOf = (refreshedAt ?? incomingSummaryDate).map {
            $0.formatted(date: .abbreviated, time: .shortened)
        } ?? "earlier"
        return "Could not refresh activity. Showing cached data from \(asOf)."
    }

    private func machineAvailabilityNotice(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(Ember.signalAttention)
            Text(message)
                .font(.footnote)
                .foregroundStyle(Ember.signalAttentionText)
            Spacer(minLength: 0)
            Button("Retry") { Task { await refreshSummary() } }
                .font(.footnote)
        }
        .padding(12)
        .background(Ember.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Ember.hairline, lineWidth: 0.75)
        }
    }

    private func refreshSummary() async {
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            summaryRefreshNotice = "Activity could not be refreshed. Check your Longhouse connection."
            return
        }
        let machineID = machine.deviceId
        do {
            let response = try await api.listMachineSummaries()
            if let updated = response.machines.first(where: { $0.machine.deviceId == machineID }) {
                refreshed = updated
                refreshedAt = response.generatedAt.flatMap { LonghouseAPI.parseServerDate($0) } ?? Date()
                refreshedDirectory = nil
                summaryRefreshNotice = nil
            } else {
                summaryRefreshNotice = incoming == nil
                    ? "Activity is unavailable for this machine."
                    : staleRefreshNotice
            }
        } catch {
            summaryRefreshNotice = summary == nil
                ? "Activity and sync are unavailable."
                : staleRefreshNotice
            do {
                let entries = try await api.listMachines()
                refreshedDirectory = entries.first(where: { $0.deviceId == machineID })
            } catch {
                summaryRefreshNotice = "\(summaryRefreshNotice ?? "") Machine connection information is also last known."
            }
        }
    }

    private func syncSection(_ sync: MachineSync) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Sync")
            VStack(spacing: 0) {
                MachineSyncRow(title: "History", value: sync.historyDisplay)
                Divider().overlay(Ember.hairline)
                MachineSyncRow(title: "Newest upload", value: machineRelativeTime(sync.lastUploadAt) ?? "Never")
                Divider().overlay(Ember.hairline)
                MachineSyncRow(title: "Waiting to upload", value: sync.waitingUploads.map { String($0) } ?? "Unknown")
            }
            .machineSurfaceCard()
            if sync.stale {
                Text("Last report \(machineRelativeTime(sync.reportedAt) ?? "a while ago"); these numbers may be out of date.")
                    .font(.caption)
                    .foregroundStyle(Ember.signalAttentionText)
            }
        }
    }

    private var syncUnavailableSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Sync")
            VStack(alignment: .leading, spacing: 8) {
                Text("Sync unavailable")
                    .font(.headline)
                    .foregroundStyle(Ember.text)
                Text("No sync report is available from the current activity read.")
                    .font(.subheadline)
                    .foregroundStyle(Ember.textSecondary)
            }
            .padding(14)
            .machineSurfaceCard()
        }
    }

    private func machineSectionTitle(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.caption.weight(.semibold))
            .tracking(0.8)
            .foregroundStyle(Ember.textMuted)
    }
}

private struct MachineActivityBars: View {
    let days: [MachineDailyActivity]

    private var maxTotal: Int { max(1, days.map(\.total).max() ?? 1) }

    var body: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(days, id: \.date) { day in
                let height = max(2, CGFloat(day.total) / CGFloat(maxTotal) * 76)
                VStack(spacing: 0) {
                    Spacer(minLength: 0)
                    stackedBar(day: day)
                        .frame(height: height)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
            }
        }
        .overlay(alignment: .bottom) {
            HStack {
                Text(days.first?.date.shortMachineDate ?? "")
                Spacer()
                Text(days.last?.date.shortMachineDate ?? "Today")
            }
            .font(.caption2)
            .foregroundStyle(Ember.textMuted)
            .offset(y: 16)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Session activity over the last 14 days")
    }

    private func stackedBar(day: MachineDailyActivity) -> some View {
        let entries = day.byProvider.sorted { $0.key < $1.key }
        let total = max(1, entries.reduce(0) { $0 + $1.value })
        return GeometryReader { proxy in
            VStack(spacing: 0) {
                ForEach(entries, id: \.key) { entry in
                    Rectangle()
                        .fill(MachineActivityPalette.color(for: entry.key))
                        .frame(height: proxy.size.height * CGFloat(entry.value) / CGFloat(total))
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 2, style: .continuous))
        }
    }
}

private struct MachineLiveSessionRow: View {
    let session: SessionBrief

    var body: some View {
        HStack(spacing: 10) {
            ProviderGlyph(
                provider: session.provider,
                size: 18,
                variant: machineProviderGlyphVariant(for: session.provider)
            )
            VStack(alignment: .leading, spacing: 2) {
                Text(session.title)
                    .foregroundStyle(Ember.text)
                    .lineLimit(1)
                Text(session.project ?? "No project")
                    .font(.caption)
                    .foregroundStyle(Ember.textSecondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            Text(machineRelativeTime(session.lastActivityAt) ?? "now")
                .font(.caption)
                .foregroundStyle(Ember.textMuted)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .contentShape(Rectangle())
    }
}

private struct MachineAgentRow: View {
    let provider: String
    let state: String

    var body: some View {
        HStack(spacing: 10) {
            ProviderGlyph(
                provider: provider,
                size: 18,
                variant: machineProviderGlyphVariant(for: provider)
            )
            Text(ProviderBrands.displayName(provider))
                .foregroundStyle(Ember.text)
            Spacer()
            Text(state)
                .font(.subheadline)
                .foregroundStyle(state == "Ready" ? Ember.signalLiveText : Ember.signalAttentionText)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }
}

private struct MachineSyncRow: View {
    let title: String
    let value: String

    var body: some View {
        HStack {
            Text(title).foregroundStyle(Ember.textSecondary)
            Spacer()
            Text(value)
                .foregroundStyle(Ember.text)
                .multilineTextAlignment(.trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }
}

private extension MachineSync {
    var historyDisplay: String {
        let state = history.state.lowercased()
        let files = history.sourceCount.map { "\($0.formatted()) files" }
        switch state {
        case "current":
            return files.map { "All imported · \($0)" } ?? "All imported"
        case "discovering":
            return "Discovering archive"
        case "inventory_ready":
            return "Inventory ready"
        case "importing":
            return "Importing\(historyProgressSuffix)"
        case "backpressured":
            return "Catching up\(historyProgressSuffix)"
        case "paused":
            return "Paused"
        case "blocked_source":
            return "Blocked on a source file"
        case "offline":
            return "Machine offline"
        case "broken", "error":
            return "Needs repair"
        default:
            return "Not reported"
        }
    }

    private var historyProgressSuffix: String {
        var parts: [String] = []
        if let bytes = history.remainingBytes, bytes > 0 {
            parts.append("\(formatMachineBytes(bytes)) left")
        }
        if let records = history.remainingRecords, records > 0 {
            parts.append("\(records.formatted()) records left")
        }
        return parts.isEmpty ? "" : " · \(parts.joined(separator: " · "))"
    }
}

private func formatMachineBytes(_ bytes: Int) -> String {
    guard bytes > 0 else { return "0 B" }
    let units = ["B", "KB", "MB", "GB", "TB"]
    var value = Double(bytes)
    var unit = 0
    while value >= 1024, unit < units.count - 1 {
        value /= 1024
        unit += 1
    }
    let rendered = value >= 10 || unit == 0
        ? String(format: "%.0f", value)
        : String(format: "%.1f", value)
    return "\(rendered) \(units[unit])"
}

private extension String {
    var shortMachineDate: String {
        let parts = split(separator: "-")
        if parts.count >= 3,
           let month = Int(parts[1]),
           let day = Int(parts[2]),
           (1...12).contains(month) {
            let names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return "\(names[month - 1]) \(day)"
        }
        return self
    }
}

private extension View {
    func machineSurfaceCard() -> some View {
        self
            .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Ember.hairline, lineWidth: 0.75)
            }
    }
}

#Preview("Machine detail · dark") {
    NavigationStack {
        MachineDetailView(summary: MachinePreviewFixtures.cinder)
    }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Machine detail · light") {
    NavigationStack {
        MachineDetailView(summary: MachinePreviewFixtures.cinder)
    }
    .environmentObject(AppState())
    .preferredColorScheme(.light)
    .emberChrome()
}

#Preview("Machine detail · directory only · dark") {
    NavigationStack {
        MachineDetailView(
            machine: MachinePreviewFixtures.cubeBench.machine,
            summaryNotice: "Activity unavailable. Machine connection and launch readiness are still available."
        )
    }
    .environmentObject(AppState())
    .preferredColorScheme(.dark)
    .emberChrome()
}

#Preview("Machine detail · directory only · light") {
    NavigationStack {
        MachineDetailView(
            machine: MachinePreviewFixtures.cubeBench.machine,
            summaryNotice: "Activity unavailable. Machine connection and launch readiness are still available."
        )
    }
    .environmentObject(AppState())
    .preferredColorScheme(.light)
    .emberChrome()
}
