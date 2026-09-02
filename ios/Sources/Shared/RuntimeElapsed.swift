import Foundation

/// Elapsed-time copy for the runtime dock. Two registers: a precise count
/// ("1m 12s") while a turn is executing, where the ticking digits are the
/// cheapest honest "still alive" signal there is; and a coarse age ("4m",
/// "2h") for idle or ended sessions, where seconds would be noise.
enum RuntimeElapsed {
    static func label(seconds raw: TimeInterval, precise: Bool) -> String {
        let seconds = max(0, Int(raw.rounded(.down)))
        if precise {
            if seconds < 60 { return "\(seconds)s" }
            if seconds < 3600 {
                return "\(seconds / 60)m \(String(format: "%02d", seconds % 60))s"
            }
            return "\(seconds / 3600)h \(String(format: "%02d", (seconds % 3600) / 60))m"
        }
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3600)h" }
        return "\(seconds / 86_400)d"
    }

    static func label(from start: Date, to end: Date, precise: Bool) -> String {
        label(seconds: end.timeIntervalSince(start), precise: precise)
    }
}
