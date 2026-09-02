import Foundation
import UIKit

/// Ships the app's own lifecycle marks (stream connects, stalls, polls,
/// reconciles) to the server, so a phone can be debugged from a terminal
/// instead of a cable. `SessionOpenWaterfall` already logs every mark to
/// OSLog; this is the same stream of facts, batched and posted every few
/// seconds to `/api/telemetry/client-diagnostics`, where they land as one
/// log line each beside the server's view of the same session.
@MainActor
final class ClientDiagnosticsReporter {
    static let shared = ClientDiagnosticsReporter()

    static let flushDelay: TimeInterval = 3
    static let flushThreshold = 40
    static let maxBuffered = 400

    /// Whoever holds an API client installs the sink; without one, marks
    /// stay local (fixtures, previews, signed-out app).
    var sink: (@Sendable (ClientDiagnosticsPayload) async -> Void)?

    private var buffer: [ClientDiagnosticsPayload.Entry] = []
    private var flushTask: Task<Void, Never>?
    private let deviceLabel: String = "\(UIDevice.current.name) \(UIDevice.current.systemVersion)"
    private let appBuild: String? = (try? BuildIdentityLoader.loadFromMainBundle().get())?.qualifiedVersion

    func record(stage: String, detail: String? = nil, sessionId: String?) {
        buffer.append(
            ClientDiagnosticsPayload.Entry(
                at_ms: Int64(Date().timeIntervalSince1970 * 1000),
                stage: stage,
                detail: (detail?.isEmpty ?? true) ? nil : detail,
                session_id: sessionId
            )
        )
        if buffer.count > Self.maxBuffered {
            buffer.removeFirst(buffer.count - Self.maxBuffered)
        }
        if buffer.count >= Self.flushThreshold {
            flush()
            return
        }
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.flushDelay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.flushTask = nil
            self?.flush()
        }
    }

    func flush() {
        flushTask?.cancel()
        flushTask = nil
        guard let sink, !buffer.isEmpty else { return }
        let entries = Array(buffer.suffix(200))
        buffer.removeAll()
        let payload = ClientDiagnosticsPayload(
            surface: "ios",
            device_label: deviceLabel,
            app_build: appBuild,
            entries: entries
        )
        Task { await sink(payload) }
    }
}
