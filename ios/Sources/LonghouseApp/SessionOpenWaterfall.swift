import Foundation
import OSLog

@MainActor
final class SessionOpenWaterfall {
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionOpen")
    private let sessionId: String
    private let startedAt = Date()

    init(sessionId: String) {
        self.sessionId = sessionId
        mark("start")
    }

    func mark(_ stage: String, _ detail: String = "") {
        let elapsedMs = Int(Date().timeIntervalSince(startedAt) * 1000)
        if detail.isEmpty {
            logger.info("session open stage=\(stage, privacy: .public) session=\(self.sessionId, privacy: .public) elapsed_ms=\(elapsedMs, privacy: .public)")
        } else {
            logger.info("session open stage=\(stage, privacy: .public) session=\(self.sessionId, privacy: .public) elapsed_ms=\(elapsedMs, privacy: .public) \(detail, privacy: .public)")
        }
        // The same marks, shipped: OSLog on a phone is unreadable without a
        // cable and root, so the server keeps a copy beside its own log.
        // Per-frame marks stay local; a busy Codex turn emits several a
        // second, and `stream_end` already carries the line and byte totals.
        guard !Self.localOnlyStages.contains(stage) else { return }
        ClientDiagnosticsReporter.shared.record(stage: stage, detail: detail, sessionId: sessionId)
    }

    private static let localOnlyStages: Set<String> = ["stream_changed", "stream_preview_applied"]
}
