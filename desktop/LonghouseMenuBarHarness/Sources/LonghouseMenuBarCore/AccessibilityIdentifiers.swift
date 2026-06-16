import Foundation

public enum LonghouseMenuBarAccessibilityID {
    public static let panel = "LonghouseMenuBar.Panel"

    public enum Error {
        public static let headline = "LonghouseMenuBar.Error.Headline"
        public static let message = "LonghouseMenuBar.Error.Message"
        public static let retryButton = "LonghouseMenuBar.Error.RetryButton"
    }

    public enum Header {
        public static let statusGlyph = "LonghouseMenuBar.Header.StatusGlyph"
        public static let headline = "LonghouseMenuBar.Header.Headline"
        public static let statusBadge = "LonghouseMenuBar.Header.StatusBadge"
        public static let lastShip = "LonghouseMenuBar.Header.LastShip"
    }

    public enum Metric: String, CaseIterable {
        case service
        case engineAge
        case outbox
        case dead

        public var title: String {
            "LonghouseMenuBar.Metric.\(rawValue).Title"
        }

        public var value: String {
            "LonghouseMenuBar.Metric.\(rawValue).Value"
        }
    }

    public enum Detail: String, CaseIterable {
        case serviceFile
        case logPath
        case spoolPending
        case outboxOldest
        case launchState
        case machineRunner
        case serviceMachine
        case storedRunnerURL

        public var label: String {
            "LonghouseMenuBar.Detail.\(rawValue).Label"
        }

        public var value: String {
            "LonghouseMenuBar.Detail.\(rawValue).Value"
        }
    }

    public enum Section: String, CaseIterable {
        case launchChecks
        case reasons
        case next

        public var container: String {
            "LonghouseMenuBar.Section.\(rawValue)"
        }

        public var title: String {
            "LonghouseMenuBar.Section.\(rawValue).Title"
        }

        public func tag(_ index: Int) -> String {
            "LonghouseMenuBar.Section.\(rawValue).Tag.\(index)"
        }
    }

    public enum Disclosure {
        public static let troubleshooting = "LonghouseMenuBar.Disclosure.Troubleshooting"
        public static let technicalDetails = "LonghouseMenuBar.Disclosure.TechnicalDetails"
    }

    public enum Feedback {
        public static let container = "LonghouseMenuBar.Feedback"
        public static let title = "LonghouseMenuBar.Feedback.Title"
        public static let detail = "LonghouseMenuBar.Feedback.Detail"
    }

    public enum Button {
        public static let refresh = "LonghouseMenuBar.Button.Refresh"
        public static let doctor = "LonghouseMenuBar.Button.Doctor"
        public static let repair = "LonghouseMenuBar.Button.Repair"
        public static let copyDiagnostics = "LonghouseMenuBar.Button.CopyDiagnostics"
        public static let openLogs = "LonghouseMenuBar.Button.OpenLogs"
        public static let openLonghouse = "LonghouseMenuBar.Button.OpenLonghouse"
        public static let stopAllBackgroundManaged = "LonghouseMenuBar.Button.StopAllBackgroundManaged"
        public static let stopAllBackgroundBridges = "LonghouseMenuBar.Button.StopAllBackgroundBridges"
    }
}
