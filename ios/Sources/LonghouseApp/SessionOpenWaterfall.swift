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
    }
}
