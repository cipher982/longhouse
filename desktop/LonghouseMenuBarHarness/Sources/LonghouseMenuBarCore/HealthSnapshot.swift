import Foundation
import SwiftUI

public enum HarnessSeverity: String, Codable, CaseIterable {
    case green
    case yellow
    case red
    case gray

    public var accentColor: Color {
        switch self {
        case .green:
            return Color(red: 0.17, green: 0.70, blue: 0.39)
        case .yellow:
            return Color(red: 0.90, green: 0.67, blue: 0.16)
        case .red:
            return Color(red: 0.86, green: 0.29, blue: 0.23)
        case .gray:
            return Color(red: 0.48, green: 0.53, blue: 0.58)
        }
    }

    public var symbolName: String {
        switch self {
        case .green:
            return "checkmark.circle.fill"
        case .yellow:
            return "exclamationmark.triangle.fill"
        case .red:
            return "xmark.circle.fill"
        case .gray:
            return "circle.dotted"
        }
    }

    public var uppercaseLabel: String {
        rawValue.uppercased()
    }
}

public struct UpdateInfoSnapshot: Codable, Equatable {
    public let installedVersion: String
    public let latestVersion: String?
    public let updateAvailable: Bool
    public let upgradeCommand: String
    public let checkedAt: String?
}

public struct HealthSnapshot: Codable, Equatable {
    public let schemaVersion: Int?
    public let collectedAt: String?
    public let healthState: String
    public let severity: String
    public let headline: String
    public let reasons: [String]
    public let suggestedActions: [String]
    public let service: ServiceSnapshot?
    public let engineStatus: EngineStatusSnapshot?
    public let outbox: OutboxSnapshot?
    public let launchReadiness: LaunchReadinessSnapshot?
    public let updateInfo: UpdateInfoSnapshot?

    public init(
        schemaVersion: Int?,
        collectedAt: String?,
        healthState: String,
        severity: String,
        headline: String,
        reasons: [String],
        suggestedActions: [String],
        service: ServiceSnapshot?,
        engineStatus: EngineStatusSnapshot?,
        outbox: OutboxSnapshot?,
        launchReadiness: LaunchReadinessSnapshot?,
        updateInfo: UpdateInfoSnapshot? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.collectedAt = collectedAt
        self.healthState = healthState
        self.severity = severity
        self.headline = headline
        self.reasons = reasons
        self.suggestedActions = suggestedActions
        self.service = service
        self.engineStatus = engineStatus
        self.outbox = outbox
        self.launchReadiness = launchReadiness
        self.updateInfo = updateInfo
    }

    public var parsedSeverity: HarnessSeverity {
        HarnessSeverity(rawValue: severity) ?? .gray
    }

    public var statusBadge: String {
        "\(parsedSeverity.uppercaseLabel) · \(healthState.replacingOccurrences(of: "_", with: " ").uppercased())"
    }

    public var lastShipLabel: String {
        engineStatus?.payload?.lastShipAt ?? "No shipments yet"
    }

    public var serviceStatusLabel: String {
        service?.status?.replacingOccurrences(of: "-", with: " ") ?? "unknown"
    }

    public var outboxCount: Int {
        outbox?.fileCount ?? 0
    }

    public var outboxOldestLabel: String {
        if let seconds = outbox?.oldestAgeSeconds {
            return Self.ageLabel(seconds: seconds)
        }
        return "-"
    }

    public var engineAgeLabel: String {
        if let seconds = engineStatus?.ageSeconds {
            return Self.ageLabel(seconds: seconds)
        }
        return "-"
    }

    public var spoolPendingLabel: String {
        String(engineStatus?.payload?.spoolPendingCount ?? 0)
    }

    public var spoolDeadLabel: String {
        String(engineStatus?.payload?.spoolDeadCount ?? 0)
    }

    public var launchStateLabel: String {
        launchReadiness?.state ?? "-"
    }

    public var machineRunnerLabel: String {
        let machineName = launchReadiness?.machineName ?? "-"
        let runnerName = launchReadiness?.runner?.runnerName ?? "-"
        return "\(machineName) / \(runnerName)"
    }

    public var serviceMachineLabel: String {
        launchReadiness?.serviceMachineName ?? "-"
    }

    public var storedRunnerURLLabel: String {
        let storedURL = launchReadiness?.storedURL ?? "-"
        let runnerURL = launchReadiness?.runner?.runnerURLs?.joined(separator: ", ") ?? "-"
        return "\(storedURL) / \(runnerURL)"
    }

    private static func ageLabel(seconds: Int) -> String {
        if seconds < 60 {
            return "\(seconds)s"
        }
        if seconds < 3600 {
            return "\(seconds / 60)m"
        }
        return "\(seconds / 3600)h"
    }
}

public struct ServiceSnapshot: Codable, Equatable {
    public let platform: String?
    public let status: String?
    public let serviceName: String?
    public let serviceFile: String?
    public let logPath: String?
}

public struct EngineStatusSnapshot: Codable, Equatable {
    public let path: String?
    public let exists: Bool?
    public let fresh: Bool?
    public let ageSeconds: Int?
    public let payload: EngineStatusPayload?
    public let error: String?
}

public struct EngineStatusPayload: Codable, Equatable {
    public let version: String?
    public let daemonPid: Int?
    public let lastShipAt: String?
    public let spoolPendingCount: Int?
    public let spoolDeadCount: Int?
    public let parseErrorCount1H: Int?
    public let consecutiveShipFailures: Int?
    public let diskFreeBytes: UInt64?
    public let isOffline: Bool?
    public let recentDeadLetters: [DeadLetterSnapshot]?
    public let lastUpdated: String?

    enum CodingKeys: String, CodingKey {
        case version
        case daemonPid
        case lastShipAt
        case spoolPendingCount
        case spoolDeadCount
        case parseErrorCount1H = "parseErrorCount1h"
        case consecutiveShipFailures
        case diskFreeBytes
        case isOffline
        case recentDeadLetters
        case lastUpdated
    }
}

public struct DeadLetterSnapshot: Codable, Equatable {
    public let provider: String?
    public let filePath: String?
    public let rangeBytes: Int?
    public let createdAt: String?
}

public struct OutboxSnapshot: Codable, Equatable {
    public let path: String?
    public let fileCount: Int?
    public let oldestAgeSeconds: Int?
}

public struct LaunchReadinessSnapshot: Codable, Equatable {
    public let state: String?
    public let headline: String?
    public let reasons: [String]?
    public let suggestedActions: [String]?
    public let storedURL: String?
    public let machineName: String?
    public let serviceMachineName: String?
    public let runner: RunnerSnapshot?

    enum CodingKeys: String, CodingKey {
        case state
        case headline
        case reasons
        case suggestedActions
        case storedURL = "storedUrl"
        case machineName
        case serviceMachineName
        case runner
    }
}

public struct RunnerSnapshot: Codable, Equatable {
    public let path: String?
    public let exists: Bool?
    public let error: String?
    public let runnerName: String?
    public let runnerID: String?
    public let runnerURLs: [String]?
    public let installMode: String?

    enum CodingKeys: String, CodingKey {
        case path
        case exists
        case error
        case runnerName
        case runnerID = "runnerId"
        case runnerURLs = "runnerUrls"
        case installMode
    }
}
