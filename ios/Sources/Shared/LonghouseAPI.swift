import Foundation
import OSLog

/// Client-side realtime latency beacon.
///
/// Measures provider-emitted → iOS-rendered latency. Posts fire-and-forget
/// to /api/telemetry/client-render. Idempotent per event_id so rapid
/// re-renders do not double-count.
actor RenderBeaconReporter {
    static let shared = RenderBeaconReporter()

    struct WebKitDiagnostics: Encodable, Equatable, Sendable {
        let stage: String
        let payload_byte_size: Int
        let row_count: Int
        let latest_item_id: String?
        let render_sequence: Int
        let js_failure_count: Int
        let should_stick_to_bottom: Bool
        let web_view_loaded: Bool
        let render_duration_ms: Int?
        let error_description: String?

        init(
            stage: String,
            payload_byte_size: Int,
            row_count: Int,
            latest_item_id: String?,
            render_sequence: Int,
            js_failure_count: Int,
            should_stick_to_bottom: Bool,
            web_view_loaded: Bool,
            render_duration_ms: Int? = nil,
            error_description: String?
        ) {
            self.stage = stage
            self.payload_byte_size = payload_byte_size
            self.row_count = row_count
            self.latest_item_id = latest_item_id
            self.render_sequence = render_sequence
            self.js_failure_count = js_failure_count
            self.should_stick_to_bottom = should_stick_to_bottom
            self.web_view_loaded = web_view_loaded
            self.render_duration_ms = render_duration_ms
            self.error_description = error_description
        }
    }

    struct Payload: Encodable, Sendable {
        let event_id: String
        let session_id: String?
        let surface: String
        let managed: Bool
        let emitted_at_ms: Int64
        let rendered_at_ms: Int64
        let clock_skew_ms: Int
        let server_fanout_at_ms: Int64?
        let client_received_at_ms: Int64?
        let pubsub_seq: Int?
        let webkit: WebKitDiagnostics?
    }

    private var lastBeaconKey: String?

    func payload(
        sessionId: String,
        latestEventId: String,
        emittedAt: Date,
        managed: Bool,
        clockSkewMs: Int = 0,
        serverFanoutAtMs: Int64? = nil,
        clientReceivedAtMs: Int64? = nil,
        pubsubSeq: Int? = nil,
        webkit: WebKitDiagnostics? = nil
    ) -> Payload? {
        let stage = webkit?.stage ?? "rendered"
        let beaconKey = "\(sessionId):\(latestEventId):\(stage)"
        if lastBeaconKey == beaconKey { return nil }
        lastBeaconKey = beaconKey
        return Payload(
            event_id: latestEventId,
            session_id: sessionId,
            surface: "ios",
            managed: managed,
            emitted_at_ms: Int64(emittedAt.timeIntervalSince1970 * 1000),
            rendered_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            clock_skew_ms: clockSkewMs,
            server_fanout_at_ms: serverFanoutAtMs,
            client_received_at_ms: clientReceivedAtMs,
            pubsub_seq: pubsubSeq,
            webkit: webkit
        )
    }
}

protocol SessionWorkspaceClient: Sendable {
    func sessionWorkspace(id: String, limit: Int, branchMode: String) async throws -> SessionWorkspaceResponse
    func sessionMobileTail(
        id: String,
        limit: Int,
        offset: Int,
        branchMode: String,
        snapshotEventId: Int?
    ) async throws -> SessionMobileTailResponse
    func sendInput(id: String, text: String, intent: String, clientRequestId: String?) async throws -> SessionInputResponse
    func sendInputMultipart(id: String, text: String, attachments: [ComposerAttachment], clientRequestId: String?) async throws -> SessionInputResponse
    func draftReply(id: String, maxChars: Int) async throws -> DraftReplyResponse
    func setSessionLoopMode(id: String, loopMode: SessionLoopMode) async throws -> LoopModeResponse
    func postRenderBeacon(_ payload: RenderBeaconReporter.Payload) async
}

struct LonghouseAPI: Sendable {
    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionOpen")

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

        let decoded = try JSONDecoder.snakeCase.decode(APITimelineSessionsListResponse.self, from: data)
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

