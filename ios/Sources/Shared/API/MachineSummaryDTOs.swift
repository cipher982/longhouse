import Foundation

// MARK: - Machine activity summary

/// The summary endpoint is deliberately decoded independently from the launch
/// directory. Older servers may omit newer optional fields while they are
/// rolling out the endpoint, so absent collections become empty collections and
/// absent values remain nil.
public struct MachinesSummaryResponse: Decodable, Sendable {
    public let generatedAt: String?
    public let days: Int
    public let utcOffsetMinutes: Int
    public let firstDay: String?
    public let lastDay: String?
    public let machines: [MachineSummary]

    public init(
        generatedAt: String? = nil,
        days: Int = 14,
        utcOffsetMinutes: Int = 0,
        firstDay: String? = nil,
        lastDay: String? = nil,
        machines: [MachineSummary] = []
    ) {
        self.generatedAt = generatedAt
        self.days = days
        self.utcOffsetMinutes = utcOffsetMinutes
        self.firstDay = firstDay
        self.lastDay = lastDay
        self.machines = machines
    }

    private enum CodingKeys: String, CodingKey {
        case generatedAt, days, utcOffsetMinutes, firstDay, lastDay, machines
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try container.decodeIfPresent(String.self, forKey: .generatedAt)
        days = try container.decodeIfPresent(Int.self, forKey: .days) ?? 14
        utcOffsetMinutes = try container.decodeIfPresent(Int.self, forKey: .utcOffsetMinutes) ?? 0
        firstDay = try container.decodeIfPresent(String.self, forKey: .firstDay)
        lastDay = try container.decodeIfPresent(String.self, forKey: .lastDay)
        machines = try container.decodeIfPresent([MachineSummary].self, forKey: .machines) ?? []
    }
}

public struct MachineSummary: Decodable, Sendable, Hashable {
    public let machine: MachineDirectoryEntry
    public let activity: MachineActivity
    public let sync: MachineSync?

    public init(
        machine: MachineDirectoryEntry,
        activity: MachineActivity = MachineActivity(),
        sync: MachineSync? = nil
    ) {
        self.machine = machine
        self.activity = activity
        self.sync = sync
    }

    private enum CodingKeys: String, CodingKey { case machine, activity, sync }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        machine = try container.decode(MachineDirectoryEntry.self, forKey: .machine)
        activity = try container.decodeIfPresent(MachineActivity.self, forKey: .activity) ?? MachineActivity()
        sync = try container.decodeIfPresent(MachineSync.self, forKey: .sync)
    }
}

public struct MachineActivity: Decodable, Sendable, Hashable {
    public let sessionsStarted: Int
    public let daily: [MachineDailyActivity]
    public let topProjects: [MachineProjectCount]
    public let latestSession: SessionBrief?
    public let liveCount: Int
    public let liveSessions: [SessionBrief]

    public init(
        sessionsStarted: Int = 0,
        daily: [MachineDailyActivity] = [],
        topProjects: [MachineProjectCount] = [],
        latestSession: SessionBrief? = nil,
        liveCount: Int = 0,
        liveSessions: [SessionBrief] = []
    ) {
        self.sessionsStarted = sessionsStarted
        self.daily = daily
        self.topProjects = topProjects
        self.latestSession = latestSession
        self.liveCount = liveCount
        self.liveSessions = liveSessions
    }

    private enum CodingKeys: String, CodingKey {
        case sessionsStarted, daily, topProjects, latestSession, liveCount, liveSessions
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionsStarted = try container.decodeIfPresent(Int.self, forKey: .sessionsStarted) ?? 0
        daily = try container.decodeIfPresent([MachineDailyActivity].self, forKey: .daily) ?? []
        topProjects = try container.decodeIfPresent([MachineProjectCount].self, forKey: .topProjects) ?? []
        latestSession = try container.decodeIfPresent(SessionBrief.self, forKey: .latestSession)
        liveCount = try container.decodeIfPresent(Int.self, forKey: .liveCount) ?? 0
        liveSessions = try container.decodeIfPresent([SessionBrief].self, forKey: .liveSessions) ?? []
    }
}

public struct MachineDailyActivity: Decodable, Sendable, Hashable {
    public let date: String
    public let total: Int
    public let byProvider: [String: Int]

    public init(date: String, total: Int = 0, byProvider: [String: Int] = [:]) {
        self.date = date
        self.total = total
        self.byProvider = byProvider
    }

    private enum CodingKeys: String, CodingKey { case date, total, byProvider }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        date = try container.decodeIfPresent(String.self, forKey: .date) ?? ""
        total = try container.decodeIfPresent(Int.self, forKey: .total) ?? 0
        byProvider = try container.decodeIfPresent([String: Int].self, forKey: .byProvider) ?? [:]
    }
}

public struct MachineProjectCount: Decodable, Sendable, Hashable {
    public let project: String
    public let sessions: Int

    public init(project: String, sessions: Int = 0) {
        self.project = project
        self.sessions = sessions
    }

