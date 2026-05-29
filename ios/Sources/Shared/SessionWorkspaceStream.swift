import Foundation

/// Realtime push from the server for a single session workspace.
///
/// Thin SSE (text/event-stream) client using URLSession.AsyncBytes. Parses
/// the event:/id:/data: grammar manually — there's no Apple SSE API.
///
/// Lifecycle: create with `start(sessionId:)`, observe via `AsyncStream`,
/// and call `stop()` on disappear. Automatically sends Last-Event-ID on
/// reconnect so the server can replay buffered events from the pubsub.
///
/// iOS background rules: caller must stop() when scenePhase != .active.
/// Background URLSession is not used; SSE over URLSession.shared is
/// foreground-only by Apple's contract.
actor SessionWorkspaceStream {
    struct Connected: Decodable, Sendable {
        let session_id: String
        let server_now_ms: Int64?
    }

    struct WorkspaceChanged: Decodable, Sendable {
        struct TranscriptPreview: Decodable, Sendable {
            let event_id: Int
            let text: String
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
        let thread_session_count: Int?
        let latest_event_emitted_at_ms: Int64?
        let server_fanout_at_ms: Int64?
        let server_now_ms: Int64?
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
    }

    /// Thrown internally when the SSE response is 401 so the reconnect loop can
    /// distinguish "auth is bad, stop looping" from a transient disconnect.
    private struct UnauthorizedError: Error {}

    private let baseURL: URL
    private let sessionId: String
    private let skipInitial: Bool
    private var task: Task<Void, Never>?
    /// Reconnect cursor. The server sets the SSE `id:` field to the per-topic
    /// pubsub sequence (NOT the DB event id), and replays buffered messages
    /// with `seq > Last-Event-ID`. So this tracks pubsub_seq despite the name.
    /// Seeded from a persisted snapshot on resume so a freshly-created actor
    /// replays from where the last one left off instead of cold.
    private var lastEventId: Int = 0
    private var serverClockSkewMs: Int64 = 0
    private var continuation: AsyncStream<Event>.Continuation?

    init(baseURL: URL, sessionId: String, skipInitial: Bool = true, sinceSeq: Int? = nil) {
        self.baseURL = baseURL
        self.sessionId = sessionId
        self.skipInitial = skipInitial
        if let sinceSeq, sinceSeq > 0 {
            self.lastEventId = sinceSeq
        }
    }

    static func streamURL(baseURL: URL, sessionId: String, skipInitial: Bool = true) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(sessionId)/workspace/stream"),
            resolvingAgainstBaseURL: false
        )!
        if skipInitial {
            components.queryItems = [URLQueryItem(name: "skip_initial", value: "true")]
        }
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
        continuation?.yield(event)
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
        let url = Self.streamURL(baseURL: baseURL, sessionId: sessionId, skipInitial: skipInitial)
        var req = URLRequest(url: url)
        req.addValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.addValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if lastEventId > 0 {
            req.addValue(String(lastEventId), forHTTPHeaderField: "Last-Event-ID")
        }
        if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
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

        let (bytes, response) = try await session.bytes(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        if http.statusCode == 401 {
            throw UnauthorizedError()
        }
        guard http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }

        var eventName = ""
        var eventId: String? = nil
        var dataBuffer = ""

        for try await line in bytes.lines {
            if Task.isCancelled { break }
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

    private func dispatch(eventName: String, eventId: String?, payload: String) async {
        if let eventId, let parsed = Int(eventId) {
            setLastEventId(parsed)
        }
        guard let data = payload.data(using: .utf8) else { return }
        switch eventName {
        case "connected":
            if let c = try? JSONDecoder().decode(Connected.self, from: data) {
                setSkew(c.server_now_ms)
                emit(.connected(c))
            }
        case "workspace_changed":
            if let w = try? JSONDecoder().decode(WorkspaceChanged.self, from: data) {
                emit(.changed(w))
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
