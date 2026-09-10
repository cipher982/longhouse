import Foundation
import OSLog

struct SessionProjection: Sendable {
    let sessionId: String
    let timelineTitle: String?
    let summaryTitle: String?
    let firstUserMessage: String?
    let titleState: String?
    let titleSource: String?
    let runtimePhase: String?
    let displayPhase: String?
    let lastActivityAt: String?
    let source: String
    let authority: String?
    let stateContractVersion: Int?
    let presentationPolicyVersion: Int?
    let commitSeq: String?
    let mode: String?
    let presentation: SessionPresentationSnapshot?
    let activity: SessionActivitySnapshot?
    let control: SessionControlSnapshot?

    var activityObservedAt: String? { activity?.observedAt }

    init(
        sessionId: String,
        timelineTitle: String?,
        summaryTitle: String?,
        firstUserMessage: String?,
        titleState: String?,
        titleSource: String?,
        runtimePhase: String?,
        displayPhase: String?,
        lastActivityAt: String?,
        source: String,
        authority: String? = nil,
        stateContractVersion: Int? = nil,
        presentationPolicyVersion: Int? = nil,
        commitSeq: String? = nil,
        mode: String? = nil,
        presentation: SessionPresentationSnapshot? = nil,
        activity: SessionActivitySnapshot? = nil,
        control: SessionControlSnapshot? = nil
    ) {
        self.sessionId = sessionId
        self.timelineTitle = timelineTitle
        self.summaryTitle = summaryTitle
        self.firstUserMessage = firstUserMessage
        self.titleState = titleState
        self.titleSource = titleSource
        self.runtimePhase = runtimePhase
        self.displayPhase = displayPhase
        self.lastActivityAt = lastActivityAt
        self.source = source
        self.authority = authority
        self.stateContractVersion = stateContractVersion
        self.presentationPolicyVersion = presentationPolicyVersion
        self.commitSeq = commitSeq
        self.mode = mode
        self.presentation = presentation
        self.activity = activity
        self.control = control
    }
}

enum SessionProjectionEvent: Sendable {
    case delta(SessionProjection)
    case remove(sessionId: String)
    /// The SSE connection itself is established. Emitted from `drain` after the
    /// 200, so it cannot claim authority the stream has not actually acquired.
    case connected
    /// Coalesced liveness evidence with no payload. Renews the projection lease
    /// so a healthy idle stream does not expire without publishing UI state for
    /// every line of a chatty SSE connection.
    case alive
    /// The stream failed and is backing off. Emitted on every failed attempt so
    /// the store can stop presenting Runtime Host projection as current — the
    /// stream retries forever, so silence here is indistinguishable from health.
    case failed(String)
}

/// Records whether a single stream connection ever carried traffic, so an
/// ordinary server-initiated close can be told apart from a connection that
/// never worked.
actor LivenessFlag {
    private(set) var observed = false

    func markObserved() {
        observed = true
    }
}

/// Bounds how often transport liveness becomes observable UI state.
///
/// The stream can deliver hundreds of protocol lines per second while agents
/// are active. Publishing a fresh timestamp for each line invalidates the
/// entire SwiftUI panel even while it is hidden. Ten seconds is comfortably
/// inside the five-minute projection authority lease.
struct ProjectionLeasePulse {
    static let interval: TimeInterval = 10

    private var lastEmittedAt: Date?

    init(lastEmittedAt: Date? = nil) {
        self.lastEmittedAt = lastEmittedAt
    }

    mutating func shouldEmit(at date: Date) -> Bool {
        if let lastEmittedAt,
           date.timeIntervalSince(lastEmittedAt) < Self.interval {
            return false
        }
        lastEmittedAt = date
        return true
    }
}

/// Decides whether a dropped stream connection should be reported to the user.
///
/// Pulled out of the retry loop so the sequence can be tested without a network.
/// The subtlety it exists to get right: a grace that is replenished by the same
/// connection it forgives is not a grace at all — a stream that emits one line
/// and closes, over and over, would suppress its own failures forever.
struct ProjectionRetryPolicy {
    /// A connection that carried traffic for at least this long is a healthy
    /// session whose close is ordinary rather than evidence of a problem.
    static let healthySessionSeconds: TimeInterval = 30

    private(set) var consecutiveFailures = 0

    enum Outcome: Equatable {
        /// Ordinary close of a working stream. Stay quiet, retry promptly.
        case retryQuietly
        /// Report the failure; the stream is not working.
        case reportFailure
    }

