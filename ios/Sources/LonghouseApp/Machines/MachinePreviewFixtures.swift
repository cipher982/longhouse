import Foundation

/// Preview-only data mirrors the summary contract while never touching the
/// network. Keep this small enough to exercise the live, attention, idle,
/// sync-only, and quiet/offline rows in one rendered frame.
enum MachinePreviewFixtures {
    static let response = MachinesSummaryResponse(
        generatedAt: "2026-10-03T16:40:00Z",
        days: 14,
        utcOffsetMinutes: -300,
        firstDay: "2026-09-20",
        lastDay: "2026-10-03",
        machines: [cinder, cubeBench, cube, clifford, quietOne, quietTwo]
    )

    static let cinder = MachineSummary(
        machine: directory(
            id: "cinder",
            name: "cinder",
            online: true,
            connectedSince: "2026-10-03T04:08:00Z",
            supports: ["omp.sign_in"],
            providers: ["omp", "claude", "cursor", "codex"]
        ),
        activity: MachineActivity(
            sessionsStarted: 254,
            daily: dailyCounts,
            topProjects: [MachineProjectCount(project: "zerg", sessions: 78)],
            latestSession: SessionBrief(
                sessionId: "cinder-latest",
                title: "Fix chat scroll capture on iOS",
                project: "zerg",
                provider: "omp",
                lastActivityAt: "2026-10-03T16:39:00Z",
                activityState: "working"
            ),
            liveCount: 9,
            liveSessions: [
                SessionBrief(sessionId: "cinder-1", title: "Broken signup page deep dive", project: "zerg", provider: "omp", lastActivityAt: "2026-10-03T16:39:00Z", activityState: "working"),
                SessionBrief(sessionId: "cinder-2", title: "UI UX redesign for runners…", project: "zerg", provider: "omp", lastActivityAt: "2026-10-03T16:38:00Z", activityState: "working"),
                SessionBrief(sessionId: "cinder-3", title: "Fix chat scroll capture on iOS", project: "zerg", provider: "omp", lastActivityAt: "2026-10-03T16:37:00Z", activityState: "working"),
            ]
        ),
        sync: MachineSync(
            reportedAt: "2026-10-03T16:40:00Z",
            reportAgeSeconds: 8,
            stale: false,
            status: "healthy",
            statusSummary: "All imported",
            lastUploadAt: "2026-10-03T16:39:52Z",
            waitingUploads: 0,
            history: MachineSyncHistory(state: "current", sourceCount: 3)
        )
    )

    static let cubeBench = MachineSummary(
        machine: directory(
            id: "cube-bench",
            name: "cube-bench",
            online: true,
            supports: ["codex.sign_in"],
            providers: [],
            unavailable: [MachineLaunchUnavailableProvider(provider: "codex", reason: "not_authenticated", remediation: nil)],
            blockedBy: "providers_not_ready"
        ),
        activity: MachineActivity(sessionsStarted: 12, daily: dailyCounts.map { MachineDailyActivity(date: $0.date, total: $0.total / 4, byProvider: $0.byProvider) }),
        sync: MachineSync(status: "healthy", history: MachineSyncHistory(state: "current"))
    )

    static let cube = MachineSummary(
        machine: directory(id: "cube", name: "cube", online: true, providers: ["omp"]),
        activity: MachineActivity(sessionsStarted: 18, daily: dailyCounts.map { MachineDailyActivity(date: $0.date, total: $0.total / 6, byProvider: $0.byProvider) }),
        sync: MachineSync(status: "healthy", history: MachineSyncHistory(state: "current"))
    )

    static let clifford = MachineSummary(
        machine: directory(id: "clifford-sauron", name: "clifford-sauron", online: false, lastSeenAt: "2026-10-01T16:40:00Z", blockedBy: "control_down"),
        activity: MachineActivity(sessionsStarted: 3, daily: dailyCounts.map { MachineDailyActivity(date: $0.date, total: $0.total / 10, byProvider: $0.byProvider) }),
        sync: MachineSync(stale: false, status: "offline", history: MachineSyncHistory(state: "current"))
    )

    static let quietOne = MachineSummary(
        machine: directory(id: "drose-web-pepper", name: "drose-web-pepper", online: false, lastSeenAt: "2026-09-25T16:40:00Z", blockedBy: "control_down"),
        activity: MachineActivity(),
        sync: nil
    )

    static let quietTwo = MachineSummary(
        machine: directory(id: "sauron-clifford", name: "sauron-clifford", online: false, lastSeenAt: "2026-09-24T16:40:00Z", blockedBy: "control_down"),
        activity: MachineActivity(),
        sync: nil
    )

    static let directoryMachines: [MachineDirectoryEntry] = response.machines.map(\.machine)

    private static let dailyCounts: [MachineDailyActivity] = (0..<14).map { index in
        let total = [45, 28, 26, 22, 17, 20, 12, 17, 25, 30, 21, 14, 9, 17][index]
        return MachineDailyActivity(
            date: "2026-\(String(format: "%02d", index < 11 ? 9 : 10))-\(String(format: "%02d", index < 11 ? 20 + index : index - 10))",
            total: total,
            byProvider: ["omp": total * 7 / 10, "claude": total * 2 / 10, "cursor": total / 10]
        )
    }

    private static func directory(
        id: String,
        name: String,
        online: Bool,
        lastSeenAt: String? = "2026-10-03T16:40:00Z",
        connectedSince: String? = nil,
        supports: [String] = [],
        providers: [String] = [],
        unavailable: [MachineLaunchUnavailableProvider] = [],
        blockedBy: String? = nil
    ) -> MachineDirectoryEntry {
        MachineDirectoryEntry(
            deviceId: id,
            machineName: name,
            online: online,
            controlChannelStatus: online ? "connected" : "down",
            supports: supports,
            lastSeenAt: lastSeenAt,
            connectedSince: connectedSince,
            engineBuild: nil,
            launch: MachineLaunchProjection(
                blockedBy: blockedBy,
                providers: providers.map { MachineLaunchProviderOption(provider: $0) },
                defaultProvider: providers.first,
                unavailableProviders: unavailable
            )
        )
    }
}
