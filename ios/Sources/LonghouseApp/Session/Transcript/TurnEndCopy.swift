import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

/// Copy for the provider's turn accounting. Pure so row anchoring and duration
/// behavior stay deterministic.
enum TurnEndCopy {
    /// "2m 9s", "58s", "1h 2m": the terminal's own compaction of a duration.
    nonisolated static func duration(milliseconds: Int) -> String {
        let total = max(0, milliseconds) / 1000
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        if hours > 0 { return minutes > 0 ? "\(hours)h \(minutes)m" : "\(hours)h" }
        if minutes > 0 { return seconds > 0 ? "\(minutes)m \(seconds)s" : "\(minutes)m" }
        return "\(seconds)s"
    }

    /// "Turn finished 9:15 AM" today, "Turn finished Tue 9:15 AM" within a
    /// week, else with the date. A stopped turn says "stopped" instead.
    nonisolated static func doneAt(
        _ endedAt: String,
        now: Date = Date(),
        calendar: Calendar = .current,
        verb: String = "Turn finished"
    ) -> String {
        guard let date = LonghouseDateParser.parse(endedAt) else { return verb }
        let time = DateFormatter()
        time.calendar = calendar
        time.dateStyle = .none
        time.timeStyle = .short
        if calendar.isDate(date, inSameDayAs: now) {
            return "\(verb) \(time.string(from: date))"
        }
        let day = DateFormatter()
        day.calendar = calendar
        let withinWeek = now.timeIntervalSince(date) < 7 * 24 * 3600
        day.setLocalizedDateFormatFromTemplate(withinWeek ? "EEE" : "MMM d")
        return "\(verb) \(day.string(from: date)) \(time.string(from: date))"
    }
}