    /// - Parameters:
    ///   - carriedTraffic: whether this connection ever delivered a line.
    ///   - duration: how long the connection lasted before it dropped.
    /// - Returns: whether to surface the drop, and whether to reset the backoff.
    mutating func recordDrop(
        carriedTraffic: Bool,
        duration: TimeInterval
    ) -> (outcome: Outcome, resetBackoff: Bool) {
        if carriedTraffic && duration >= Self.healthySessionSeconds {
            // A stream that ran for a while and then closed is doing what
            // long-lived SSE connections normally do.
            consecutiveFailures = 0
            return (.retryQuietly, true)
        }

        consecutiveFailures += 1
        // One forgiven drop per streak, and only if the connection worked at
        // all. The counter is never reset here, so a flapping stream reports on
        // its second consecutive drop.
        if carriedTraffic && consecutiveFailures == 1 {
            return (.retryQuietly, true)
        }
        return (.reportFailure, false)
    }
}

enum SessionProjectionStream {
    private static let logger = Logger(
        subsystem: "ai.longhouse.desktop",
        category: "session-projection-stream"
    )

    private actor StreamIdleDeadline {
        let timeoutSeconds: TimeInterval
        let sleepDuration: Duration
        private var lastFrameAt = Date()

        init(timeout: TimeInterval) {
            timeoutSeconds = timeout
            sleepDuration = .milliseconds(max(1, Int(timeout * 1_000)))
        }

        func markFrame(at date: Date = Date()) {
            lastFrameAt = date
        }

        func hasExpired(at date: Date = Date()) -> Bool {
            date.timeIntervalSince(lastFrameAt) >= timeoutSeconds
        }
    }

    private struct Delta: Decodable {
        let sessionId: String
        let timelineTitle: String?
        let summaryTitle: String?
        let firstUserMessage: String?
        let titleState: String?
        let titleSource: String?
        let runtimePhase: String?
        let displayPhase: String?
        let lastActivityAt: String?
        let source: String
        let authority: String?
        let stateContractVersion: Int?
        let presentationPolicyVersion: Int?
        let commitSeq: String?
        let mode: String?
        let presentation: SessionPresentationSnapshot?
        let activity: SessionActivitySnapshot?
        let control: SessionControlSnapshot?
    }

    private struct Remove: Decodable { let sessionId: String }

    struct SSELineDecoder {
        private var buffer: [UInt8] = []

        mutating func append(_ byte: UInt8) -> String? {
            guard byte == 0x0A else {
                buffer.append(byte)
                return nil
            }
            if buffer.last == 0x0D {
                buffer.removeLast()
            }
            let line = String(decoding: buffer, as: UTF8.self)
            buffer.removeAll(keepingCapacity: true)
            return line
        }
    }

