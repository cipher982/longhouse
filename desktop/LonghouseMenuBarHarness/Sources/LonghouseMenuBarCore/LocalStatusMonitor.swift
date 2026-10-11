import Dispatch
import Darwin
import Foundation

final class LocalStatusMonitor: @unchecked Sendable {
    struct Projection: Sendable {
        let sessions: [SessionState]
        let engine: EngineStatusPayload?
        let evidenceObservedAt: String?
        let presentationChanged: Bool
        let requiresSnapshotRefresh: Bool

        init(
            sessions: [SessionState],
            engine: EngineStatusPayload?,
            evidenceObservedAt: String? = nil,
            presentationChanged: Bool = false,
            requiresSnapshotRefresh: Bool = false
        ) {
            self.sessions = sessions
            self.engine = engine
            self.evidenceObservedAt = evidenceObservedAt
            self.presentationChanged = presentationChanged
            self.requiresSnapshotRefresh = requiresSnapshotRefresh
        }
    }

    struct SessionState: Sendable {
        let sessionId: String
    }

    struct PresentationKey: Equatable {
        let sessionIds: [String]
        let engine: EngineStatusPayload?

        init(sessionIds: [String], engine: EngineStatusPayload?) {
            self.sessionIds = sessionIds
            self.engine = Self.withoutClocks(engine)
        }

        static func withoutClocks(_ payload: EngineStatusPayload?) -> EngineStatusPayload? {
            guard let payload else { return nil }
            let localProjection = payload.localProjection.map { projection in
                LocalProjectionStatus(
                    version: nil,
                    generatedAt: nil,
                    enginePulseAt: nil,
                    lastReconciledAt: nil,
                    reconciliation: projection.reconciliation.map { reconciliation in
                        ProjectionReconciliationStatus(
                            state: reconciliation.state,
                            reason: reconciliation.reason,
                            startedAt: nil,
                            failureReason: reconciliation.failureReason
                        )
                    }
                )
            }
            let shippingProgress = payload.shippingProgress.map { progress in
                ShippingProgressSnapshot(
                    pendingWork: progress.pendingWork,
                    stalled: progress.stalled,
                    secondsWithoutProgress: nil,
                    observedAt: nil
                )
            }
            let recentDeadLetters = payload.recentDeadLetters?.map { deadLetter in
                DeadLetterSnapshot(
                    provider: deadLetter.provider,
                    filePath: deadLetter.filePath,
                    rangeBytes: deadLetter.rangeBytes,
                    createdAt: nil
                )
            }
            return EngineStatusPayload(
                version: payload.version,
                daemonPid: payload.daemonPid,
                lastShipAt: nil,
                spoolPendingCount: payload.spoolPendingCount,
                spoolDeadCount: payload.spoolDeadCount,
                archiveBacklog: payload.archiveBacklog,
                storageV2Outbox: payload.storageV2Outbox,
                parseErrorCount1H: nil,
                diskFreeBytes: nil,
                isOffline: payload.isOffline,
                localProjection: localProjection,
                shippingProgress: shippingProgress,
                recentDeadLetters: recentDeadLetters,
                lastUpdated: nil,
                build: payload.build
            )
        }
    }


    private let statusURL: URL
    private let queue = DispatchQueue(label: "ai.longhouse.menu-bar.local-status", qos: .userInitiated)
    private let onChange: @Sendable (Projection) -> Void
    private let onWatchLost: @Sendable () -> Void
    /// The status file was readable and now is not (deleted, unreadable,
    /// undecodable). The evidence it carried is superseded, not current.
    private let onUnreadable: @Sendable () -> Void
    private let decoder = JSONDecoder()
    private var source: DispatchSourceFileSystemObject?
    private var directoryHandle: CInt = -1
    private var presentationFingerprint: LocalStatusFilePresentationKey?
    private var lastPayload: EngineStatusPayload?
    private var lastSessionIds: [String]?
    private var lastEvidenceObservedAt: String?
    private var debounce: DispatchWorkItem?
    private let watchingLock = NSLock()
    private var watching = false

