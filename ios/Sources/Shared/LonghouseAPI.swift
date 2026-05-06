import Foundation

/// Client-side realtime latency beacon.
///
/// Measures provider-emitted → iOS-rendered latency. Posts fire-and-forget
/// to /api/telemetry/client-render. Idempotent per event_id so rapid
/// re-renders do not double-count.
actor RenderBeaconReporter {
    static let shared = RenderBeaconReporter()

    struct Payload: Encodable, Sendable {
        let event_id: String
        let session_id: String?
        let surface: String
        let managed: Bool
        let emitted_at_ms: Int64
        let rendered_at_ms: Int64
        let clock_skew_ms: Int
    }

    private var lastBeaconedEventId: String?

    func payload(
        sessionId: String,
        latestEventId: String,
        emittedAt: Date,
        managed: Bool
    ) -> Payload? {
        if lastBeaconedEventId == latestEventId { return nil }
        lastBeaconedEventId = latestEventId
        return Payload(
            event_id: latestEventId,
            session_id: sessionId,
            surface: "ios",
            managed: managed,
            emitted_at_ms: Int64(emittedAt.timeIntervalSince1970 * 1000),
            rendered_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            clock_skew_ms: 0
        )
    }
}

protocol SessionWorkspaceClient: Sendable {
    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse
    func sendInput(id: String, text: String, intent: String) async throws -> SessionInputResponse
    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse
    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse
    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async
}

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
        return decoded.sessions.map(\.sessionSummary)
    }

    static func sessionWorkspaceURL(baseURL: URL, id: String, limit: Int = 200, branchMode: String = "head") -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/workspace"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "branch_mode", value: branchMode),
        ]
        return components.url!
    }

    func sessionWorkspace(id: String, limit: Int = 200, branchMode: String = "head") async throws -> SessionWorkspaceResponse {
        var request = URLRequest(url: Self.sessionWorkspaceURL(baseURL: baseURL, id: id, limit: limit, branchMode: branchMode))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionWorkspaceResponse.self, from: data)
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

    /// Posts user input with server-decided outcome. When the session is idle
    /// the input dispatches immediately (`outcome == .sent`). When it's
    /// working, the row is durably queued and auto-drains at the next safe
    /// turn boundary (`outcome == .queued`).
    ///
    /// For `intent == "steer"` the server may return a structured 409 with
    /// `error_code: "turn_ended"` when the active turn ended between the
    /// UI's capability check and dispatch. That surfaces as
    /// `LonghouseAPIError.structured(...)` so the caller can offer a
    /// "Queue instead" action instead of silently converting the intent.
    func sendInput(id: String, text: String, intent: String = "auto") async throws -> SessionInputResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/input"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text, "intent": intent])

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionInputResponse.self, from: data)
    }

    /// Extract `{"detail": {"error_code": ..., "message": ...}}` from an
    /// HTTPException body. Returns nil when the body isn't structured,
    /// letting callers fall back to the generic `LonghouseAPIError.from(...)`.
    static func parseStructuredError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        guard
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let detail = obj["detail"] as? [String: Any],
            let code = detail["error_code"] as? String
        else {
            return nil
        }
        let message = (detail["message"] as? String) ?? ""
        return .structured(status: statusCode, errorCode: code, message: message)
    }

    func draftReply(id: String, maxChars: Int = 1200) async throws -> DraftReplyResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/draft-reply"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["max_chars": maxChars])

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(DraftReplyResponse.self, from: data)
    }

    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/loop-mode"))
        request.httpMethod = "PATCH"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["loop_mode": loopMode.rawValue])

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(LoopModeResponse.self, from: data)
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

    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/telemetry/client-render"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        guard let body = try? JSONEncoder().encode(payload) else { return }
        request.httpBody = body
        _ = try? await data(for: request)
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

extension LonghouseAPI: SessionWorkspaceClient {}

enum LonghouseAPIError: Error {
    case requestFailed
    case notAuthenticated
    case conflict
    case serviceUnavailable
    case upstreamFailed
    /// Server returned a structured error payload (e.g. `{"detail": {"error_code": "turn_ended"}}`).
    /// Carries status + code + message so the caller can branch on the
    /// semantic outcome instead of parsing ad-hoc strings.
    case structured(status: Int, errorCode: String, message: String)

    static func from(statusCode: Int) -> LonghouseAPIError {
        switch statusCode {
        case 401:
            return .notAuthenticated
        case 409:
            return .conflict
        case 502:
            return .upstreamFailed
        case 503:
            return .serviceUnavailable
        default:
            return .requestFailed
        }
    }
}

extension LonghouseAPIError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .requestFailed:
            return "Request failed."
        case .notAuthenticated:
            return "Session expired."
        case .conflict:
            return "Session is busy. Try again in a moment."
        case .serviceUnavailable:
            return "Service is not configured yet."
        case .upstreamFailed:
            return "Generation failed. Try again."
        case .structured(_, _, let message):
            return message.isEmpty ? "Request was rejected." : message
        }
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
