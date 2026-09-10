import Foundation
import OSLog

/// The diagnostics funnel for one session open: every mark becomes one OSLog
/// line and, unless it is a per-frame stage, one telemetry entry.
///
/// Both of those are main-actor work. A wake loop that re-armed forever was
/// measured here at ~14k marks/second, with 1333 of 1559 main-thread samples
/// inside `_os_log_impl`, and it shipped a telemetry POST every 40 marks for as
/// long as it ran. So marks spend a bounded burst: per stage, and in total, so
/// a storm cannot buy volume by spreading itself over many stage names. A
/// healthy open emits tens of marks and never notices. What is dropped is
/// counted and reported, which keeps a loop visible in the log instead of
/// letting it consume the log.
@MainActor
final class SessionOpenWaterfall {
    private struct StageWindow {
        var startedAt: Date
        var emitted = 0
        var suppressed = 0
    }

    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionOpen")
    private let sessionId: String
    private let startedAt = Date()
    private var stages: [String: StageWindow] = [:]
    private var windowStartedAt = Date()
    private var totalEmitted = 0
    private var totalSuppressed = 0

    /// Marks allowed per stage per `windowSeconds`. A busy provider turn emits
    /// a handful of frames a second; a runaway loop emits thousands, and this
    /// is where that stops.
    private static let stageBurstLimit = 8
    /// Marks allowed across every stage per window. The per-stage limit alone
    /// bounds one loop; this bounds the total no matter how the marks are
    /// spread.
    private static let totalBurstLimit = 60
    private static let windowSeconds: TimeInterval = 1
    /// Keep no more stage windows than this many seconds of drift; a storm that
    /// invents names must not grow the map without limit.
    private static let stageRetentionSeconds: TimeInterval = 5
    private static let stageRetentionLimit = 64

    init(sessionId: String) {
        self.sessionId = sessionId
        mark("start")
    }

    func mark(_ stage: String, _ detail: String = "") {
        let now = Date()

        if now.timeIntervalSince(windowStartedAt) >= Self.windowSeconds {
            reportSuppressed(totalSuppressed, stage: nil)
            windowStartedAt = now
            totalEmitted = 0
            totalSuppressed = 0
            pruneStages(at: now)
        }
        if totalEmitted >= Self.totalBurstLimit {
            totalSuppressed += 1
            return
        }

        var window = stages[stage] ?? StageWindow(startedAt: now)
        if now.timeIntervalSince(window.startedAt) >= Self.windowSeconds {
            reportSuppressed(window.suppressed, stage: stage)
            window = StageWindow(startedAt: now)
        }
        if window.emitted >= Self.stageBurstLimit {
            window.suppressed += 1
            stages[stage] = window
            return
        }
        window.emitted += 1
        stages[stage] = window
        totalEmitted += 1
        emit(stage, detail)
    }

    /// Drops, across every stage, for tests that pin the budget.
    var suppressedMarkCountForTesting: Int {
        stages.values.reduce(0) { $0 + $1.suppressed } + totalSuppressed
    }

    /// The suppression is itself evidence rather than silence: one line naming
    /// what was dropped, at most once per stage per window.
    private func reportSuppressed(_ count: Int, stage: String?) {
        guard count > 0 else { return }
        let scope = stage.map { "stage=\($0)" } ?? "stage=all"
        emit("mark_suppressed", "\(scope) suppressed=\(count)")
    }

    private func pruneStages(at now: Date) {
        guard stages.count > Self.stageRetentionLimit else { return }
        stages = stages.filter { now.timeIntervalSince($0.value.startedAt) < Self.stageRetentionSeconds }
    }

    private func emit(_ stage: String, _ detail: String) {
        let elapsedMs = Int(Date().timeIntervalSince(startedAt) * 1000)
        if detail.isEmpty {
            logger.info("session open stage=\(stage, privacy: .public) session=\(self.sessionId, privacy: .public) elapsed_ms=\(elapsedMs, privacy: .public)")
        } else {
            logger.info("session open stage=\(stage, privacy: .public) session=\(self.sessionId, privacy: .public) elapsed_ms=\(elapsedMs, privacy: .public) \(detail, privacy: .public)")
        }
        // Where the run got to, for the next launch to read if this one never
        // terminates cleanly.
        RunBreadcrumb.shared.note(stage: stage, sessionId: sessionId)
        // The same marks, shipped: OSLog on a phone is unreadable without a
        // cable and root, so the server keeps a copy beside its own log.
        // Per-frame marks stay local; a busy Codex turn emits several a
        // second, and `stream_end` already carries the line and byte totals.
        guard !Self.localOnlyStages.contains(stage) else { return }
        ClientDiagnosticsReporter.shared.record(stage: stage, detail: detail, sessionId: sessionId)
    }

    private static let localOnlyStages: Set<String> = ["stream_changed", "stream_preview_applied"]
}