    init(
        statusPath: String,
        onWatchLost: @escaping @Sendable () -> Void = {},
        onUnreadable: @escaping @Sendable () -> Void = {},
        onChange: @escaping @Sendable (Projection) -> Void
    ) {
        statusURL = URL(fileURLWithPath: statusPath)
        self.onWatchLost = onWatchLost
        self.onUnreadable = onUnreadable
        self.onChange = onChange
        decoder.keyDecodingStrategy = .convertFromSnakeCase
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
            directoryHandle = handle
            if let initial = readStatusFile() {
                presentationFingerprint = initial.presentationKey
                lastPayload = initial.engine
                lastSessionIds = initial.sessionIds
                lastEvidenceObservedAt = initial.evidenceObservedAt
                onChange(
                    Projection(
                        sessions: initial.sessionIds.map(SessionState.init(sessionId:)),
                        engine: initial.engine,
                        evidenceObservedAt: initial.evidenceObservedAt
                    )
                )
            }
            let newSource = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: directoryHandle,
                eventMask: [.write, .rename, .extend, .delete],
                queue: queue
            )
            newSource.setEventHandler { [weak self, weak newSource] in
                guard let self else { return }
                // On a directory fd these mean the directory itself moved or
                // went away; files written inside it arrive as `.write`.
                if let events = newSource?.data, !events.isDisjoint(with: [.delete, .rename]) {
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
        guard let next = readStatusFile() else {
            // Forget what the file said, so its return is a change again.
            if lastPayload != nil || lastSessionIds != nil {
                presentationFingerprint = nil
                lastPayload = nil
                lastSessionIds = nil
                lastEvidenceObservedAt = nil
                onUnreadable()
            }
            return
        }
        let key = next.presentationKey
        let presentationChanged = key != presentationFingerprint
        let requiresSnapshotRefresh = key.requiresSnapshotRefresh(comparedTo: presentationFingerprint)
        guard next.engine != lastPayload
                || next.sessionIds != lastSessionIds
                || next.evidenceObservedAt != lastEvidenceObservedAt
                || presentationChanged
        else { return }

        presentationFingerprint = key
        lastPayload = next.engine
        lastSessionIds = next.sessionIds
        lastEvidenceObservedAt = next.evidenceObservedAt
        onChange(
            Projection(
                sessions: next.sessionIds.map(SessionState.init(sessionId:)),
                engine: next.engine,
                evidenceObservedAt: next.evidenceObservedAt,
                presentationChanged: presentationChanged,
                requiresSnapshotRefresh: requiresSnapshotRefresh
            )
        )
    }

    private func readStatusFile() -> LocalStatusFileSnapshot? {
        guard let data = try? Data(contentsOf: statusURL) else { return nil }
        return try? decoder.decode(LocalStatusFileSnapshot.self, from: data)
    }
}
private struct LocalStatusFileSnapshot: Decodable {
    private struct MachineEvidence: Decodable {
        let observedAt: String?
    }

    let sessionIds: [String]
    let evidenceObservedAt: String?
    let engine: EngineStatusPayload?
    let presentationKey: LocalStatusFilePresentationKey

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        func value(_ key: CodingKeys) -> StatusJSONValue? {
            try? container.decode(StatusJSONValue.self, forKey: key)
        }
        func normalized(_ value: StatusJSONValue?, for key: CodingKeys) -> StatusJSONValue? {
            guard !StatusJSONValue.isVolatileStatusKey(key.stringValue, parentKey: nil) else { return nil }
            return value?.withoutVolatileStatusValues(parentKey: key.stringValue)
        }

        let managedSessions = value(.managedSessions)
        let sessions = value(.sessions)
        let phaseLedger = value(.phaseLedger)
        let phaseLedgerStatus = value(.phaseLedgerStatus)
        let unmanagedSessionBindings = value(.unmanagedSessionBindings)
        let controlChannel = value(.controlChannel)
        let hostLink = value(.hostLink)
        let heartbeatTransport = value(.heartbeatTransport)
        sessionIds = managedSessions?.sessionIds ?? []
        evidenceObservedAt = (try? container.decode(MachineEvidence.self, forKey: .machineEvidence))?
            .observedAt
        engine = try? EngineStatusPayload(from: decoder)

