import Foundation

public enum HeaderSummaryVariant: String, CaseIterable, Sendable {
    case minimal = "minimal"
    case telemetryRail = "telemetry-rail"
    case sessionRibbon = "session-ribbon"

    public static let `default`: HeaderSummaryVariant = .minimal
}
