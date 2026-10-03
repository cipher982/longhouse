import SwiftUI

@MainActor
struct MachineDetailView: View {
    @EnvironmentObject private var appState: AppState
    @State private var showingLaunchSheet = false
    @State private var openedSession: SessionRoute?
    let summary: MachineSummary

    private var status: MachineStatus {
        deriveMachineStatus(machine: summary.machine, activity: summary.activity, sync: summary.sync)
    }

    private var canLaunch: Bool { summary.machine.isLaunchable }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                if canLaunch {
                    Button("New session here") { showingLaunchSheet = true }
                        .buttonStyle(EmberPrimaryButtonStyle())
                        .accessibilityIdentifier("machine-new-session")
                }
                activityCard
                if !summary.activity.liveSessions.isEmpty {
                    liveNowSection
                }
                agentsSection
                if let sync = summary.sync {
                    syncSection(sync)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
            .padding(.bottom, 28)
        }
        .background { EmberHearthBackground() }
        .navigationTitle(summary.machine.machineName)
        .navigationBarTitleDisplayMode(.inline)
        .navigationDestination(item: $openedSession) { route in
            SessionView(
                sessionId: route.sessionId,
                fallbackTitle: route.fallbackTitle,
                fallbackSubtitle: route.fallbackSubtitle
            )
        }
        .sheet(isPresented: $showingLaunchSheet) {
            LaunchSessionSheet(
                preselectedDeviceId: summary.machine.deviceId,
                onLaunchSelection: nil
            ) { sessionID in
                showingLaunchSheet = false
                openedSession = SessionRoute(
                    sessionId: sessionID,
                    fallbackTitle: "New session",
                    fallbackSubtitle: summary.machine.machineName
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
                Text(summary.machine.machineName)
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
            if summary.machine.online, let detail = status.detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(status.role.textColor.opacity(0.9))
            }
        }
    }

    private var connectionLabel: String? {
        if summary.machine.online {
            guard let raw = summary.machine.lastSeenAt,
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
        return status.detail
    }

    private var activityCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Last 14 days · \(summary.activity.sessionsStarted) sessions")
            MachineActivityBars(days: summary.activity.daily)
                .frame(height: 96)
                .padding(.horizontal, 14)
                .padding(.top, 10)
                .padding(.bottom, 18)
                .machineSurfaceCard()
        }
    }

    private var liveNowSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Live now")
            VStack(spacing: 0) {
                ForEach(summary.activity.liveSessions) { session in
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
                    if session.id != summary.activity.liveSessions.last?.id {
                        Divider().overlay(Ember.hairline)
                    }
                }
            }
            .machineSurfaceCard()
        }
    }

    private var agentsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            machineSectionTitle("Agents")
            VStack(spacing: 0) {
                ForEach(summary.machine.launch.providers, id: \.provider) { provider in
                    MachineAgentRow(provider: provider.provider, state: "Ready")
                    if provider.provider != summary.machine.launch.providers.last?.provider {
                        Divider().overlay(Ember.hairline)
                    }
                }
                ForEach(summary.machine.launch.unavailableProviders, id: \.provider) { item in
                    if item.reason == "not_authenticated" {
                        ProviderSignInRow(
                            deviceId: summary.machine.deviceId,
                            machineName: summary.machine.machineName,
                            item: item,
                            displayName: ProviderBrands.displayName(item.provider),
                            canRelay: summary.machine.supports.contains("\(item.provider).sign_in"),
                            makeAPI: { LonghouseAPI(host: appState.serverURL) },
                            refreshMachines: { }
                        )
                    } else {
                        MachineAgentRow(
                            provider: item.provider,
                            state: item.reason == "cli_missing" ? "Not installed" : "Signed out"
                        )
                    }
                }
            }
            .machineSurfaceCard()
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
        switch history.state.lowercased() {
        case "complete", "completed", "healthy", "imported": return "All imported"
        case "syncing", "running", "in_progress": return "Importing"
        case "degraded", "waiting": return "Catching up"
        case "broken", "error": return "Needs repair"
        default: return history.state.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
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