        let counterKeys: [(CodingKeys, String)] = [
            (.diskFreeBytes, "disk_free_bytes"),
            (.lastShipResult, "last_ship_result"),
            (.lastShipErrorKind, "last_ship_error_kind"),
            (.lastShipErrorMessage, "last_ship_error_message"),
            (.lastShipHttpStatus, "last_ship_http_status"),
            (.runtimeEventOutbox, "runtime_event_outbox"),
            (.shipAttempts1H, "ship_attempts_1h"),
            (.shipSuccesses1H, "ship_successes_1h"),
            (.shipRateLimited1H, "ship_rate_limited_1h"),
            (.shipServerErrors1H, "ship_server_errors_1h"),
            (.shipPayloadRejections1H, "ship_payload_rejections_1h"),
            (.shipPayloadTooLarge1H, "ship_payload_too_large_1h"),
            (.shipRetryableClientErrors1H, "ship_retryable_client_errors_1h"),
            (.shipConnectErrors1H, "ship_connect_errors_1h"),
            (.shipLatencyP50Ms1H, "ship_latency_p50_ms_1h"),
            (.shipLatencyP95Ms1H, "ship_latency_p95_ms_1h"),
            (.shipAttempts10m, "ship_attempts_10m"),
            (.shipSuccesses10m, "ship_successes_10m"),
            (.shipRateLimited10m, "ship_rate_limited_10m"),
            (.shipServerErrors10m, "ship_server_errors_10m"),
            (.shipRetryableClientErrors10m, "ship_retryable_client_errors_10m"),
            (.shipConnectErrors10m, "ship_connect_errors_10m"),
            (.shipLanes, "ship_lanes"),
            (.shipScheduler, "ship_scheduler"),
            (.adaptiveBacklogLimiter, "adaptive_backlog_limiter"),
            (.localDatabaseBytes, "local_database_bytes"),
            (.historyImport, "history_import"),
            (.update, "update"),
        ]
        var countersAndErrors: [String: StatusJSONValue] = [:]
        for (key, name) in counterKeys {
            if let fact = normalized(value(key), for: key) {
                countersAndErrors[name] = fact
            }
        }
        let rootKey = LocalStatusRootPresentationKey(
            managedSessions: normalized(managedSessions, for: .managedSessions),
            sessions: normalized(sessions, for: .sessions),
            phaseLedger: normalized(phaseLedger, for: .phaseLedger),
            phaseLedgerStatus: normalized(phaseLedgerStatus, for: .phaseLedgerStatus),
            unmanagedSessionBindings: normalized(unmanagedSessionBindings, for: .unmanagedSessionBindings),
            controlChannel: normalized(controlChannel, for: .controlChannel),
            hostLink: normalized(hostLink, for: .hostLink),
            heartbeatTransport: normalized(heartbeatTransport, for: .heartbeatTransport),
            countersAndErrors: countersAndErrors
        )
        presentationKey = LocalStatusFilePresentationKey(
            sessionIds: sessionIds,
            engine: LocalStatusMonitor.PresentationKey(sessionIds: sessionIds, engine: engine),
            root: rootKey
        )
    }

    private enum CodingKeys: String, CodingKey {
        case machineEvidence
        case managedSessions
        case sessions
        case phaseLedger
        case phaseLedgerStatus
        case unmanagedSessionBindings
        case controlChannel
        case hostLink
        case heartbeatTransport
        case diskFreeBytes
        case lastShipResult
        case lastShipErrorKind
        case lastShipErrorMessage
        case lastShipHttpStatus
        case runtimeEventOutbox
        case shipAttempts1H
        case shipSuccesses1H
        case shipRateLimited1H
        case shipServerErrors1H
        case shipPayloadRejections1H
        case shipPayloadTooLarge1H
        case shipRetryableClientErrors1H
        case shipConnectErrors1H
        case shipLatencyP50Ms1H
        case shipLatencyP95Ms1H
        case shipAttempts10m
        case shipSuccesses10m
        case shipRateLimited10m
        case shipServerErrors10m
        case shipRetryableClientErrors10m
        case shipConnectErrors10m
        case shipLanes
        case shipScheduler
        case adaptiveBacklogLimiter
        case localDatabaseBytes
        case historyImport
        case update
    }
}

private struct LocalStatusFilePresentationKey: Equatable {
    let sessionIds: [String]
    let engine: LocalStatusMonitor.PresentationKey
    let root: LocalStatusRootPresentationKey