    static func sessionMobileTailURL(
        baseURL: URL,
        id: String,
        limit: Int = 50,
        offset: Int = 0,
        branchMode: String = "head",
        snapshotEventId: Int? = nil
    ) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/mobile-tail"),
            resolvingAgainstBaseURL: false
        )!
        var items = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
            URLQueryItem(name: "branch_mode", value: branchMode),
        ]
        if let snapshotEventId {
            items.append(URLQueryItem(name: "snapshot_event_id", value: String(snapshotEventId)))
        }
        components.queryItems = items
        return components.url!
    }

    func sessionWorkspace(id: String, limit: Int = 200, branchMode: String = "head") async throws -> SessionWorkspaceResponse {
        var request = URLRequest(
            url: Self.sessionWorkspaceURL(baseURL: baseURL, id: id, limit: limit, branchMode: branchMode),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("no-cache", forHTTPHeaderField: "Cache-Control")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(APISessionWorkspaceResponse.self, from: data).sessionWorkspaceResponse
    }

    func sessionMobileTail(
        id: String,
        limit: Int = 50,
        offset: Int = 0,
        branchMode: String = "head",
        snapshotEventId: Int? = nil
    ) async throws -> SessionMobileTailResponse {
        var request = URLRequest(
            url: Self.sessionMobileTailURL(
                baseURL: baseURL,
                id: id,
                limit: limit,
                offset: offset,
                branchMode: branchMode,
                snapshotEventId: snapshotEventId
            ),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("no-cache", forHTTPHeaderField: "Cache-Control")

        let requestStartedAt = Date()
        Self.logger.info("mobile-tail request started session=\(id, privacy: .public) limit=\(limit, privacy: .public) offset=\(offset, privacy: .public)")
        let (data, httpResponse) = try await data(for: request)
        let responseMs = Int(Date().timeIntervalSince(requestStartedAt) * 1000)
        let contentEncoding = httpResponse.value(forHTTPHeaderField: "content-encoding") ?? "none"
        let wireContentLength = httpResponse.value(forHTTPHeaderField: "content-length") ?? "unknown"
        let serverTiming = httpResponse.value(forHTTPHeaderField: "server-timing") ?? "none"
        Self.logger.info("mobile-tail response received session=\(id, privacy: .public) status=\(httpResponse.statusCode, privacy: .public) decoded_bytes=\(data.count, privacy: .public) wire_content_length=\(wireContentLength, privacy: .public) encoding=\(contentEncoding, privacy: .public) elapsed_ms=\(responseMs, privacy: .public) server_timing=\(serverTiming, privacy: .public)")
        guard httpResponse.statusCode == 200 else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decodeStartedAt = Date()
        let decoded = try JSONDecoder.snakeCase.decode(SessionMobileTailResponse.self, from: data)
        let decodeMs = Int(Date().timeIntervalSince(decodeStartedAt) * 1000)
        Self.logger.info("mobile-tail decode finished session=\(id, privacy: .public) events=\(decoded.events.count, privacy: .public) total=\(decoded.projection.total, privacy: .public) elapsed_ms=\(decodeMs, privacy: .public)")
        return decoded
    }

    func sessionTurns(id: String) async throws -> [SessionTurn] {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/turns"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decoded = try JSONDecoder.snakeCase.decode(APISessionTurnsListResponse.self, from: data)
        return decoded.sessionTurnsResponse.turns
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
    func sendInput(
        id: String,
        text: String,
        intent: String = "auto",
        clientRequestId: String? = nil
    ) async throws -> SessionInputResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/input"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = ["text": text, "intent": intent]
        if let clientRequestId, !clientRequestId.isEmpty {
            body["client_request_id"] = clientRequestId
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(APISessionInputResponse.self, from: data).sessionInputResponse
    }

    /// Multipart POST for inputs that include image attachments. Server route
    /// only accepts `intent=auto` in v1 (steer/queue must use the JSON endpoint).
    func sendInputMultipart(
        id: String,
        text: String,
        attachments: [ComposerAttachment],
        clientRequestId: String? = nil
    ) async throws -> SessionInputResponse {
        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/inputs-multipart"))
        request.httpMethod = "POST"
        request.addValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("Longhouse-iOS", forHTTPHeaderField: "User-Agent")
        let body = Self.buildMultipartBody(
            boundary: boundary,
            text: text,
            intent: "auto",
            clientRequestId: clientRequestId,
            attachments: attachments,
        )
        request.httpBody = body
        let totalBytes = body.count
        let attachmentBytes = attachments.reduce(0) { $0 + $1.byteSize }
        let started = Date()

        let data: Data
        let httpResponse: HTTPURLResponse
        do {
            (data, httpResponse) = try await self.data(for: request)
        } catch {
            let elapsedMs = Int(Date().timeIntervalSince(started) * 1000)
            print(
                "[image-attach] ios upload transport_failed count=\(attachments.count) " +
                "attachment_bytes=\(attachmentBytes) total_bytes=\(totalBytes) elapsed_ms=\(elapsedMs)"
            )
            throw error
        }
        let elapsedMs = Int(Date().timeIntervalSince(started) * 1000)
        print(
            "[image-attach] ios upload count=\(attachments.count) " +
            "attachment_bytes=\(attachmentBytes) total_bytes=\(totalBytes) " +
            "status=\(httpResponse.statusCode) elapsed_ms=\(elapsedMs)"
        )
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(APISessionInputResponse.self, from: data).sessionInputResponse
    }

    static func buildMultipartBody(
        boundary: String,
        text: String,
        intent: String,
        clientRequestId: String?,
        attachments: [ComposerAttachment]
    ) -> Data {
        var body = Data()
        let crlf = "\r\n"
        let dashes = "--"

        func appendField(name: String, value: String) {
            body.append("\(dashes)\(boundary)\(crlf)".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"\(name)\"\(crlf)\(crlf)".data(using: .utf8)!)
            body.append(value.data(using: .utf8) ?? Data())
            body.append(crlf.data(using: .utf8)!)
        }

        appendField(name: "text", value: text)
        appendField(name: "intent", value: intent)
        if let clientRequestId, !clientRequestId.isEmpty {
            appendField(name: "client_request_id", value: clientRequestId)
        }

        for attachment in attachments {
            let safeFilename = sanitizeMultipartFilename(attachment.filename)
            body.append("\(dashes)\(boundary)\(crlf)".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"attachments\"; filename=\"\(safeFilename)\"\(crlf)".data(using: .utf8)!)
            body.append("Content-Type: \(attachment.mimeType)\(crlf)\(crlf)".data(using: .utf8)!)
            body.append(attachment.data)
            body.append(crlf.data(using: .utf8)!)
        }
        body.append("\(dashes)\(boundary)\(dashes)\(crlf)".data(using: .utf8)!)
        return body
    }

    private static func sanitizeMultipartFilename(_ name: String) -> String {
        let stripped = name.replacingOccurrences(of: "\"", with: "")
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
        return stripped.isEmpty ? "image.jpg" : stripped
    }

    /// Extract `{"detail": {"error_code": ..., "message": ...}}` from an
    /// HTTPException body. Returns nil when the body isn't structured,
    /// letting callers fall back to the generic `LonghouseAPIError.from(...)`.
    static func parseStructuredError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        guard
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let detail = obj["detail"] as? [String: Any],
            let code = (detail["error_code"] as? String) ?? (detail["code"] as? String)
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
        return try JSONDecoder.snakeCase.decode(APISessionDraftReplyResponse.self, from: data).draftReplyResponse
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
        return try JSONDecoder.snakeCase.decode(APISessionLoopModeResponse.self, from: data).loopModeResponse
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

// MARK: - Remote session launch

public struct MachineDirectoryEntry: Decodable, Sendable, Hashable {
    public let deviceId: String
    public let machineName: String
    public let online: Bool
    public let controlChannelStatus: String?
    public let supports: [String]
    public let canLaunchCodex: Bool?
    public let launchBlockedBy: String?
    public let lastSeenAt: String?
    public let engineBuild: String?

    public var supportsCodexLaunch: Bool { canLaunchCodex ?? supports.contains("codex.launch") }
    public var isLaunchable: Bool {
        let controlConnected = controlChannelStatus.map { $0 == "connected" } ?? online
        return controlConnected && supportsCodexLaunch
    }
}

public struct MachineDirectoryResponse: Decodable, Sendable {
    public let machines: [MachineDirectoryEntry]
}

public enum RemoteLaunchState: String, Decodable, Sendable {
    case launching
    case live
    case launchingUnknown = "launching_unknown"
    case launchFailed = "launch_failed"
    case launchOrphaned = "launch_orphaned"
}

public struct RemoteSessionLaunchResponse: Decodable, Sendable {
    public let sessionId: String
    public let launchState: RemoteLaunchState
    public let launchErrorCode: String?
    public let launchErrorMessage: String?
}

extension LonghouseAPI {
    func listMachines() async throws -> [MachineDirectoryEntry] {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/machines"))
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(MachineDirectoryResponse.self, from: data).machines
    }

    func recentWorkspacePaths(deviceId: String, limit: Int = 50) async throws -> [String] {
        var components = URLComponents(url: baseURL.appendingPathComponent("/api/timeline/sessions"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "device_id", value: deviceId),
            URLQueryItem(name: "days_back", value: "30"),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "hide_autonomous", value: "false"),
        ]
        var request = URLRequest(url: components.url!)
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decoded = try JSONDecoder.snakeCase.decode(APITimelineSessionsListResponse.self, from: data)
        return Self.workspacePathSuggestions(from: decoded.sessions)
    }

    static func workspacePathSuggestions(from cards: [APITimelineSessionCardResponse], limit: Int = 16) -> [String] {
        var seen = Set<String>()
        var paths: [String] = []

        func add(_ path: String?) {
            guard let path, path.starts(with: "/"), !seen.contains(path) else { return }
            seen.insert(path)
            paths.append(path)
        }

        for card in cards {
            for session in [card.head, card.detail, card.root] {
                add(session.cwd)
                add(parentWorkspacePath(session.cwd))
            }
            if paths.count >= limit { break }
        }

        return Array(paths.prefix(limit))
    }

    static func compactWorkspacePath(_ path: String) -> String {
        path.replacingOccurrences(of: #"^/Users/[^/]+"#, with: "~", options: .regularExpression)
    }

    private static func parentWorkspacePath(_ path: String?) -> String? {
        guard let path else { return nil }
        let normalized = path.replacingOccurrences(of: #"/+$"#, with: "", options: .regularExpression)
        guard let slash = normalized.lastIndex(of: "/"), slash > normalized.startIndex else { return nil }
        let parent = String(normalized[..<slash])
        let parentName = parent.split(separator: "/").last.map(String.init)
        guard let parentName, !parentName.isEmpty, parentName != "git" else { return nil }
        return parent
    }

    func launchRemoteSession(
        deviceId: String,
        provider: String = "codex",
        cwd: String,
        displayName: String? = nil,
        clientRequestId: String? = nil
    ) async throws -> RemoteSessionLaunchResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/launch"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = [
            "device_id": deviceId,
            "provider": provider,
            "cwd": cwd,
        ]
        if let displayName, !displayName.isEmpty { body["display_name"] = displayName }
        if let clientRequestId, !clientRequestId.isEmpty { body["client_request_id"] = clientRequestId }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseLaunchError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(RemoteSessionLaunchResponse.self, from: data)
    }

    static func parseLaunchError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        guard
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let detail = obj["detail"] as? [String: Any],
            let code = (detail["code"] as? String) ?? (detail["error_code"] as? String)
        else {
            return nil
        }
        let message = (detail["message"] as? String) ?? ""
        return .structured(status: statusCode, errorCode: code, message: message)
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
        decoder.keyDecodingStrategy = .custom { codingPath in
            guard let key = codingPath.last else {
                return LonghouseJSONCodingKey("")
            }
            if codingPath.dropLast().contains(where: { pathKey in
                pathKey.stringValue == "tool_input_json" || pathKey.stringValue == "toolInputJson"
            }) {
                return LonghouseJSONCodingKey(key.stringValue)
            }
            return LonghouseJSONCodingKey(JSONDecoder.longhouseConvertFromSnakeCase(key.stringValue))
        }
        return decoder
    }()

    private static func longhouseConvertFromSnakeCase(_ key: String) -> String {
        guard key.contains("_") else { return key }
        let parts = key.split(separator: "_", omittingEmptySubsequences: false)
        guard let first = parts.first else { return key }
        return String(first) + parts.dropFirst().map { part in
            guard let firstCharacter = part.first else { return "" }
            return firstCharacter.uppercased() + part.dropFirst()
        }.joined()
    }
}

private struct LonghouseJSONCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init(_ stringValue: String) {
        self.stringValue = stringValue
        self.intValue = nil
    }

    init?(stringValue: String) {
        self.init(stringValue)
    }

    init?(intValue: Int) {
        self.stringValue = String(intValue)
        self.intValue = intValue
    }
}

struct UserNotificationSettings: Decodable, Equatable {
    let apnsEnabled: Bool
}