    /// - Parameter session: Optional caller-owned URLSession override. A
    ///   supplied session must be dedicated to this stream because the stale
    ///   watchdog cancels every task on it.
    static func projections(
        connection: RealtimeConnectionSnapshot,
        sessionIds: [String],
        session: URLSession? = nil,
        idleTimeout: TimeInterval = 45
    ) -> AsyncStream<SessionProjectionEvent> {
        let ownsSession = session == nil
        let session = session ?? makeStreamSession()
        return AsyncStream { continuation in
            continuation.onTermination = { _ in
                // The consumer stopped. The producer's own defer only runs once
                // its drain actually unwinds, and a stalled body read is exactly
                // what this stream is built to survive, so tear the owned
                // session down from here as well: `finishTasksAndInvalidate` in
                // the defer is then a no-op.
                if ownsSession {
                    session.invalidateAndCancel()
                }
            }
            let task = Task.detached(priority: .userInitiated) {
                defer {
                    if ownsSession {
                        session.finishTasksAndInvalidate()
                    }
                }
                var backoff = Duration.milliseconds(250)
                let allowedSessionIds = Set(sessionIds)
                var retryPolicy = ProjectionRetryPolicy()
                while !Task.isCancelled {
                    let liveness = LivenessFlag()
                    let connectionStartedAt = Date()
                    do {
                        try await drain(
                            connection: connection,
                            session: session,
                            allowedSessionIds: allowedSessionIds,
                            continuation: continuation,
                            liveness: liveness,
                            idleTimeout: idleTimeout
                        )
                    } catch is CancellationError {
                        break
                    } catch {
                        let description = String(describing: error)
                        logger.error("Runtime Host session stream failed: \(description, privacy: .public)")

                        let decision = retryPolicy.recordDrop(
                            carriedTraffic: await liveness.observed,
                            duration: Date().timeIntervalSince(connectionStartedAt)
                        )
                        if decision.resetBackoff {
                            backoff = .milliseconds(250)
                        }
                        if decision.outcome == .reportFailure {
                            continuation.yield(.failed(description))
                        }
                        try? await Task.sleep(for: backoff)
                        backoff = min(backoff * 2, .seconds(10))
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func makeStreamSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        // Runtime Host emits heartbeat frames well inside 45 seconds; matching
        // the iOS stream's deadline avoids firing on a healthy connection.
        configuration.timeoutIntervalForRequest = 45
        return URLSession(configuration: configuration)
    }

    private static func drain(
        connection: RealtimeConnectionSnapshot,
        session: URLSession,
        allowedSessionIds: Set<String>,
        continuation: AsyncStream<SessionProjectionEvent>.Continuation,
        liveness: LivenessFlag,
        idleTimeout: TimeInterval
    ) async throws {
        guard let rawURL = connection.runtimeUrl,
              let baseURL = URL(string: rawURL),
              let tokenPath = connection.tokenPath
        else { throw URLError(.badURL) }
        let token = try String(contentsOfFile: tokenPath, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { throw URLError(.userAuthenticationRequired) }

        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/agents/sessions/stream"),
            resolvingAgainstBaseURL: false
        )!
        var queryItems = [
            URLQueryItem(name: "limit", value: "100"),
            URLQueryItem(name: "skip_initial_replay", value: "false"),
        ]
        if let machineName = connection.machineName {
            queryItems.append(URLQueryItem(name: "device_id", value: machineName))
        }
        components.queryItems = queryItems
        var request = URLRequest(url: components.url!)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(token, forHTTPHeaderField: "X-Agents-Token")

        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        logger.info("Runtime Host session stream connected")
        // Only now is the stream genuinely established. Its bounded initial
        // replay hydrates the known sessions before live deltas continue on
        // the same connection, avoiding a second per-session read path.
        continuation.yield(.connected)
        let deadline = StreamIdleDeadline(timeout: idleTimeout)
        let watchdog = Task {
            do {
                while !Task.isCancelled {
                    try await Task.sleep(for: deadline.sleepDuration)
                    if await deadline.hasExpired() {
                        logger.info("Runtime Host session stream stalled; forcing reconnect")
                        session.getAllTasks { tasks in
                            for task in tasks { task.cancel() }
                        }
                        break
                    }
                }
            } catch is CancellationError {
                // The stream ended or the consumer cancelled before stalling.
            } catch {
                // A watchdog cancellation is advisory; the byte loop owns the
                // stream error reported to the existing retry path.
            }
        }
        defer { watchdog.cancel() }

        var eventName = ""
        var dataLines: [String] = []
        var lineDecoder = SSELineDecoder()
        var leasePulse = ProjectionLeasePulse(lastEmittedAt: Date())
        for try await byte in bytes {
            guard let line = lineDecoder.append(byte) else { continue }
            // Every complete line from the peer is liveness, keepalive
            // comments included: the deadline asks whether the connection is
            // still delivering, not whether the parser found anything in it.
            // The `.alive` pulses this code emits itself are the opposite case
            // and deliberately never reset it.
            await deadline.markFrame()
            await liveness.markObserved()
            if leasePulse.shouldEmit(at: Date()) {
                continuation.yield(.alive)
            }
            if line.isEmpty {
                if eventName == "session_delta", !dataLines.isEmpty {
                    let data = Data(dataLines.joined(separator: "\n").utf8)
                    let decoder = JSONDecoder()
                    decoder.keyDecodingStrategy = .convertFromSnakeCase
                    let delta = try decoder.decode(Delta.self, from: data)
                    guard allowedSessionIds.contains(delta.sessionId) else {
                        eventName = ""
                        dataLines.removeAll(keepingCapacity: true)
                        continue
                    }
                    continuation.yield(
                        .delta(SessionProjection(
                            sessionId: delta.sessionId,
                            timelineTitle: delta.timelineTitle,
                            summaryTitle: nil,
                            firstUserMessage: nil,
                            titleState: delta.titleState,
                            titleSource: delta.titleSource,
                            runtimePhase: delta.runtimePhase,
                            displayPhase: delta.displayPhase,
                            lastActivityAt: delta.lastActivityAt,
                            source: delta.source,
                            authority: delta.authority,
                            stateContractVersion: delta.stateContractVersion,
                            presentationPolicyVersion: delta.presentationPolicyVersion,
                            commitSeq: delta.commitSeq,
                            mode: delta.mode,
                            presentation: delta.presentation,
                            activity: delta.activity,
                            control: delta.control
                        ))
                    )
                } else if eventName == "session_remove", !dataLines.isEmpty {
                    let data = Data(dataLines.joined(separator: "\n").utf8)
                    let decoder = JSONDecoder()
                    decoder.keyDecodingStrategy = .convertFromSnakeCase
                    let sessionId = try decoder.decode(Remove.self, from: data).sessionId
                    if allowedSessionIds.contains(sessionId) {
                        continuation.yield(.remove(sessionId: sessionId))
                    }
                }
                eventName = ""
                dataLines.removeAll(keepingCapacity: true)
            } else if line.hasPrefix("event:") {
                eventName = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                dataLines.append(line.dropFirst(5).trimmingCharacters(in: .whitespaces))
            }
        }

        // Reaching here means the server closed the stream. Returning normally
        // would let the retry loop reconnect and renew the authority lease on
        // every cycle, so a server that accepts and immediately hangs up would
        // look permanently healthy.
        throw URLError(.networkConnectionLost)
    }

}
