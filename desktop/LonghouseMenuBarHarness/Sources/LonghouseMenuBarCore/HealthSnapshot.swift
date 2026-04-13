import Foundation
import SwiftUI

public enum HarnessSeverity: String, Codable, CaseIterable, Sendable {
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

public struct UpdateInfoSnapshot: Codable, Equatable, Sendable {
    public let installedVersion: String
    public let latestVersion: String?
    public let updateAvailable: Bool
    public let upgradeCommand: String
    public let checkedAt: String?
}

public struct HealthSnapshot: Codable, Equatable, Sendable {
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

    public var ambientStatusLabel: String {
        switch parsedSeverity {
        case .green:
            return "Healthy"
        case .yellow:
            return "Watching"
        case .red:
            return "Needs repair"
        case .gray:
            return "Unknown"
        }
    }

    public var lastShipLabel: String {
        engineStatus?.payload?.lastShipAt ?? "No shipments yet"
    }

    public var lastShipSummaryLabel: String {
        guard let raw = engineStatus?.payload?.lastShipAt else {
            return "No shipments yet"
        }
        guard let parsed = Self.parseISO8601(raw) else {
            return "Last ship \(raw)"
        }
        return "Last ship \(Self.relativeLabel(for: parsed))"
    }

    public var serviceStatusLabel: String {
        service?.status?.replacingOccurrences(of: "-", with: " ") ?? "unknown"
    }

    public var serviceStatusTitle: String {
        serviceStatusLabel.capitalized
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

    public var launchSummaryLabel: String {
        let state = launchStateLabel
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if state.lowercased() == "ready",
           let machineName = launchReadiness?.machineName,
           !machineName.isEmpty {
            return "Launch ready on \(machineName)"
        }
        if state.isEmpty || state == "-" {
            return "Launch state unavailable"
        }
        return "Launch \(state)"
    }

    public var attentionSummaryLabel: String {
        let primaryReason = reasons.first.map(Self.humanizeReason)
        switch parsedSeverity {
        case .green:
            return "Shipping is healthy on this Mac. Leave Longhouse running quietly in the menu bar."
        case .yellow:
            if let primaryReason {
                return "\(primaryReason). Refresh or inspect logs if this keeps aging."
            }
            return "Longhouse is still shipping, but local status is aging."
        case .red:
            if let primaryReason {
                return "\(primaryReason). Repair is the fastest path to restore shipping."
            }
            return "Shipping is blocked on this Mac. Repair is the fastest path to restore it."
        case .gray:
            return "Longhouse could not determine the current local health state."
        }
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

    private static func parseISO8601(_ raw: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = fractional.date(from: raw) {
            return parsed
        }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: raw)
    }

    private static func relativeLabel(for date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private static func humanizeReason(_ raw: String) -> String {
        switch raw {
        case "service_stopped":
            return "The local service is stopped"
        case "spool_dead":
            return "Dead letters need attention"
        case "outbox_stuck":
            return "The outbox is backing up"
        case "engine_status_stale":
            return "The engine status is stale"
        default:
            return raw
                .replacingOccurrences(of: "_", with: " ")
                .replacingOccurrences(of: "-", with: " ")
                .capitalized
        }
    }
}

public struct ServiceSnapshot: Codable, Equatable, Sendable {
    public let platform: String?
    public let status: String?
    public let serviceName: String?
    public let serviceFile: String?
    public let logPath: String?
}

public struct EngineStatusSnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let exists: Bool?
    public let fresh: Bool?
    public let ageSeconds: Int?
    public let payload: EngineStatusPayload?
    public let error: String?
}

public struct EngineStatusPayload: Codable, Equatable, Sendable {
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

public struct DeadLetterSnapshot: Codable, Equatable, Sendable {
    public let provider: String?
    public let filePath: String?
    public let rangeBytes: Int?
    public let createdAt: String?
}

public struct OutboxSnapshot: Codable, Equatable, Sendable {
    public let path: String?
    public let fileCount: Int?
    public let oldestAgeSeconds: Int?
}

public struct LaunchReadinessSnapshot: Codable, Equatable, Sendable {
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

public struct RunnerSnapshot: Codable, Equatable, Sendable {
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
