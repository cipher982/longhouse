import Foundation

/// Shared date formatters for the menu-bar hot paths.
enum ISO8601DateFormatters {
    private final class Parsers: @unchecked Sendable {
        let fractional: ISO8601DateFormatter
        let plain: ISO8601DateFormatter

        init() {
            fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            plain = ISO8601DateFormatter()
            plain.formatOptions = [.withInternetDateTime]
        }
    }

    private final class LockedFormatter: @unchecked Sendable {
        private let formatter: ISO8601DateFormatter
        private let lock = NSLock()

        init(options: ISO8601DateFormatter.Options) {
            formatter = ISO8601DateFormatter()
            formatter.formatOptions = options
        }

        func string(from date: Date) -> String {
            lock.withLock { formatter.string(from: date) }
        }
    }

    private final class LockedRelativeFormatter: @unchecked Sendable {
        private let formatter = RelativeDateTimeFormatter()
        private let lock = NSLock()

        init() {
            formatter.unitsStyle = .full
        }

        func string(for date: Date, relativeTo referenceDate: Date) -> String {
            lock.withLock {
                formatter.localizedString(for: date, relativeTo: referenceDate)
            }
        }
    }

    // ISO8601DateFormatter.date(from:) is thread-safe on the supported modern
    // macOS versions. These instances are configured once and then used only
    // for lock-free concurrent parsing.
    private static let parsers = Parsers()
    private static let actionLogFormatter = LockedFormatter(options: [.withInternetDateTime])
    private static let fractionalFormatter = LockedFormatter(
        options: [.withInternetDateTime, .withFractionalSeconds]
    )
    private static let relativeFormatter = LockedRelativeFormatter()

    static func parse(_ value: String) -> Date? {
        parsers.fractional.date(from: value) ?? parsers.plain.date(from: value)
    }

    static func actionLogTimestamp(for date: Date) -> String {
        actionLogFormatter.string(from: date)
    }

    static func fractionalTimestamp(for date: Date) -> String {
        fractionalFormatter.string(from: date)
    }

    static func relativeLabel(for date: Date, relativeTo referenceDate: Date) -> String {
        relativeFormatter.string(for: date, relativeTo: referenceDate)
    }
}