    private enum CodingKeys: String, CodingKey { case project, sessions }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        project = try container.decodeIfPresent(String.self, forKey: .project) ?? ""
        sessions = try container.decodeIfPresent(Int.self, forKey: .sessions) ?? 0
    }
}

public struct SessionBrief: Decodable, Sendable, Hashable, Identifiable {
    public let sessionId: String
    public let title: String
    public let project: String?
    public let provider: String?
    public let lastActivityAt: String?
    public let activityState: String?

    public var id: String { sessionId }

    public init(
        sessionId: String,
        title: String,
        project: String? = nil,
        provider: String? = nil,
        lastActivityAt: String? = nil,
        activityState: String? = nil
    ) {
        self.sessionId = sessionId
        self.title = title
        self.project = project
        self.provider = provider
        self.lastActivityAt = lastActivityAt
        self.activityState = activityState
    }

    private enum CodingKeys: String, CodingKey {
        case sessionId, title, project, provider, lastActivityAt, activityState
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try container.decodeIfPresent(String.self, forKey: .sessionId) ?? ""
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? "Untitled session"
        project = try container.decodeIfPresent(String.self, forKey: .project)
        provider = try container.decodeIfPresent(String.self, forKey: .provider)
        lastActivityAt = try container.decodeIfPresent(String.self, forKey: .lastActivityAt)
        activityState = try container.decodeIfPresent(String.self, forKey: .activityState)
    }
}

public struct MachineSync: Decodable, Sendable, Hashable {
    public let reportedAt: String?
    public let reportAgeSeconds: Int?
    public let stale: Bool
    public let status: String
    public let statusSummary: String
    public let engineVersion: String?
    public let lastUploadAt: String?
    public let uploadP95Ms: Int?
    public let waitingUploads: Int?
    public let failedUploads: Int?
    public let history: MachineSyncHistory

    public init(
        reportedAt: String? = nil,
        reportAgeSeconds: Int? = nil,
        stale: Bool = false,
        status: String = "unknown",
        statusSummary: String = "",
        engineVersion: String? = nil,
        lastUploadAt: String? = nil,
        uploadP95Ms: Int? = nil,
        waitingUploads: Int? = nil,
        failedUploads: Int? = nil,
        history: MachineSyncHistory = MachineSyncHistory()
    ) {
        self.reportedAt = reportedAt
        self.reportAgeSeconds = reportAgeSeconds
        self.stale = stale
        self.status = status
        self.statusSummary = statusSummary
        self.engineVersion = engineVersion
        self.lastUploadAt = lastUploadAt
        self.uploadP95Ms = uploadP95Ms
        self.waitingUploads = waitingUploads
        self.failedUploads = failedUploads
        self.history = history
    }

    private enum CodingKeys: String, CodingKey {
        case reportedAt, reportAgeSeconds, stale, status, statusSummary, engineVersion
        case lastUploadAt, uploadP95Ms, waitingUploads, failedUploads, history
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        reportedAt = try container.decodeIfPresent(String.self, forKey: .reportedAt)
        reportAgeSeconds = try container.decodeIfPresent(Int.self, forKey: .reportAgeSeconds)
        stale = try container.decodeIfPresent(Bool.self, forKey: .stale) ?? false
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "unknown"
        statusSummary = try container.decodeIfPresent(String.self, forKey: .statusSummary) ?? ""
        engineVersion = try container.decodeIfPresent(String.self, forKey: .engineVersion)
        lastUploadAt = try container.decodeIfPresent(String.self, forKey: .lastUploadAt)
        uploadP95Ms = try container.decodeIfPresent(Int.self, forKey: .uploadP95Ms)
        waitingUploads = try container.decodeIfPresent(Int.self, forKey: .waitingUploads)
        failedUploads = try container.decodeIfPresent(Int.self, forKey: .failedUploads)
        history = try container.decodeIfPresent(MachineSyncHistory.self, forKey: .history) ?? MachineSyncHistory()
    }
}

public struct MachineSyncHistory: Decodable, Sendable, Hashable {
    public let state: String
    public let sourceCount: Int?
    public let remainingBytes: Int?
    public let remainingRecords: Int?
    public let acknowledgedRecords: Int?

    public init(
        state: String = "unknown",
        sourceCount: Int? = nil,
        remainingBytes: Int? = nil,
        remainingRecords: Int? = nil,
        acknowledgedRecords: Int? = nil
    ) {
        self.state = state
        self.sourceCount = sourceCount
        self.remainingBytes = remainingBytes
        self.remainingRecords = remainingRecords
        self.acknowledgedRecords = acknowledgedRecords
    }

    private enum CodingKeys: String, CodingKey {
        case state, sourceCount, remainingBytes, remainingRecords, acknowledgedRecords
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "unknown"
        sourceCount = try container.decodeIfPresent(Int.self, forKey: .sourceCount)
        remainingBytes = try container.decodeIfPresent(Int.self, forKey: .remainingBytes)
        remainingRecords = try container.decodeIfPresent(Int.self, forKey: .remainingRecords)
        acknowledgedRecords = try container.decodeIfPresent(Int.self, forKey: .acknowledgedRecords)
    }
}
