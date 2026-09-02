import Foundation
import OSLog

/// Realtime push from the server for a single session workspace.
///
/// Thin SSE (text/event-stream) client using URLSession.AsyncBytes. Parses
/// the event:/id:/data: grammar manually — there's no Apple SSE API.
///
/// Lifecycle: create with `start(sessionId:)`, observe via `AsyncStream`,
/// and call `stop()` on disappear. Automatically sends Last-Event-ID on
/// reconnect so the server can replay buffered events from the pubsub.
///
/// A stale-stream watchdog forces a reconnect if no event of any kind
/// (heartbeat included) arrives within `staleTimeoutSeconds`. The server
/// heartbeats this stream every 30s (`WORKSPACE_STREAM_HEARTBEAT_SECONDS`),
/// so 45s is a safe floor. Without it a dead-but-open TCP connection leaves
/// the session frozen until `timeoutIntervalForRequest` gives up an hour later.
///
/// iOS background rules: caller must stop() when scenePhase != .active.
/// Background URLSession is not used; SSE over URLSession.shared is
/// foreground-only by Apple's contract.
/// Captures the transport one stream negotiated so the app can report whether
/// the phone spoke h2 or h3 to the server. URLSession hands metrics over at
/// task end, so the answer is read when the stream closes.
private final class StreamTransportMetrics: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var protocolName: String?

    var negotiatedProtocol: String? {
        lock.withLock { protocolName }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        let name = metrics.transactionMetrics.last?.networkProtocolName
        lock.withLock { protocolName = name }
    }
}

