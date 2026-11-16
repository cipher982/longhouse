import Foundation

/// Shared ISO8601 parsing for Longhouse backend timestamps.
///
/// Keep two pre-configured formatters (fractional + plain) to avoid allocating
/// them on every tool-row render. Access stays serialized because the parsers
/// are shared across Swift Testing and timeline refresh tasks.
enum LonghouseDateParser {
    private static let lock = NSLock()
    nonisolated(unsafe) private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    nonisolated(unsafe) private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func parse(_ s: String) -> Date? {
        if let date = parseInternetDateTime(s) { return date }
        lock.lock()
        defer { lock.unlock() }
        return fractional.date(from: s) ?? plain.date(from: s)
    }

    /// `YYYY-MM-DDTHH:MM:SS[.fff…](Z|±HH:MM|±HHMM)`, the only shape the server
    /// emits, parsed without ICU. ISO8601DateFormatter costs microseconds per
    /// call and timeline sorting calls this per comparison: 0.7 s of main
    /// thread on a device cold launch. Anything else falls through to the
    /// formatters, so accepted input is unchanged.
    static func parseInternetDateTime(_ string: String) -> Date? {
        var string = string
        return string.withUTF8 { b -> Date? in
            let n = b.count
            guard n >= 20 else { return nil }
            func number(_ at: Int, _ width: Int) -> Int? {
                guard at + width <= n else { return nil }
                var value = 0
                for k in at..<(at + width) {
                    let digit = b[k] &- 48
                    guard digit < 10 else { return nil }
                    value = value * 10 + Int(digit)
                }
                return value
            }
            guard b[4] == 0x2D, b[7] == 0x2D, b[10] == 0x54, b[13] == 0x3A, b[16] == 0x3A,
                  let year = number(0, 4), let month = number(5, 2), let day = number(8, 2),
                  let hour = number(11, 2), let minute = number(14, 2), let second = number(17, 2),
                  (1...12).contains(month), day >= 1, hour < 24, minute < 60, second < 60
            else { return nil }
            // Impossible dates (Feb 30, Feb 29 in a common year) fall through
            // to the formatter rather than rolling into the next month.
            let isLeap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
            let monthDays = [31, isLeap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            guard day <= monthDays[month - 1] else { return nil }
            var i = 19
            var fraction = 0.0
            if b[i] == 0x2E {
                i += 1
                let start = i
                var scale = 0.1
                while i < n, b[i] &- 48 < 10 {
                    fraction += Double(b[i] &- 48) * scale
                    scale /= 10
                    i += 1
                }
                guard i > start else { return nil }
            }
            guard i < n else { return nil }
            var offset = 0
            switch b[i] {
            case 0x5A:
                i += 1
            case 0x2B, 0x2D:
                let sign = b[i] == 0x2B ? 1 : -1
                i += 1
                guard let offsetHours = number(i, 2) else { return nil }
                i += 2
                if i < n, b[i] == 0x3A { i += 1 }
                guard let offsetMinutes = number(i, 2), offsetHours < 24, offsetMinutes < 60 else { return nil }
                i += 2
                offset = sign * (offsetHours * 3600 + offsetMinutes * 60)
            default:
                return nil
            }
            guard i == n else { return nil }
            // Days from 1970-01-01 in the proleptic Gregorian calendar
            // (Howard Hinnant's days_from_civil).
            let y = month <= 2 ? year - 1 : year
            let era = y / 400
            let yearOfEra = y - era * 400
            let dayOfYear = (153 * ((month + 9) % 12) + 2) / 5 + day - 1
            let dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear
            let days = era * 146_097 + dayOfEra - 719_468
            let seconds = days * 86_400 + hour * 3600 + minute * 60 + second - offset
            return Date(timeIntervalSince1970: Double(seconds) + fraction)
        }
    }
}
