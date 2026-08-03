import Foundation
import OSLog

private enum SessionStreamTransportEvent: @unchecked Sendable {
    case response(HTTPURLResponse)
    case data(Data)
}

private final class SessionStreamDataDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let continuation: AsyncThrowingStream<SessionStreamTransportEvent, Error>.Continuation

    init(continuation: AsyncThrowingStream<SessionStreamTransportEvent, Error>.Continuation) {
        self.continuation = continuation
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let response = response as? HTTPURLResponse else {
            continuation.finish(throwing: URLError(.badServerResponse))
            completionHandler(.cancel)
            return
        }
        continuation.yield(.response(response))
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        continuation.yield(.data(data))
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: (any Error)?
    ) {
        if let error {
            continuation.finish(throwing: error)
        } else {
            continuation.finish()
        }
    }
}

/// Realtime push from the server for a single session workspace.
///
/// Thin SSE (text/event-stream) client using URLSessionDataDelegate. Parses
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
    struct ClockCalibration: Sendable {
        let skewMs: Int64
        let rttMs: Int?
        let uncertaintyMs: Int?
        let sampleCount: Int
    }

    private struct ClockSyncResponse: Decodable {
        let server_received_at_ms: Int64
        let server_sent_at_ms: Int64
    }

    struct Connected: Decodable, Sendable {
        let session_id: String
        let server_now_ms: Int64?
    }

    struct WorkspaceChanged: Decodable, Sendable {
        struct ShipTrace: Decodable, Sendable {
            let trace_id: String?
            let observed_at_ms: Int64?
            let enqueued_at_ms: Int64?
            let job_started_at_ms: Int64?
            let http_send_started_at_ms: Int64?
        }

        struct ServerTrace: Decodable, Sendable {
            let handler_entered_at_ms: Int64?
        }

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
        let thread_session_count: Int?
        let latest_event_emitted_at_ms: Int64?
        let server_fanout_at_ms: Int64?
        let server_now_ms: Int64?
        var catalog_commit_seq: Int64? = nil
        let pubsub_seq: Int?
        let transcript_preview: TranscriptPreview?
        var ship_trace: ShipTrace? = nil
        var server_trace: ServerTrace? = nil
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
    private let knownWorkspaceFingerprint: String?
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionStream")
    private var task: Task<Void, Never>?
    /// Reconnect cursor. The server sets the SSE `id:` field to the per-topic
    /// pubsub sequence (NOT the DB event id), and replays buffered messages
    /// with `seq > Last-Event-ID`. So this tracks pubsub_seq despite the name.
    /// Seeded from a persisted snapshot on resume so a freshly-created actor
    /// replays from where the last one left off instead of cold.
    private var lastEventId: Int = 0
    private var serverClockSkewMs: Int64 = 0
    private var clockSyncRttMs: Int?
    private var clockSyncUncertaintyMs: Int?
    private var clockSyncSampleCount = 0
    private var clockCalibrationTask: Task<Void, Never>?
    private var continuation: AsyncStream<Event>.Continuation?

    init(
        baseURL: URL,
        sessionId: String,
        skipInitial: Bool = true,
        sinceSeq: Int? = nil,
        knownWorkspaceFingerprint: String? = nil
    ) {
        self.baseURL = baseURL
        self.sessionId = sessionId
        self.skipInitial = skipInitial
        self.knownWorkspaceFingerprint = knownWorkspaceFingerprint
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

    func clockCalibration() -> ClockCalibration {
        ClockCalibration(
            skewMs: serverClockSkewMs,
            rttMs: clockSyncRttMs,
            uncertaintyMs: clockSyncUncertaintyMs,
            sampleCount: clockSyncSampleCount
        )
    }

    /// Starts the stream and returns an AsyncStream for events. Must be
    /// called at most once per instance. Subsequent calls return an empty
    /// stream so early events cannot be lost to a continuation-attach race.
    func start() -> AsyncStream<Event> {
        if task != nil {
            return AsyncStream { $0.finish() }
        }
        return AsyncStream { continuation in
            self.continuation = continuation
            self.clockCalibrationTask = Task { [weak self] in
                await self?.calibrateClock()
            }
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
        clockCalibrationTask?.cancel()
        clockCalibrationTask = nil
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
        guard clockSyncRttMs == nil else { return }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        serverClockSkewMs = nowMs - serverNowMs
    }

    private func calibrateClock(rounds: Int = 5) async {
        let url = baseURL.appendingPathComponent("/api/telemetry/clock")
        for _ in 0..<rounds {
            if Task.isCancelled { return }
            var request = URLRequest(url: url)
            request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
            let clientSentAtMs = Int64(Date().timeIntervalSince1970 * 1000)
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                let clientReceivedAtMs = Int64(Date().timeIntervalSince1970 * 1000)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { continue }
                let sample = try JSONDecoder().decode(ClockSyncResponse.self, from: data)
                recordClockSample(
                    clientSentAtMs: clientSentAtMs,
                    serverReceivedAtMs: sample.server_received_at_ms,
                    serverSentAtMs: sample.server_sent_at_ms,
                    clientReceivedAtMs: clientReceivedAtMs
                )
            } catch {
                continue
            }
        }
    }

    private func recordClockSample(
        clientSentAtMs t0: Int64,
        serverReceivedAtMs t1: Int64,
        serverSentAtMs t2: Int64,
        clientReceivedAtMs t3: Int64
    ) {
        guard t3 >= t0, t2 >= t1 else { return }
        let rttMs = Int(max(0, t3 - t0 - (t2 - t1)))
        let clientAheadMs = ((t0 - t1) + (t3 - t2)) / 2
        clockSyncSampleCount += 1
        if clockSyncRttMs == nil || rttMs < clockSyncRttMs! {
            clockSyncRttMs = rttMs
            clockSyncUncertaintyMs = (rttMs + 1) / 2
            serverClockSkewMs = clientAheadMs
        }
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
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if lastEventId > 0 {
            req.addValue(String(lastEventId), forHTTPHeaderField: "Last-Event-ID")
        }
        if let authorizationHeader = SharedAuthStore.authorizationHeader(for: baseURL.absoluteString) {
            req.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
        } else if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
            req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }
        req.assumesHTTP3Capable = false
        // waitsForConnectivity: the URLSession waits during transient network
        // unavailability (cell→wifi transitions) instead of failing fast.
        // Keep the long-lived SSE lane off the shared Alt-Svc cache. The
        // hosted edge can advertise HTTP/3 for ordinary requests while its
        // streaming response remains buffered; an ephemeral session starts
        // this connection on the reliable HTTP/2 path instead.
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest = 600
        config.timeoutIntervalForResource = 3600
        let transport = AsyncThrowingStream<SessionStreamTransportEvent, Error>.makeStream()
        let delegate = SessionStreamDataDelegate(continuation: transport.continuation)
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        let dataTask = session.dataTask(with: req)
        defer {
            dataTask.cancel()
            session.invalidateAndCancel()
            transport.continuation.finish()
        }

        logger.debug("workspace stream request started session=\(self.sessionId, privacy: .public) since_seq=\(self.lastEventId, privacy: .public) skip_initial=\(self.skipInitial, privacy: .public)")
        dataTask.resume()
        var responseAccepted = false
        var bytesBuffer = Data()
        var eventName = ""
        var eventId: String? = nil
        var dataBuffer = ""

        for try await transportEvent in transport.stream {
            if Task.isCancelled { break }
            switch transportEvent {
            case .response(let http):
                if http.statusCode == 401 {
                    logger.info("workspace stream unauthorized session=\(self.sessionId, privacy: .public)")
                    throw UnauthorizedError()
                }
                guard http.statusCode == 200 else {
                    logger.error("workspace stream non-200 session=\(self.sessionId, privacy: .public) status=\(http.statusCode, privacy: .public)")
                    throw URLError(.badServerResponse)
                }
                responseAccepted = true
                logger.debug("workspace stream response connected session=\(self.sessionId, privacy: .public)")
            case .data(let data):
                guard responseAccepted else { continue }
                bytesBuffer.append(data)
                while let newline = bytesBuffer.firstIndex(of: 0x0A) {
                    var lineData = bytesBuffer[..<newline]
                    bytesBuffer.removeSubrange(...newline)
                    if lineData.last == 0x0D { lineData = lineData.dropLast() }
                    let line = String(decoding: lineData, as: UTF8.self)
                    if line.isEmpty {
                        await self.dispatch(eventName: eventName, eventId: eventId, payload: dataBuffer)
                        eventName = ""
                        eventId = nil
                        dataBuffer = ""
                        continue
                    }
                    if line.hasPrefix(":") {
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
