import Foundation
import OSLog

/// Realtime push for the timeline list. Mirrors web's
/// `useTimelineSessionStream` and the backend
/// `/api/timeline/sessions/stream` contract.
///
/// Differences from `SessionWorkspaceStream`:
/// - The server does not emit `id:` on this stream. There is no
///   Last-Event-ID replay; reconnect is a fresh subscription. Callers
///   must run a REST bootstrap on `connected` events to resync state
///   (the first connect is preceded by the REST `load()` already).
/// - 401 is terminal: the actor stops reconnecting and emits
///   `disconnected(.notAuthenticated)` once. Caller should surface a
///   re-auth UI rather than silently looping.
/// - A stale-stream watchdog forces reconnect if no event of any kind
///   (heartbeat or otherwise) arrives within `staleTimeoutSeconds`.
///   Server heartbeats every 30s, so 45s is a safe floor.
actor TimelineSessionsStream {
    enum Event: Sendable {
        case connected
        case upsert(card: APITimelineSessionCardResponse, total: Int?, hasRealSessions: Bool?)
        case remove(threadId: String, total: Int?, hasRealSessions: Bool?)
        case heartbeat
        case disconnected(Error?)
    }

    struct UpsertPayload: Decodable, Sendable {
        let session: APITimelineSessionCardResponse
        let total: Int?
        let hasRealSessions: Bool?
    }

    struct RemovePayload: Decodable, Sendable {
        let threadId: String
        let total: Int?
        let hasRealSessions: Bool?
    }

    private let baseURL: URL
    private let daysBack: Int
    private let limit: Int
    private let skipInitialReplay: Bool
    private let staleTimeoutSeconds: TimeInterval
    private var task: Task<Void, Never>?
    private var continuation: AsyncStream<Event>.Continuation?
    private var lastEventAt: Date = Date()
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "TimelineStream")

    init(
        baseURL: URL,
        daysBack: Int = 14,
        limit: Int = 40,
        skipInitialReplay: Bool = true,
        staleTimeoutSeconds: TimeInterval = 45
    ) {
        self.baseURL = baseURL
        self.daysBack = daysBack
        self.limit = limit
        self.skipInitialReplay = skipInitialReplay
        self.staleTimeoutSeconds = staleTimeoutSeconds
    }

    static func streamURL(baseURL: URL, daysBack: Int, limit: Int, skipInitialReplay: Bool) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/stream"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "days_back", value: String(daysBack)),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "skip_initial_replay", value: skipInitialReplay ? "true" : "false"),
        ]
        return components.url!
    }

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
                    } catch LonghouseAPIError.notAuthenticated {
                        await self.emit(.disconnected(LonghouseAPIError.notAuthenticated))
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

    private func finishContinuation() {
        continuation?.finish()
        continuation = nil
    }

    private func openAndDrain() async throws {
        let url = Self.streamURL(
            baseURL: baseURL,
            daysBack: daysBack,
            limit: limit,
            skipInitialReplay: skipInitialReplay
        )
        var req = URLRequest(url: url)
        req.addValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.addValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
            req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }
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
            throw LonghouseAPIError.notAuthenticated
        }
        guard http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }

        lastEventAt = Date()
        let watchdog = Task { [weak self, staleTimeoutSeconds] in
            // Force-close the URLSession if no event arrives for the
            // stale timeout. Catches dead-but-open TCP connections.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(staleTimeoutSeconds * 1_000_000_000))
                guard let self else { break }
                if await self.staleSinceLastEvent() {
                    session.invalidateAndCancel()
                    break
                }
            }
        }
        defer { watchdog.cancel() }

        var eventName = ""
        var dataBuffer = ""

        for try await line in bytes.lines {
            if Task.isCancelled { break }
            if line.isEmpty {
                await dispatch(eventName: eventName, payload: dataBuffer)
                eventName = ""
                dataBuffer = ""
                continue
            }
            if line.hasPrefix(":") { continue }
            if let sep = line.firstIndex(of: ":") {
                let field = String(line[..<sep])
                var value = String(line[line.index(after: sep)...])
                if value.hasPrefix(" ") { value.removeFirst() }
                switch field {
                case "event": eventName = value
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

    private func staleSinceLastEvent() -> Bool {
        Date().timeIntervalSince(lastEventAt) >= staleTimeoutSeconds
    }

    private func dispatch(eventName: String, payload: String) async {
        guard let data = payload.data(using: .utf8) else { return }
        switch eventName {
        case "connected":
            emit(.connected)
        case "session_upsert":
            do {
                let parsed = try JSONDecoder.snakeCase.decode(UpsertPayload.self, from: data)
                emit(.upsert(card: parsed.session, total: parsed.total, hasRealSessions: parsed.hasRealSessions))
            } catch {
                logger.error("decode session_upsert failed: \(error.localizedDescription, privacy: .public)")
            }
        case "session_remove":
            do {
                let parsed = try JSONDecoder.snakeCase.decode(RemovePayload.self, from: data)
                emit(.remove(threadId: parsed.threadId, total: parsed.total, hasRealSessions: parsed.hasRealSessions))
            } catch {
                logger.error("decode session_remove failed: \(error.localizedDescription, privacy: .public)")
            }
        case "heartbeat":
            emit(.heartbeat)
        default:
            break
        }
    }
}
