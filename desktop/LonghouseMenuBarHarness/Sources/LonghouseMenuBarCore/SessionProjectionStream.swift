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
}

enum SessionProjectionStream {
    private struct UpsertEnvelope: Decodable {
        let session: Card
    }

    private struct Card: Decodable {
        let head: Head
    }

    private struct Head: Decodable {
        let id: String
        let timelineTitle: String?
        let summaryTitle: String?
        let firstUserMessage: String?
        let titleState: String?
        let titleSource: String?
        let runtimePhase: String?
        let displayPhase: String?
        let lastActivityAt: String?
    }

    static func projections(connection: RealtimeConnectionSnapshot) -> AsyncStream<SessionProjection> {
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
        continuation: AsyncStream<SessionProjection>.Continuation
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
        components.queryItems = [
            URLQueryItem(name: "limit", value: "40"),
            URLQueryItem(name: "skip_initial_replay", value: "false"),
        ]
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
                if eventName == "session_upsert", !dataLines.isEmpty {
                    let data = Data(dataLines.joined(separator: "\n").utf8)
                    let decoder = JSONDecoder()
                    decoder.keyDecodingStrategy = .convertFromSnakeCase
                    let payload = try decoder.decode(UpsertEnvelope.self, from: data)
                    let head = payload.session.head
                    continuation.yield(
                        SessionProjection(
                            sessionId: head.id,
                            timelineTitle: head.timelineTitle,
                            summaryTitle: head.summaryTitle,
                            firstUserMessage: head.firstUserMessage,
                            titleState: head.titleState,
                            titleSource: head.titleSource,
                            runtimePhase: head.runtimePhase,
                            displayPhase: head.displayPhase,
                            lastActivityAt: head.lastActivityAt
                        )
                    )
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
