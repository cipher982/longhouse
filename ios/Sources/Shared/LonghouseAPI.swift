import Foundation

struct LonghouseAPI: Sendable {
    let baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    init(host: String) {
        self.baseURL = URL(string: host)!
    }

    func sessionsNeedingAttention(authToken: String) async throws -> [SessionSummary] {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue(authToken, forHTTPHeaderField: "Cookie")

        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "days_back", value: "14"),
            URLQueryItem(name: "limit", value: "20"),
        ]
        request.url = components.url

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.requestFailed
        }

        let decoded = try JSONDecoder.snakeCase.decode(SessionsResponse.self, from: data)
        let heads = decoded.sessions.map(\.head)
        let actionable = heads.filter { ($0.presenceState == "needs_user" || $0.presenceState == "blocked") && $0.userState == "active" }
        return actionable.map { session in
            SessionSummary(
                id: session.id,
                title: session.summaryTitle ?? session.summary ?? session.provider ?? "Session",
                presenceState: session.presenceState ?? "unknown",
                provider: session.provider,
                project: session.project,
                lastActivityAt: session.lastActivityAt
            )
        }
    }

    func continueSession(_ sessionId: String, message: String, authToken: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(sessionId)/chat/send"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue(authToken, forHTTPHeaderField: "Cookie")
        request.httpBody = try JSONEncoder().encode(["message": message])

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.requestFailed
        }
    }

    func snoozeSession(_ sessionId: String, authToken: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/agents/sessions/\(sessionId)/action"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue(authToken, forHTTPHeaderField: "Cookie")
        request.httpBody = try JSONEncoder().encode(["action": "snooze"])

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.requestFailed
        }
    }
}

enum LonghouseAPIError: Error {
    case requestFailed
    case notAuthenticated
}

extension JSONDecoder {
    static let snakeCase: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