actor SessionWorkspaceStream {
    struct Connected: Decodable, Sendable {
        let session_id: String
        let server_now_ms: Int64?
    }

    struct WorkspaceChanged: Decodable, Sendable {
        struct TranscriptPreview: Decodable, Sendable {
            let event_id: Int
            let text: String
            let role: String?
            let tool_name: String?
            let tool_input_json: JSONValue?
            let tool_output_text: String?
            let tool_call_id: String?
            let tool_call_state: String?
            let event_origin: String
            let timestamp: String
            let is_provisional: Bool
            let is_complete: Bool?
            let content_cursor: String?
            let is_stale: Bool?
            let stale_reason: String?

            var sessionTranscriptPreview: SessionTranscriptPreview {
                SessionTranscriptPreview(
                    eventId: event_id,
                    text: text,
                    role: role,
                    toolName: tool_name,
                    toolInputJSON: tool_input_json?.objectValue,
                    toolOutputText: tool_output_text,
                    toolCallId: tool_call_id,
                    toolCallState: tool_call_state.flatMap(ToolCallState.init(rawValue:)),
                    eventOrigin: event_origin,
                    timestamp: timestamp,
                    isProvisional: is_provisional,
                    isComplete: is_complete,
                    contentCursor: content_cursor,
                    isStale: is_stale,
                    staleReason: stale_reason
                )
            }
        }

        let session_id: String
        let latest_event_id: Int
        /// What woke the server: `ingest` (durable events landed), `runtime`
        /// (state facts), `transcript_preview`, `read_update`, `title_update`.
        var change_kind: String? = nil
        /// Durable events an `ingest` wake carried; nil for other wakes.
        var events_inserted: Int? = nil
        let thread_session_count: Int?
        let latest_event_emitted_at_ms: Int64?
        let server_fanout_at_ms: Int64?
        let server_now_ms: Int64?
        var catalog_commit_seq: Int64? = nil
        let pubsub_seq: Int?
        let transcript_preview: TranscriptPreview?
    }

    struct ReplayGap: Decodable, Sendable {
        let session_id: String
        let requested_seq: Int
        let earliest_seq: Int?
        let latest_seq: Int
        let reason: String
    }

    enum Event: Sendable {
        case connected(Connected)
        case changed(WorkspaceChanged)
        case replayGap(ReplayGap)
        case heartbeat
        case disconnected(Error?)
        /// The stream got a 401. Cookies are stale; reconnecting with them is
        /// pointless. The actor stops its retry loop and hands control to the
        /// caller, which should refresh auth and start a new stream.
        case unauthorized
        /// A transport fact worth recording (response headers, first byte,
        /// stall, decode failure, negotiated protocol). Never counts as
        /// liveness: only server frames reset the stale watchdog.
        case diagnostic(stage: String, detail: String)
    }

    /// Thrown internally when the SSE response is 401 so the reconnect loop can
    /// distinguish "auth is bad, stop looping" from a transient disconnect.
    private struct UnauthorizedError: Error {}

    private let baseURL: URL
    private let sessionId: String
    private let skipInitial: Bool
    private let knownWorkspaceFingerprint: String?
    private let staleTimeoutSeconds: TimeInterval
    private var lastEventAt: Date = Date()
    /// Per-connection transport counters, reported on stall and close so the
    /// difference between "no frames" and "frames the parser dropped" is visible.
    private var drainLineCount = 0
    private var drainByteCount = 0
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionStream")
    private var task: Task<Void, Never>?
    /// Reconnect cursor. The server sets the SSE `id:` field to the per-topic
    /// pubsub sequence (NOT the DB event id), and replays buffered messages
    /// with `seq > Last-Event-ID`. So this tracks pubsub_seq despite the name.
    /// Seeded from a persisted snapshot on resume so a freshly-created actor
    /// replays from where the last one left off instead of cold.
    private var lastEventId: Int = 0
    private var serverClockSkewMs: Int64 = 0
    private var continuation: AsyncStream<Event>.Continuation?

    init(
        baseURL: URL,
        sessionId: String,
        skipInitial: Bool = true,
        sinceSeq: Int? = nil,
        knownWorkspaceFingerprint: String? = nil,
        staleTimeoutSeconds: TimeInterval = 45
    ) {
        self.baseURL = baseURL
        self.sessionId = sessionId
        self.skipInitial = skipInitial
        self.knownWorkspaceFingerprint = knownWorkspaceFingerprint
        self.staleTimeoutSeconds = staleTimeoutSeconds
        if let sinceSeq, sinceSeq > 0 {
            self.lastEventId = sinceSeq
        }
    }

    static func streamURL(
        baseURL: URL,
        sessionId: String,
        skipInitial: Bool = true,
        knownWorkspaceFingerprint: String? = nil
    ) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(sessionId)/workspace/stream"),
            resolvingAgainstBaseURL: false
        )!
        var queryItems: [URLQueryItem] = []
        if skipInitial {
            queryItems.append(URLQueryItem(name: "skip_initial", value: "true"))
        }
        if let knownWorkspaceFingerprint, !knownWorkspaceFingerprint.isEmpty {
            queryItems.append(URLQueryItem(name: "known_workspace_fingerprint", value: knownWorkspaceFingerprint))
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url!
    }

    func clockSkewMs() -> Int64 { serverClockSkewMs }

    /// Starts the stream and returns an AsyncStream for events. Must be
    /// called at most once per instance. Subsequent calls return an empty
    /// stream so early events cannot be lost to a continuation-attach race.
    func start() -> AsyncStream<Event> {
        if task != nil {
            return AsyncStream { $0.finish() }
        }
        return AsyncStream { continuation in
            self.continuation = continuation
            self.task = Task { [weak self] in
                guard let self else { return }
                var backoffMs: UInt64 = 500
                while !Task.isCancelled {
                    do {
                        try await self.openAndDrain()
                        backoffMs = 500
                    } catch is CancellationError {
                        break
                    } catch is UnauthorizedError {
                        // Stale cookies: stop looping and let the caller refresh
                        // auth + restart the stream. Reconnecting here would
                        // just 401 again on a backoff timer.
                        await self.emit(.unauthorized)
                        break
                    } catch {
                        await self.note("stream_error", "error=\(error.localizedDescription) next_backoff_ms=\(backoffMs)")
                        await self.emit(.disconnected(error))
                    }
                    try? await Task.sleep(nanoseconds: backoffMs * 1_000_000)
                    backoffMs = min(backoffMs * 2, 15_000)
                }
                await self.finishContinuation()
            }
        }
    }

    private func finishContinuation() {
        continuation?.finish()
        continuation = nil
    }

    func stop() {
        task?.cancel()
        task = nil
        continuation?.finish()
        continuation = nil
    }

    private func emit(_ event: Event) {
        lastEventAt = Date()
        continuation?.yield(event)
    }

    /// Diagnostics bypass `emit` on purpose: they describe the transport and
    /// must not read as server liveness to the stale watchdog.
    private func note(_ stage: String, _ detail: String) {
        continuation?.yield(.diagnostic(stage: stage, detail: detail))
    }

    private func staleSinceLastEvent() -> Bool {
        Date().timeIntervalSince(lastEventAt) >= staleTimeoutSeconds
    }

    private func setLastEventId(_ id: Int) {
        if id > lastEventId { lastEventId = id }
    }

    private func replaceLastEventId(_ id: Int) {
        lastEventId = max(0, id)
    }

    private func setSkew(_ serverNowMs: Int64?) {
        guard let serverNowMs else { return }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        serverClockSkewMs = nowMs - serverNowMs
    }

    private func openAndDrain() async throws {
        let url = Self.streamURL(
            baseURL: baseURL,
            sessionId: sessionId,
            skipInitial: skipInitial,
            knownWorkspaceFingerprint: knownWorkspaceFingerprint
        )
        var req = URLRequest(url: url)
        req.addValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.addValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if lastEventId > 0 {
            req.addValue(String(lastEventId), forHTTPHeaderField: "Last-Event-ID")
        }
        if let authorizationHeader = SharedAuthStore.authorizationHeader(for: baseURL.absoluteString) {
            req.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
        } else if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
            req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }
        // waitsForConnectivity: the URLSession waits during transient network
        // unavailability (cell→wifi transitions) instead of failing fast.
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest = 600
        config.timeoutIntervalForResource = 3600
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }

        logger.debug("workspace stream request started session=\(self.sessionId, privacy: .public) since_seq=\(self.lastEventId, privacy: .public) skip_initial=\(self.skipInitial, privacy: .public)")
        let transport = StreamTransportMetrics()
        let openedAt = Date()
        drainLineCount = 0
        drainByteCount = 0
        defer {
            note(
                "stream_end",
                "lines=\(drainLineCount) bytes=\(drainByteCount) protocol=\(transport.negotiatedProtocol ?? "unknown") open_s=\(Int(Date().timeIntervalSince(openedAt)))"
            )
        }
        let (bytes, response) = try await session.bytes(for: req, delegate: transport)
        guard let http = response as? HTTPURLResponse else {
            logger.error("workspace stream bad response session=\(self.sessionId, privacy: .public)")
            throw URLError(.badServerResponse)
        }
        if http.statusCode == 401 {
            logger.info("workspace stream unauthorized session=\(self.sessionId, privacy: .public)")
            throw UnauthorizedError()
        }
        guard http.statusCode == 200 else {
            logger.error("workspace stream non-200 session=\(self.sessionId, privacy: .public) status=\(http.statusCode, privacy: .public)")
            throw URLError(.badServerResponse)
        }
        logger.debug("workspace stream response connected session=\(self.sessionId, privacy: .public)")
        note(
            "stream_response",
            "status=\(http.statusCode) encoding=\(http.value(forHTTPHeaderField: "Content-Encoding") ?? "identity") content_type=\(http.value(forHTTPHeaderField: "Content-Type") ?? "none") ttfb_ms=\(Int(Date().timeIntervalSince(openedAt) * 1000))"
        )

        lastEventAt = Date()
        let watchdog = Task { [weak self, staleTimeoutSeconds] in
            // Force-close the URLSession if no event arrives for the stale
            // timeout. Catches dead-but-open TCP connections, which otherwise
            // read as a healthy stream and freeze the session.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(staleTimeoutSeconds * 1_000_000_000))
                guard let self else { break }
                if await self.staleSinceLastEvent() {
                    await self.logStaleStreamStop()
                    session.invalidateAndCancel()
                    break
                }
            }
        }
        defer { watchdog.cancel() }

        var eventName = ""
        var eventId: String? = nil
        var dataBuffer = ""

        for try await line in SSELineReader.lines(from: bytes) {
            if Task.isCancelled { break }
            if drainLineCount == 0 {
                note("stream_first_line", "elapsed_ms=\(Int(Date().timeIntervalSince(openedAt) * 1000))")
            }
            drainLineCount += 1
            drainByteCount += line.utf8.count + 1
            if line.isEmpty {
                await self.dispatch(eventName: eventName, eventId: eventId, payload: dataBuffer)
                eventName = ""
                eventId = nil
                dataBuffer = ""
                continue
            }
            if line.hasPrefix(":") {
                // SSE comment / keep-alive. Ignore.
                continue
            }
            if let sep = line.firstIndex(of: ":") {
                let field = String(line[..<sep])
                var value = String(line[line.index(after: sep)...])
                if value.hasPrefix(" ") { value.removeFirst() }
                switch field {
                case "event": eventName = value
                case "id": eventId = value
                case "data":
                    if !dataBuffer.isEmpty { dataBuffer.append("\n") }
                    dataBuffer.append(value)
                default: break
                }
            }
        }
        if !Task.isCancelled {
            emit(.disconnected(nil))
        }
    }

    private func logStaleStreamStop() {
        logger.info(
            "workspace stream stale, forcing reconnect session=\(self.sessionId, privacy: .public) stale_after_s=\(self.staleTimeoutSeconds, privacy: .public)"
        )
        note("stream_stale", "stale_after_s=\(Int(staleTimeoutSeconds)) lines=\(drainLineCount) bytes=\(drainByteCount)")
    }

    private func dispatch(eventName: String, eventId: String?, payload: String) async {
        if let eventId, let parsed = Int(eventId) {
            setLastEventId(parsed)
        }
        guard let data = payload.data(using: .utf8) else { return }
        switch eventName {
        case "connected":
            do {
                let c = try JSONDecoder().decode(Connected.self, from: data)
                setSkew(c.server_now_ms)
                emit(.connected(c))
            } catch {
                logger.error("workspace stream connected decode failed session=\(self.sessionId, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            }
        case "workspace_changed":
            do {
                let w = try JSONDecoder().decode(WorkspaceChanged.self, from: data)
                emit(.changed(w))
            } catch {
                logger.error(
                    "workspace stream decode failed session=\(self.sessionId, privacy: .public) error=\(error.localizedDescription, privacy: .public) payload=\(payload, privacy: .private(mask: .hash))"
                )
                note("stream_decode_failed", "event=workspace_changed bytes=\(payload.utf8.count) error=\(error)")
            }
        case "replay_gap":
            if let gap = try? JSONDecoder().decode(ReplayGap.self, from: data) {
                // The cursor belongs to an old or truncated replay domain.
                // Reset to the server's current latest seq so future reconnects
                // do not keep asking for an impossible cursor.
                replaceLastEventId(gap.latest_seq)
                emit(.replayGap(gap))
            }
        case "heartbeat":
            emit(.heartbeat)
        default:
            break
        }
    }
}
