import Foundation

struct LonghouseAPI: Sendable {
    let baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    init(host: String) {
        self.init(baseURL: URL(string: host)!)
    }

    func sessionsNeedingAttention() async throws -> [SessionSummary] {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "days_back", value: "14"),
            URLQueryItem(name: "limit", value: "20"),
        ]
        request.url = components.url

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

    func refreshSession() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/auth/refresh"))
        request.httpMethod = "POST"

        let (_, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    private func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        var request = request
        if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
            request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LonghouseAPIError.requestFailed
        }
        persistResponseCookies(from: httpResponse, requestURL: request.url)
        return (data, httpResponse)
    }

    private func persistResponseCookies(from response: HTTPURLResponse, requestURL: URL?) {
        guard let requestURL else {
            return
        }

        let headerFields = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
            guard let key = entry.key as? String else {
                return
            }
            result[key] = String(describing: entry.value)
        }

        let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: requestURL)
        guard !cookies.isEmpty else {
            return
        }
        SharedAuthStore.setManagedCookies(cookies, for: baseURL.absoluteString)
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
