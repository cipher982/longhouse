import Foundation

struct LonghouseAPI: Sendable {
    let baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    init?(host: String) {
        guard let url = URL(string: host) else { return nil }
        self.init(baseURL: url)
    }

    func sessionsNeedingAttention() async throws -> [SessionSummary] {
        var components = URLComponents(url: baseURL.appendingPathComponent("/api/timeline/sessions"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "days_back", value: "14"),
            URLQueryItem(name: "limit", value: "20"),
        ]
        var request = URLRequest(url: components.url!)
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
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

    func recentSessions(limit: Int = 30) async throws -> [SessionSummary] {
        var components = URLComponents(url: baseURL.appendingPathComponent("/api/timeline/sessions"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "days_back", value: "14"),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        var request = URLRequest(url: components.url!)
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }

        let decoded = try JSONDecoder.snakeCase.decode(SessionsResponse.self, from: data)
        return decoded.sessions.map(\.head).map { session in
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

    func sessionDetail(id: String) async throws -> SessionDetail {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionDetail.self, from: data)
    }

    func sessionEvents(id: String, limit: Int = 200) async throws -> [SessionEvent] {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/events"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "branch_mode", value: "head"),
        ]
        var request = URLRequest(url: components.url!)
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decoded = try JSONDecoder.snakeCase.decode(SessionEventsResponse.self, from: data)
        return decoded.events
    }

    func sessionTurns(id: String) async throws -> [SessionTurn] {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/turns"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decoded = try JSONDecoder.snakeCase.decode(SessionTurnsResponse.self, from: data)
        return decoded.turns
    }

    func sendLive(id: String, text: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/send-live"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["message": text])

        let (_, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    func sessionAction(id: String, action: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/action"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["action": action])

        let (_, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    func refreshSession() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/auth/refresh"))
        request.httpMethod = "POST"

        let (_, httpResponse) = try await data(for: request, allowRetry: false)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    private func data(for request: URLRequest, allowRetry: Bool = true) async throws -> (Data, HTTPURLResponse) {
        var request = request
        request.timeoutInterval = 15
        // Explicit cookie injection: widget extension runs in a separate process
        // without shared HTTPCookieStorage, so it must read from the keychain.
        if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
            request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LonghouseAPIError.requestFailed
        }
        persistResponseCookies(from: httpResponse, requestURL: request.url)

        if httpResponse.statusCode == 401 && allowRetry {
            do {
                try await refreshSession()
                return try await self.data(for: request, allowRetry: false)
            } catch {
                throw LonghouseAPIError.notAuthenticated
            }
        }
        return (data, httpResponse)
    }

    private func persistResponseCookies(from response: HTTPURLResponse, requestURL: URL?) {
        guard let requestURL else { return }
        let headerFields = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
            guard let key = entry.key as? String else { return }
            result[key] = String(describing: entry.value)
        }
        let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: requestURL)
        guard !cookies.isEmpty else { return }
        SharedAuthStore.setManagedCookies(cookies, for: baseURL.absoluteString)
        // Also mirror to HTTPCookieStorage.shared for the main app process.
        for cookie in cookies where SharedAuthStore.managedCookieNames.contains(cookie.name) {
            HTTPCookieStorage.shared.setCookie(cookie)
        }
    }
}

enum LonghouseAPIError: Error {
    case requestFailed
    case notAuthenticated

    static func from(statusCode: Int) -> LonghouseAPIError {
        statusCode == 401 ? .notAuthenticated : .requestFailed
    }
}

extension JSONDecoder {
    static let snakeCase: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
