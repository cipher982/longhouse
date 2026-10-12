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

    // The same timestamp strings are parsed on every menu-bar redraw. The
    // bounded cache is shared by MainActor presentation code and detached
    // producer work, so only cache access is locked; formatter parsing stays
    // outside the lock on misses.
    private enum ParseResult {
        case date(Date)
        case invalid

        var date: Date? {
            if case let .date(date) = self { return date }
            return nil
        }
    }

    private final class ParseCache: @unchecked Sendable {
        private static let capacity = 512
        private let lock = NSLock()
        private var results: [String: ParseResult] = [:]

        func date(for value: String, parse: () -> Date?) -> Date? {
            if let result = lock.withLock({ results[value] }) {
                return result.date
            }

            let parsed = parse()
            return lock.withLock {
                if let result = results[value] {
                    return result.date
                }
                if results.count >= Self.capacity {
                    results.removeAll(keepingCapacity: true)
                }
                results[value] = parsed.map(ParseResult.date) ?? .invalid
                return parsed
            }
        }
    }

    // ISO8601DateFormatter instances are configured once and are safe for
    // concurrent parsing on the supported modern macOS versions.
    private static let parsers = Parsers()
    private static let parseCache = ParseCache()
    private static let actionLogFormatter = LockedFormatter(options: [.withInternetDateTime])
    private static let fractionalFormatter = LockedFormatter(
        options: [.withInternetDateTime, .withFractionalSeconds]
    )
    private static let relativeFormatter = LockedRelativeFormatter()

    static func parse(_ value: String) -> Date? {
        parseCache.date(for: value) {
            parsers.fractional.date(from: value) ?? parsers.plain.date(from: value)
        }
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
