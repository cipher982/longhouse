import Foundation
import OSLog

// MARK: - Console session launch

public struct MachineLaunchProviderOption: Decodable, Sendable, Hashable {
    public let provider: String
}

/// A provider the machine's engine can drive but cannot run right now
/// (signed out, CLI missing). Never launchable; shown so the user can fix it.
public struct MachineLaunchUnavailableProvider: Decodable, Sendable, Hashable {
    public let provider: String
    public let reason: String
    public let remediation: String?

    public init(provider: String, reason: String, remediation: String?) {
        self.provider = provider
        self.reason = reason
        self.remediation = remediation
    }
}

/// A provider login the Machine Agent started on a machine: open the URL,
/// enter `userCode` (device_code) or paste back the page's code (paste_code).
public struct ProviderSignInStart: Decodable, Sendable, Hashable {
    public let attemptId: String
    public let provider: String
    public let flow: String
    public let verificationUrl: String
    public let userCode: String?
    public let prerequisite: String?
    public let expiresInSecs: Int

    private enum CodingKeys: String, CodingKey {
        case attemptId = "attempt_id"
        case provider, flow, prerequisite
        case verificationUrl = "verification_url"
        case userCode = "user_code"
        case expiresInSecs = "expires_in_secs"
    }
}

public struct MachineLaunchProjection: Decodable, Sendable, Hashable {
    public let blockedBy: String?
    public let providers: [MachineLaunchProviderOption]
    public let defaultProvider: String?
    public let unavailableProviders: [MachineLaunchUnavailableProvider]

    public init(
        blockedBy: String?,
        providers: [MachineLaunchProviderOption],
        defaultProvider: String?,
        unavailableProviders: [MachineLaunchUnavailableProvider] = []
    ) {
        self.blockedBy = blockedBy
        self.providers = providers
        self.defaultProvider = defaultProvider
        self.unavailableProviders = unavailableProviders
    }

    private enum CodingKeys: String, CodingKey {
        case blockedBy, providers, defaultProvider, unavailableProviders
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        blockedBy = try c.decodeIfPresent(String.self, forKey: .blockedBy)
        providers = try c.decode([MachineLaunchProviderOption].self, forKey: .providers)
        defaultProvider = try c.decodeIfPresent(String.self, forKey: .defaultProvider)
        // Absent on Runtime Hosts that predate provider readiness gating.
        unavailableProviders = try c.decodeIfPresent([MachineLaunchUnavailableProvider].self, forKey: .unavailableProviders) ?? []
    }
}

public struct MachineDirectoryEntry: Decodable, Sendable, Hashable {
    public let deviceId: String
    public let machineName: String
    public let online: Bool
    public let controlChannelStatus: String?
    public let supports: [String]
    public let controlOperationsByProvider: [String: [String]]
    public let lastSeenAt: String?
    public let connectedSince: String?
    public let engineBuild: String?
    public let launch: MachineLaunchProjection

    public init(
        deviceId: String,
        machineName: String,
        online: Bool,
        controlChannelStatus: String?,
        supports: [String],
        controlOperationsByProvider: [String: [String]] = [:],
        lastSeenAt: String?,
        connectedSince: String? = nil,
        engineBuild: String?,
        launch: MachineLaunchProjection
    ) {
        self.deviceId = deviceId
        self.machineName = machineName
        self.online = online
        self.controlChannelStatus = controlChannelStatus
        self.supports = supports
        self.controlOperationsByProvider = controlOperationsByProvider
        self.lastSeenAt = lastSeenAt
        self.connectedSince = connectedSince
        self.engineBuild = engineBuild
        self.launch = launch
    }

    private enum CodingKeys: String, CodingKey {
        case deviceId, machineName, online, controlChannelStatus, supports
        case controlOperationsByProvider, lastSeenAt, connectedSince, engineBuild, launch
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        deviceId = try c.decode(String.self, forKey: .deviceId)
        machineName = try c.decode(String.self, forKey: .machineName)
        online = try c.decode(Bool.self, forKey: .online)
        controlChannelStatus = try c.decodeIfPresent(String.self, forKey: .controlChannelStatus)
        supports = try c.decodeIfPresent([String].self, forKey: .supports) ?? []
        controlOperationsByProvider = try c.decodeIfPresent([String: [String]].self, forKey: .controlOperationsByProvider) ?? [:]
        lastSeenAt = try c.decodeIfPresent(String.self, forKey: .lastSeenAt)
        connectedSince = try c.decodeIfPresent(String.self, forKey: .connectedSince)
        engineBuild = try c.decodeIfPresent(String.self, forKey: .engineBuild)
        launch = try c.decode(MachineLaunchProjection.self, forKey: .launch)
    }

    public var consoleLaunchProviders: [String] {
        launch.providers.map(\.provider)
    }

    public var isLaunchable: Bool {
        !launch.providers.isEmpty
    }

    /// Provider to default to (codex for continuity, else the first advertised).
    public var defaultProvider: String? {
        launch.defaultProvider
    }

}

public struct MachineDirectoryResponse: Decodable, Sendable {
    public let machines: [MachineDirectoryEntry]
}

public struct WorkspaceSuggestion: Codable, Sendable, Hashable, Identifiable {
    public let path: String
    public let label: String
    public let gitRepo: String?
    public let gitBranch: String?
    public let score: Double
    public let lastUsedAt: String?
    public let sessionCount: Int

    public var id: String { path }

    public init(
        path: String,
        label: String,
        gitRepo: String? = nil,
        gitBranch: String? = nil,
        score: Double = 0,
        lastUsedAt: String? = nil,
        sessionCount: Int = 0
    ) {
        self.path = path
        self.label = label
        self.gitRepo = gitRepo
        self.gitBranch = gitBranch
        self.score = score
        self.lastUsedAt = lastUsedAt
        self.sessionCount = sessionCount
    }
}

public struct WorkspaceSuggestionsResponse: Decodable, Sendable {
    public let deviceId: String
    public let workspaces: [WorkspaceSuggestion]
}
public struct RecentModel: Codable, Sendable, Hashable, Identifiable {
    public let model: String
    public let lastUsedAt: String?

    public var id: String { model }

    public init(model: String, lastUsedAt: String? = nil) {
        self.model = model
        self.lastUsedAt = lastUsedAt
    }
}

public struct RecentModelsResponse: Decodable, Sendable {
    public let deviceId: String
    public let provider: String
    public let daysBack: Int?
    public let models: [RecentModel]
}


public enum RemoteLaunchState: String, Decodable, Sendable {
    case launching
    case live
    case launchingUnknown = "launching_unknown"
    case launchFailed = "launch_failed"
    case launchOrphaned = "launch_orphaned"
    case unknown

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = RemoteLaunchState(rawValue: value) ?? .unknown
    }
}

public enum RemoteExecutionLifetime: String, Codable, Sendable, Hashable, CaseIterable {
    case oneShot = "one_shot"
    case liveControl = "live_control"
}

public struct RemoteSessionLaunchResponse: Decodable, Sendable {
    public let sessionId: String
    public let launchState: RemoteLaunchState
    public let executionLifetime: RemoteExecutionLifetime?
    public let launchErrorCode: String?
    public let launchErrorMessage: String?
}

public struct SessionBranch: Decodable, Sendable {
    public let sessionId: String
    public let threadId: String
    public let turnId: String
    public let runId: String?
    public let state: String
    public let created: Bool
}

public struct ConsoleSessionCreateResponse: Decodable, Sendable {
    public let sessionId: String
    public let threadId: String
    public let created: Bool
}
