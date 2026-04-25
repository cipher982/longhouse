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
        try await timelineSessions(limit: 30).filter(\.needsAttention)
    }

    func recentSessions(limit: Int = 30) async throws -> [SessionSummary] {
        try await timelineSessions(limit: limit)
    }

    func recentActiveSessions(limit: Int = 30) async throws -> [SessionSummary] {
        try await timelineSessions(limit: limit).filter(\.isUserActive)
    }

    func timelineSessions(limit: Int = 30) async throws -> [SessionSummary] {
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
        return decoded.sessions.map { card in
            let session = card.head
            return SessionSummary(
                id: session.id,
                title: session.summaryTitle ?? session.summary ?? session.provider ?? "Session",
                presenceState: session.presenceState ?? "unknown",
                provider: session.provider,
                project: session.project,
                lastActivityAt: session.lastActivityAt,
                summary: session.summary,
                userState: session.userState,
                status: session.status,
                displayPhase: session.displayPhase,
                presenceTool: session.presenceTool,
                activeTool: session.activeTool,
                gitBranch: session.gitBranch,
                homeLabel: session.homeLabel,
                headOriginLabel: card.headOriginLabel,
                timelineAnchorAt: session.timelineAnchorAt,
                userMessages: session.userMessages,
                toolCalls: session.toolCalls,
                liveControlAvailable: session.capabilities?.liveControlAvailable,
                hostReattachAvailable: session.capabilities?.hostReattachAvailable,
                replyToLiveSessionAvailable: session.capabilities?.replyToLiveSessionAvailable
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

    func sessionEvents(id: String, limit: Int = 200, anchor: String = "tail") async throws -> [SessionEvent] {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/events"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "anchor", value: anchor),
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

    func notificationSettings() async throws -> UserNotificationSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/users/me/notifications"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(UserNotificationSettings.self, from: data)
    }

    func updateNotificationSettings(apnsEnabled: Bool) async throws -> UserNotificationSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/users/me/notifications"))
        request.httpMethod = "PATCH"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["apns_enabled": apnsEnabled])

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(UserNotificationSettings.self, from: data)
    }

    func registerAPNSDevice(
        deviceToken: String,
        pushEnvironment: String,
        appBuildId: String?,
        platform: String = "ios"
    ) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/devices/apns-register"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = [
            "device_token": deviceToken,
            "platform": platform,
            "push_environment": pushEnvironment,
        ]
        if let appBuildId, !appBuildId.isEmpty {
            body["app_build_id"] = appBuildId
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (_, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    func registerAPNSLiveActivity(
        sessionId: String,
        activityId: String,
        pushToken: String,
        pushEnvironment: String,
        appBuildId: String?
    ) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/devices/apns-live-activity/register"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = [
            "session_id": sessionId,
            "activity_id": activityId,
            "push_token": pushToken,
            "push_environment": pushEnvironment,
        ]
        if let appBuildId, !appBuildId.isEmpty {
            body["app_build_id"] = appBuildId
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (_, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    func endAPNSLiveActivity(activityId: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/devices/apns-live-activity/end"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["activity_id": activityId])

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

struct UserNotificationSettings: Decodable, Equatable {
    let apnsEnabled: Bool
}
