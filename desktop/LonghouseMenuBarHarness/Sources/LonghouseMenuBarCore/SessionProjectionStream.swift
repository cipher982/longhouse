import Foundation

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
}

enum SessionProjectionStream {
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

    static func projections(connection: RealtimeConnectionSnapshot) -> AsyncStream<SessionProjectionEvent> {
        AsyncStream { continuation in
            let task = Task.detached(priority: .userInitiated) {
                var backoff = Duration.milliseconds(250)
                while !Task.isCancelled {
                    do {
                        try await drain(connection: connection, continuation: continuation)
                        backoff = .milliseconds(250)
                    } catch is CancellationError {
                        break
                    } catch {
                        try? await Task.sleep(for: backoff)
                        backoff = min(backoff * 2, .seconds(10))
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func drain(
        connection: RealtimeConnectionSnapshot,
        continuation: AsyncStream<SessionProjectionEvent>.Continuation
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
            URLQueryItem(name: "limit", value: "40"),
            URLQueryItem(name: "skip_initial_replay", value: "false"),
        ]
        if let machineName = connection.machineName {
            queryItems.append(URLQueryItem(name: "device_id", value: machineName))
        }
        components.queryItems = queryItems
        var request = URLRequest(url: components.url!)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(token, forHTTPHeaderField: "X-Agents-Token")

        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }

        var eventName = ""
        var dataLines: [String] = []
        for try await line in bytes.lines {
            if line.isEmpty {
                if eventName == "session_delta", !dataLines.isEmpty {
                    let data = Data(dataLines.joined(separator: "\n").utf8)
                    let decoder = JSONDecoder()
                    decoder.keyDecodingStrategy = .convertFromSnakeCase
                    let delta = try decoder.decode(Delta.self, from: data)
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
                    continuation.yield(.remove(sessionId: try decoder.decode(Remove.self, from: data).sessionId))
                }
                eventName = ""
                dataLines.removeAll(keepingCapacity: true)
            } else if line.hasPrefix("event:") {
                eventName = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                dataLines.append(line.dropFirst(5).trimmingCharacters(in: .whitespaces))
            }
        }
    }
}
