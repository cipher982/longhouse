import Dispatch
import Darwin
import Foundation

final class LocalStatusMonitor: @unchecked Sendable {
    struct Projection: Sendable {
        let sessions: [SessionState]
        let engine: EngineStatusPayload?
    }

    struct SessionState: Sendable {
        let sessionId: String
    }

    private let statusURL: URL
    private let queue = DispatchQueue(label: "ai.longhouse.menu-bar.local-status", qos: .userInitiated)
    private let onChange: @Sendable (Projection) -> Void
    private let onWatchLost: @Sendable () -> Void
    private var source: DispatchSourceFileSystemObject?
    private var directoryHandle: CInt = -1
    private var fingerprint: Data?
    private var debounce: DispatchWorkItem?
    private let watchingLock = NSLock()
    private var watching = false

    init(
        statusPath: String,
        onWatchLost: @escaping @Sendable () -> Void = {},
        onChange: @escaping @Sendable (Projection) -> Void
    ) {
        statusURL = URL(fileURLWithPath: statusPath)
        self.onWatchLost = onWatchLost
        self.onChange = onChange
    }

    /// Whether a directory watch is live. False until `start()` opened the
    /// status directory, and again once that directory is deleted: either way
    /// no event will ever arrive, so the owner must re-arm.
    var isWatching: Bool {
        watchingLock.withLock { watching }
    }

    private func setWatching(_ value: Bool) {
        watchingLock.withLock { watching = value }
    }

    /// Opens the status directory here, not on the queue, so `isWatching` is
    /// false at once when the directory does not exist yet.
    func start() {
        let handle = open(statusURL.deletingLastPathComponent().path, O_EVTONLY)
        guard handle >= 0 else { return }
        setWatching(true)
        queue.async { [self] in
            guard source == nil else {
                close(handle)
                return
            }
            fingerprint = semanticFingerprint()
            directoryHandle = handle
            let newSource = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: directoryHandle,
                eventMask: [.write, .rename, .extend, .delete],
                queue: queue
            )
            newSource.setEventHandler { [weak self, weak newSource] in
                guard let self else { return }
                if newSource?.data.contains(.delete) == true {
                    self.setWatching(false)
                    newSource?.cancel()
                    self.source = nil
                    self.onWatchLost()
                    return
                }
                self.scheduleRead()
            }
            newSource.setCancelHandler { [weak self] in
                guard let self, self.directoryHandle >= 0 else { return }
                close(self.directoryHandle)
                self.directoryHandle = -1
            }
            source = newSource
            newSource.resume()
        }
    }

    func stop() {
        setWatching(false)
        queue.async { [self] in
            debounce?.cancel()
            source?.cancel()
            source = nil
        }
    }

    private func scheduleRead() {
        debounce?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.readIfChanged() }
        debounce = item
        queue.asyncAfter(deadline: .now() + .milliseconds(25), execute: item)
    }

    private func readIfChanged() {
        guard let next = semanticFingerprint(), next != fingerprint else { return }
        fingerprint = next
        onChange(Projection(sessions: localSessionStates(), engine: enginePayload()))
    }

    private func semanticFingerprint() -> Data? {
        guard let data = try? Data(contentsOf: statusURL),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let keys = [
            "archive_backlog",
            "control_channel",
            "is_offline", "last_ship_at", "last_ship_error_kind", "last_ship_result", "shipping_progress",
            "managed_sessions", "phase_ledger", "sessions_digest", "sessions_sequence",
            "spool_dead_count", "spool_pending_count", "storage_v2_outbox", "unmanaged_session_bindings",
        ]
        var semantic = Dictionary(uniqueKeysWithValues: keys.compactMap { key in
            payload[key].map { (key, $0) }
        })
        if let localProjection = payload["local_projection"] as? [String: Any] {
            var projectionSemantic: [String: Any] = [:]
            projectionSemantic["version"] = localProjection["version"]
            projectionSemantic["generated_at"] = localProjection["generated_at"]
            projectionSemantic["engine_pulse_at"] = localProjection["engine_pulse_at"]
            projectionSemantic["reconciliation"] = localProjection["reconciliation"]
            semantic["local_projection"] = projectionSemantic
        }
        return try? JSONSerialization.data(withJSONObject: semantic, options: [.sortedKeys])
    }

    private func localSessionStates() -> [SessionState] {
        guard let data = try? Data(contentsOf: statusURL),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = payload["managed_sessions"] as? [[String: Any]]
        else { return [] }
        return rows.compactMap { row in
            guard let sessionId = row["session_id"] as? String else { return nil }
            return SessionState(sessionId: sessionId)
        }
    }

    private func enginePayload() -> EngineStatusPayload? {
        guard let data = try? Data(contentsOf: statusURL) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(EngineStatusPayload.self, from: data)
    }
}