    func requiresSnapshotRefresh(comparedTo previous: Self?) -> Bool {
        guard let previous else { return false }
        return root != previous.root
    }
}

private struct LocalStatusRootPresentationKey: Equatable {
    let managedSessions: StatusJSONValue?
    let sessions: StatusJSONValue?
    let phaseLedger: StatusJSONValue?
    let phaseLedgerStatus: StatusJSONValue?
    let unmanagedSessionBindings: StatusJSONValue?
    let controlChannel: StatusJSONValue?
    let hostLink: StatusJSONValue?
    let heartbeatTransport: StatusJSONValue?
    let countersAndErrors: [String: StatusJSONValue]
}

private indirect enum StatusJSONValue: Decodable, Equatable {
    case null
    case bool(Bool)
    case integer(Int64)
    case unsigned(UInt64)
    case number(Double)
    case string(String)
    case array([StatusJSONValue])
    case object([String: StatusJSONValue])

    private struct DynamicKey: CodingKey {
        let stringValue: String
        let intValue: Int?

        init?(stringValue: String) {
            self.stringValue = stringValue
            intValue = nil
        }

        init?(intValue: Int) {
            self.stringValue = String(intValue)
            self.intValue = intValue
        }
    }

    init(from decoder: Decoder) throws {
        let single = try decoder.singleValueContainer()
        if single.decodeNil() {
            self = .null
        } else if let value = try? single.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? single.decode(Int64.self) {
            self = .integer(value)
        } else if let value = try? single.decode(UInt64.self) {
            self = .unsigned(value)
        } else if let value = try? single.decode(Double.self) {
            self = .number(value)
        } else if let value = try? single.decode(String.self) {
            self = .string(value)
        } else if var container = try? decoder.unkeyedContainer() {
            var values: [StatusJSONValue] = []
            while !container.isAtEnd {
                values.append(try container.decode(StatusJSONValue.self))
            }
            self = .array(values)
        } else {
            let container = try decoder.container(keyedBy: DynamicKey.self)
            var values: [String: StatusJSONValue] = [:]
            for key in container.allKeys {
                values[key.stringValue] = try container.decode(StatusJSONValue.self, forKey: key)
            }
            self = .object(values)
        }
    }

    var sessionIds: [String] {
        guard case let .array(rows) = self else { return [] }
        return rows.compactMap { row in
            guard case let .object(fields) = row,
                  let value = fields["session_id"] ?? fields["sessionId"],
                  case let .string(sessionId) = value else { return nil }
            return sessionId
        }
    }

    func withoutVolatileStatusValues(parentKey: String? = nil) -> StatusJSONValue {
        switch self {
        case let .object(fields):
            var filtered: [String: StatusJSONValue] = [:]
            for (key, value) in fields where !Self.isVolatileStatusKey(key, parentKey: parentKey) {
                filtered[key] = value.withoutVolatileStatusValues(parentKey: key)
            }
            return .object(filtered)
        case let .array(values):
            return .array(values.map { $0.withoutVolatileStatusValues() })
        default:
            return self
        }
    }


    /// Mirrors `heartbeat::is_volatile_status_key` so clock-only rewrites
    /// cannot invalidate the desktop's presentation snapshot.
    static func isVolatileStatusKey(_ key: String, parentKey: String?) -> Bool {
        let lowercased = key.lowercased()
        let compact = lowercased.filter { $0 != "_" }
        let parentCompact = parentKey?.lowercased().filter { $0 != "_" }
        return key.hasSuffix("_at")
            || key.hasSuffix("At")
            || lowercased == "valid_until"
            || compact == "validuntil"
            || compact == "lastupdated"
            || compact == "sendmetrics"
            || lowercased.contains("_ewma_")
            || key.contains("Ewma")
            || lowercased.hasSuffix("_1h")
            || lowercased.hasSuffix("_10m")
            || lowercased.hasSuffix("_ms")
            || compact.hasSuffix("1h")
            || compact.hasSuffix("10m")
            || key.hasSuffix("Ms")
            || lowercased.hasPrefix("seconds_")
            || compact.hasPrefix("seconds")
            || compact == "diskfreebytes"
            || compact == "sequence"
            || compact == "sessionssequence"
            || (parentCompact == "localprojection" && compact == "version")
    }
}
