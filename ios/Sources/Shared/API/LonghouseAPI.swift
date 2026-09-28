import Foundation
import OSLog

/// Which retrieval lane a session search asked for.
enum TimelineSearchLane: String, Sendable, Equatable {
    case lexical
    case semantic
}

/// Server search routes cover all indexed history when the caller names no
/// explicit `days_back`. iOS has no date-range picker of its own, so every
/// server-lane search omits it and lets the server search everything, the
/// same default web and the machine API now use.

private struct SelectedModelPayload: Decodable {
    let selectedModel: String?
}

private struct APIPauseRequestResponsePayload: Decodable {
    let status: String
    let pauseRequest: APISessionPauseRequestProjectionResponse
}

private actor RuntimeTokenRefreshCoordinator {
    private var inFlight: [String: Task<Void, Error>] = [:]

    func run(key: String, operation: @escaping @Sendable () async throws -> Void) async throws {
        if let task = inFlight[key] {
            return try await task.value
        }

        let task = Task {
            try await operation()
        }
        inFlight[key] = task
        defer {
            inFlight[key] = nil
        }
        return try await task.value
    }
}

struct LonghouseAPI: Sendable {
    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "SessionOpen")
    private static let authRefreshCoordinator = RuntimeTokenRefreshCoordinator()

    let baseURL: URL
    let allowsAuthRefresh: Bool
    let urlSession: URLSession

    init(baseURL: URL, allowsAuthRefresh: Bool = true, urlSession: URLSession = .shared) {
        self.baseURL = baseURL
        self.allowsAuthRefresh = allowsAuthRefresh
        self.urlSession = urlSession
    }

    init?(host: String, allowsAuthRefresh: Bool = true, urlSession: URLSession = .shared) {
        guard let url = URL(string: host) else { return nil }
        self.init(baseURL: url, allowsAuthRefresh: allowsAuthRefresh, urlSession: urlSession)
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

    /// Search sessions on exactly one lane. See ``TimelineSearchLane``.
    ///
    /// `daysBack` is nil by default: with no date-range picker on iOS, a
    /// search covers all indexed history unless a caller explicitly narrows
    /// it, matching web and the machine API.
    func searchSessions(
        query: String,
        lane: TimelineSearchLane,
        daysBack: Int? = nil,
        limit: Int = 30
    ) async throws -> [SessionSummary] {
        switch lane {
        case .lexical:
            return try await lexicalSearchSessions(query: query, daysBack: daysBack, limit: limit)
        case .semantic:
            return try await semanticSearchSessions(query: query, daysBack: daysBack, limit: limit)
        }
    }

    /// Keyword search over the timeline's own index.
    ///
    /// Reads the same route the browser timeline reads, so the phone and the
    /// browser cannot answer one query with different lanes, and the same
    /// visibility policy applies to both.
    func lexicalSearchSessions(query: String, daysBack: Int?, limit: Int) async throws -> [SessionSummary] {
        let url = Self.lexicalSearchURL(baseURL: baseURL, query: query, daysBack: daysBack, limit: limit)
        var request = URLRequest(url: url)
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }

        let decoded = try JSONDecoder.snakeCase.decode(TimelineCardList.self, from: data)
        return decoded.sessions.map(\.sessionSummary)
    }

    /// Dense paraphrase search. Slower, and the only lane that finds a session
    /// whose words the user did not type.
    func semanticSearchSessions(query: String, daysBack: Int?, limit: Int) async throws -> [SessionSummary] {
        let url = Self.semanticSearchURL(baseURL: baseURL, query: query, daysBack: daysBack, limit: limit)
        var request = URLRequest(url: url)
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }

        let decoded = try JSONDecoder.snakeCase.decode(APISemanticSearchResponse.self, from: data)
        return decoded.sessions.map(\.searchSessionSummary)
    }

    static func lexicalSearchURL(baseURL: URL, query: String, daysBack: Int?, limit: Int) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions"),
            resolvingAgainstBaseURL: false
        )!
        var queryItems = [
            URLQueryItem(name: "query", value: query),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "mode", value: TimelineSearchLane.lexical.rawValue),
        ]
        if let daysBack {
            // Keep the position the explicit-range URL has always had.
            queryItems.insert(URLQueryItem(name: "days_back", value: String(daysBack)), at: 1)
        }
        components.queryItems = queryItems
        return components.url!
    }

    static func semanticSearchURL(baseURL: URL, query: String, daysBack: Int?, limit: Int) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/semantic"),
            resolvingAgainstBaseURL: false
        )!
        var queryItems = [
            URLQueryItem(name: "query", value: query),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "context_mode", value: "forensic"),
        ]
        if let daysBack {
            // Keep the position the explicit-range URL has always had.
            queryItems.insert(URLQueryItem(name: "days_back", value: String(daysBack)), at: 1)
        }
        components.queryItems = queryItems
        return components.url!
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

        let decoded = try JSONDecoder.snakeCase.decode(TimelineCardList.self, from: data)
        return decoded.sessions.map(\.sessionSummary)
    }
    /// The authority can return one durable receipt by client request ID,
    /// even after it falls outside the recent chip projection.
    static func sessionInputReceiptsURL(baseURL: URL, id: String, clientRequestId: String? = nil) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/sessions/\(id)/inputs"),
            resolvingAgainstBaseURL: false
        )!
        if let clientRequestId, !clientRequestId.isEmpty {
            components.queryItems = [URLQueryItem(name: "client_request_id", value: clientRequestId)]
        }
        return components.url!
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
    /// The route chrome can be served without building a transcript
    /// projection. Keep this request separate from `mobile-tail` so a large
    /// session never holds the navigation transition hostage.
    static func sessionDetailURL(baseURL: URL, id: String) -> URL {
        baseURL.appendingPathComponent("/api/timeline/sessions/\(id)")
    }


    static func sessionSubagentsURL(baseURL: URL, id: String) -> URL {
        baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/subagents")
    }

    static func sessionMobileTailURL(
        baseURL: URL,
        id: String,
        limit: Int = 50,
        offset: Int = 0,
        branchMode: String = "head",
        snapshotEventId: String? = nil,
        cursor: String? = nil
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
            items.append(URLQueryItem(name: "snapshot_event_id", value: snapshotEventId))
        }
        if let cursor {
            items.append(URLQueryItem(name: "cursor", value: cursor))
        }
        components.queryItems = items
        return components.url!
    }

    /// Wire payloads decode as generated OpenAPI DTOs and are mapped into the
    /// domain models. The domain types carry flat display-facing state and are
    /// only Codable for on-device caches, so decoding them straight off the
    /// network silently drops `session_state`.
    static func decodeSessionWorkspace(_ data: Data) throws -> SessionWorkspaceResponse {
        try JSONDecoder.snakeCase
            .decode(APISessionWorkspaceResponse.self, from: data)
            .sessionWorkspaceResponse
    }

    static func decodeSessionMobileTail(_ data: Data) throws -> SessionMobileTailResponse {
        try JSONDecoder.snakeCase
            .decode(APISessionMobileTailResponse.self, from: data)
            .sessionMobileTailResponse
    }
    static func decodeSessionDetail(_ data: Data) throws -> SessionDetail {
        var detail = try JSONDecoder.snakeCase
            .decode(APISessionResponse.self, from: data)
            .sessionDetail
        if let payload = try? JSONDecoder.snakeCase.decode(SelectedModelPayload.self, from: data) {
            detail.selectedModel = payload.selectedModel
        }
        return detail
    }


    func sessionDetail(id: String) async throws -> SessionDetail {
        var request = URLRequest(
            url: Self.sessionDetailURL(baseURL: baseURL, id: id),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("no-cache", forHTTPHeaderField: "Cache-Control")

        let requestStartedAt = Date()
        Self.logger.debug("session-detail request started session=\(id, privacy: .public)")
        let (data, httpResponse) = try await data(for: request)
        let responseMs = Int(Date().timeIntervalSince(requestStartedAt) * 1000)
        Self.logger.debug(
            "session-detail response session=\(id, privacy: .public) status=\(httpResponse.statusCode) ms=\(responseMs) bytes=\(data.count)"
        )
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try Self.decodeSessionDetail(data)
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
        return try Self.decodeSessionWorkspace(data)
    }

    func sessionSubagents(id: String) async throws -> SessionSubagentsResponse {
        var request = URLRequest(
            url: Self.sessionSubagentsURL(baseURL: baseURL, id: id),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard httpResponse.statusCode == 200 else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionSubagentsResponse.self, from: data)
    }
    func sessionInputReceipt(id: String, clientRequestId: String) async throws -> SessionInputReceiptState? {
        var request = URLRequest(
            url: Self.sessionInputReceiptsURL(
                baseURL: baseURL,
                id: id,
                clientRequestId: clientRequestId
            ),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("no-cache", forHTTPHeaderField: "Cache-Control")
        let (data, response) = try await data(for: request)
        guard response.statusCode == 200 else {
            // An empty/not-found receipt is an unknown outcome. Keep the
            // durable local intent rather than treating a transport response
            // as proof that the side effect was rejected.
            if response.statusCode == 404 { return nil }
            if let structured = Self.parseStructuredError(statusCode: response.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: response.statusCode)
        }
        return Self.decodeInputReceiptState(data, clientRequestId: clientRequestId)
    }
    private static func decodeInputReceiptState(
        _ data: Data,
        clientRequestId: String
    ) -> SessionInputReceiptState? {
        guard let json = try? JSONSerialization.jsonObject(with: data) else { return nil }
        let records: [[String: Any]]
        if let record = json as? [String: Any] {
            let collection = (record["inputs"] as? [[String: Any]])
                ?? (record["receipts"] as? [[String: Any]])
                ?? (record["items"] as? [[String: Any]])
            records = collection ?? [record]
        } else {
            records = (json as? [[String: Any]]) ?? []
        }
        let record = records.first { value in
            let candidate = (value["client_request_id"] as? String)
                ?? (value["clientRequestId"] as? String)
            return candidate == clientRequestId
        }
        guard let record else { return nil }
        let deliveryStatus = (record["delivery_status"] as? String)
            ?? (record["status"] as? String)
            ?? (record["outcome"] as? String)
        let rawDisposition = (record["disposition"] as? String)?.lowercased()
        let intent = (record["intent"] as? String)
        let error = (record["error"] as? String)
            ?? (record["last_error"] as? String)
            ?? (record["message"] as? String)
        let inputId = (record["input_id"] as? Int)
            ?? (record["id"] as? Int)
        let liveInputId = (record["live_input_id"] as? String)
            ?? (record["liveInputId"] as? String)
        let turn: ConsoleTurnReceipt? = {
            guard let raw = record["turn"] as? [String: Any],
                  let turnId = (raw["turn_id"] as? String) ?? (raw["turnId"] as? String),
                  let state = raw["state"] as? String
            else { return nil }
            return ConsoleTurnReceipt(
                turnId: turnId,
                receiptId: (raw["receipt_id"] as? String) ?? (raw["receiptId"] as? String),
                runId: (raw["run_id"] as? String) ?? (raw["runId"] as? String),
                state: state,
                isFresh: (raw["is_fresh"] as? Bool) ?? (raw["isFresh"] as? Bool)
            )
        }()
        let disposition: SessionInputReceiptDisposition = {
            if rawDisposition == "rejected" { return .rejected }
            if rawDisposition == "unknown" { return .couldNotConfirm }
            // A durable receipt proves server ownership. Its status describes
            // delivery, not whether the operation was accepted.
            return .accepted
        }()
        let eventId = (record["event_id"] as? String)
            ?? (record["durable_event_id"] as? String)
        return SessionInputReceiptState(
            clientRequestId: clientRequestId,
            intent: intent,
            status: deliveryStatus,
            disposition: disposition,
            deliveryStatus: deliveryStatus,
            inputId: inputId,
            liveInputId: liveInputId,
            turn: turn,
            eventId: eventId,
            error: error
        )
    }

    func sessionMobileTail(
        id: String,
        limit: Int = 50,
        offset: Int = 0,
        branchMode: String = "head",
        snapshotEventId: String? = nil,
        cursor: String? = nil
    ) async throws -> SessionMobileTailResponse {
        var request = URLRequest(
            url: Self.sessionMobileTailURL(
                baseURL: baseURL,
                id: id,
                limit: limit,
                offset: offset,
                branchMode: branchMode,
                snapshotEventId: snapshotEventId,
                cursor: cursor
            ),
            cachePolicy: .reloadIgnoringLocalCacheData
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("no-cache", forHTTPHeaderField: "Cache-Control")

        let requestStartedAt = Date()
        Self.logger.debug("mobile-tail request started session=\(id, privacy: .public) limit=\(limit, privacy: .public) offset=\(offset, privacy: .public)")
        let (data, httpResponse) = try await data(for: request)
        let responseMs = Int(Date().timeIntervalSince(requestStartedAt) * 1000)
        let contentEncoding = httpResponse.value(forHTTPHeaderField: "content-encoding") ?? "none"
        let wireContentLength = httpResponse.value(forHTTPHeaderField: "content-length") ?? "unknown"
        let serverTiming = httpResponse.value(forHTTPHeaderField: "server-timing") ?? "none"
        // Keep the hot path at debug level; surface only slow or failed tail calls at info.
        if httpResponse.statusCode != 200 || responseMs >= 1_000 {
            Self.logger.info("mobile-tail response received session=\(id, privacy: .public) status=\(httpResponse.statusCode, privacy: .public) decoded_bytes=\(data.count, privacy: .public) wire_content_length=\(wireContentLength, privacy: .public) encoding=\(contentEncoding, privacy: .public) elapsed_ms=\(responseMs, privacy: .public) server_timing=\(serverTiming, privacy: .public)")
        } else {
            Self.logger.debug("mobile-tail response received session=\(id, privacy: .public) status=\(httpResponse.statusCode, privacy: .public) decoded_bytes=\(data.count, privacy: .public) wire_content_length=\(wireContentLength, privacy: .public) encoding=\(contentEncoding, privacy: .public) elapsed_ms=\(responseMs, privacy: .public) server_timing=\(serverTiming, privacy: .public)")
        }
        guard httpResponse.statusCode == 200 else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decodeStartedAt = Date()
        let decoded = try Self.decodeSessionMobileTail(data)
        let decodeMs = Int(Date().timeIntervalSince(decodeStartedAt) * 1000)
        // Decode normally runs well below this; info-level entries should mean "look here".
        if decodeMs >= 100 {
            Self.logger.info("mobile-tail decode finished session=\(id, privacy: .public) events=\(decoded.events.count, privacy: .public) total=\(decoded.projection.total, privacy: .public) elapsed_ms=\(decodeMs, privacy: .public)")
        } else {
            Self.logger.debug("mobile-tail decode finished session=\(id, privacy: .public) events=\(decoded.events.count, privacy: .public) total=\(decoded.projection.total, privacy: .public) elapsed_ms=\(decodeMs, privacy: .public)")
        }
        return decoded
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
        clientRequestId: String
    ) async throws -> SessionInputResponse {
        try await sendInput(
            id: id,
            text: text,
            intent: intent,
            clientRequestId: clientRequestId,
            model: nil
        )
    }

    func sendInput(
        id: String,
        text: String,
        intent: String = "auto",
        clientRequestId: String,
        model: String?
    ) async throws -> SessionInputResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/input"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = [
            "text": text,
            "intent": intent,
            "client_request_id": clientRequestId,
        ]
        if let model {
            let normalizedModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
            if !normalizedModel.isEmpty {
                body["model"] = normalizedModel
            }
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, httpResponse) = try await data(for: request)

        guard (200..<300).contains(httpResponse.statusCode) else {
            if let inputError = Self.parseSessionInputOperationError(statusCode: httpResponse.statusCode, data: data) {
                throw inputError
            }
            if let inputError = Self.parseSessionInputError(statusCode: httpResponse.statusCode, data: data) {
                throw inputError
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try Self.decodeSessionInputResponse(data)
    }

    /// Sends the first turn of a report-backed Console session without
    /// changing the existing text-input protocol.
    func sendInput(
        id: String,
        text: String,
        intent: String = "auto",
        clientRequestId: String,
        reportID: String?
    ) async throws -> SessionInputResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/input"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = [
            "text": text,
            "intent": intent,
            "client_request_id": clientRequestId,
        ]
        if let reportID, !reportID.isEmpty {
            body["report_id"] = reportID
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try Self.decodeSessionInputResponse(data)
    }

    /// Uploads a reviewed bug report bundle. The report is immutable once
    /// accepted; the Console turn carries only its id.
    func uploadBugReport(
        description: String,
        contextJSON: Data,
        sourceSessionID: String?,
        clientReportID: String,
        files: [BugReportUploadFile]
    ) async throws -> BugReportUploadResponse {
        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/reports"))
        request.httpMethod = "POST"
        request.addValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("Longhouse-iOS", forHTTPHeaderField: "User-Agent")
        var body = Data()
        Self.appendMultipartField(&body, boundary: boundary, name: "description", value: description)
        Self.appendMultipartField(
            &body,
            boundary: boundary,
            name: "context_json",
            value: String(data: contextJSON, encoding: .utf8) ?? "{}"
        )
        Self.appendMultipartField(&body, boundary: boundary, name: "client_report_id", value: clientReportID)
        if let sourceSessionID, !sourceSessionID.isEmpty {
            Self.appendMultipartField(&body, boundary: boundary, name: "source_session_id", value: sourceSessionID)
        }
        for file in files {
            Self.appendMultipartFile(
                &body,
                boundary: boundary,
                name: "files",
                filename: file.filename,
                mimeType: file.mimeType,
                data: file.data
            )
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        request.httpBody = body
        let (data, response) = try await self.data(for: request)
        guard (200..<300).contains(response.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: response.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: response.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(BugReportUploadResponse.self, from: data)
    }

    /// Multipart POST for inputs that include image attachments. The intent is
    /// carried unchanged; the server may reject unsupported intent/attachment
    /// combinations as a known disposition rather than silently converting it.
    func sendInputMultipart(
        id: String,
        text: String,
        intent: String = "auto",
        attachments: [ComposerAttachment],
        clientRequestId: String
    ) async throws -> SessionInputResponse {
        try await sendInputMultipart(
            id: id,
            text: text,
            intent: intent,
            attachments: attachments,
            clientRequestId: clientRequestId,
            model: nil
        )
    }

    func sendInputMultipart(
        id: String,
        text: String,
        intent: String = "auto",
        attachments: [ComposerAttachment],
        clientRequestId: String,
        model: String?
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
            intent: intent,
            clientRequestId: clientRequestId,
            model: model,
            attachments: attachments
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
            if let inputError = Self.parseSessionInputOperationError(statusCode: httpResponse.statusCode, data: data) {
                throw inputError
            }
            if let inputError = Self.parseSessionInputError(statusCode: httpResponse.statusCode, data: data) {
                throw inputError
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try Self.decodeSessionInputResponse(data)
    }

    func respondToPauseRequest(
        sessionId: String,
        pauseRequestId: String,
        decision: String,
        answers: [String: [String]]?,
        content: String?,
        message: String?
    ) async throws -> PauseRequestResponse {
        var request = URLRequest(
            url: baseURL.appendingPathComponent("/api/sessions/\(sessionId)/pause-requests/\(pauseRequestId)/response")
        )
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = ["decision": decision]
        if let answers {
            body["answers"] = answers
        }
        if let content, !content.isEmpty {
            body["content"] = content
        }
        if let message, !message.isEmpty {
            body["message"] = message
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        let decoded = try JSONDecoder.snakeCase.decode(APIPauseRequestResponsePayload.self, from: data)
        return PauseRequestResponse(status: decoded.status, pauseRequest: decoded.pauseRequest.sessionPauseRequest)
    }
    static func buildMultipartBody(
        boundary: String,
        text: String,
        intent: String,
        clientRequestId: String,
        model: String? = nil,
        attachments: [ComposerAttachment]
    ) -> Data {
        var body = Data()
        appendMultipartField(&body, boundary: boundary, name: "text", value: text)
        appendMultipartField(&body, boundary: boundary, name: "intent", value: intent)
        appendMultipartField(&body, boundary: boundary, name: "client_request_id", value: clientRequestId)
        if let model {
            let normalizedModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
            if !normalizedModel.isEmpty {
                appendMultipartField(&body, boundary: boundary, name: "model", value: normalizedModel)
            }
        }
        for attachment in attachments {
            appendMultipartFile(
                &body,
                boundary: boundary,
                name: "attachments",
                filename: attachment.filename,
                mimeType: attachment.mimeType,
                data: attachment.data
            )
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        return body
    }
    private static func appendMultipartField(_ body: inout Data, boundary: String, name: String, value: String) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
        body.append(Data(value.utf8))
        body.append(Data("\r\n".utf8))
    }

    private static func appendMultipartFile(
        _ body: inout Data,
        boundary: String,
        name: String,
        filename: String,
        mimeType: String,
        data: Data
    ) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(
            Data(
                "Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(sanitizeMultipartFilename(filename))\"\r\n".utf8
            )
        )
        body.append(Data("Content-Type: \(mimeType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n".utf8))
    }


    private static func sanitizeMultipartFilename(_ name: String) -> String {
        let stripped = name.replacingOccurrences(of: "\"", with: "")
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
        return stripped.isEmpty ? "image.jpg" : stripped
    }

    /// Extract stable error codes from wrapped FastAPI HTTPException bodies and
    /// older bare `{"error_code": ..., "error": ...}` payloads.
    /// Returns nil when the body isn't structured, letting callers fall back to
    /// the generic `LonghouseAPIError.from(...)`.
    static func parseStructuredError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return nil
        }
        return parseStructuredError(statusCode: statusCode, object: object)
    }

    private static func parseStructuredError(
        statusCode: Int,
        object: [String: Any]
    ) -> LonghouseAPIError? {
        let detail = (object["detail"] as? [String: Any]) ?? object
        guard let code = (detail["error_code"] as? String) ?? (detail["code"] as? String) else {
            return nil
        }
        let message = (detail["message"] as? String) ?? (detail["error"] as? String) ?? ""
        return .structured(status: statusCode, errorCode: code, message: message)
    }

    private static func parseSessionInputOperationError(
        statusCode: Int,
        data: Data
    ) -> SessionInputOperationError? {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            guard statusCode == 400 else { return nil }
            return SessionInputOperationError(
                statusCode: statusCode,
                message: "Longhouse rejected this message (HTTP \(statusCode)).",
                errorCode: nil,
                disposition: .rejected,
                deliveryStatus: nil,
                clientRequestId: nil,
                inputId: nil,
                liveInputId: nil,
                turn: nil
            )
        }
        let detail = (object["detail"] as? [String: Any]) ?? object
        let errorCode = (detail["error_code"] as? String)
            ?? (detail["code"] as? String)
            ?? (object["error_code"] as? String)
            ?? (object["code"] as? String)
        let rawDisposition = (detail["disposition"] as? String) ?? (object["disposition"] as? String)
        let rawStatus = (detail["delivery_status"] as? String)
            ?? (detail["deliveryStatus"] as? String)
            ?? (detail["status"] as? String)
            ?? (object["delivery_status"] as? String)
            ?? (object["deliveryStatus"] as? String)
        let disposition: SessionInputDisposition = {
            if let rawDisposition = rawDisposition {
                if let parsed = SessionInputDisposition(rawValue: rawDisposition.lowercased()) {
                    return parsed
                }
                // An unrecognised explicit disposition is not evidence of
                // rejection.
                return .unknown
            }
            if rawStatus != nil {
                // delivery_status describes handoff state, not ownership.
                // Never infer rejection from it when disposition is absent.
                return .unknown
            }
            return statusCode == 400 ? .rejected : .unknown
        }()
        let clientRequestId = (detail["client_request_id"] as? String)
            ?? (object["client_request_id"] as? String)
        let inputId = (detail["input_id"] as? Int)
            ?? (detail["id"] as? Int)
            ?? (object["input_id"] as? Int)
        let liveInputId = (detail["live_input_id"] as? String)
            ?? (object["live_input_id"] as? String)
        let turn: ConsoleTurnReceipt? = {
            guard let raw = (detail["turn"] as? [String: Any]) ?? (object["turn"] as? [String: Any]),
                  let turnId = raw["turn_id"] as? String,
                  let state = raw["state"] as? String
            else { return nil }
            return ConsoleTurnReceipt(
                turnId: turnId,
                receiptId: (raw["receipt_id"] as? String) ?? (raw["receiptId"] as? String),
                runId: (raw["run_id"] as? String) ?? (raw["runId"] as? String),
                state: state,
                isFresh: (raw["is_fresh"] as? Bool) ?? (raw["isFresh"] as? Bool)
            )
        }()
        let message = (detail["message"] as? String)
            ?? (detail["error"] as? String)
            ?? (detail["detail"] as? String)
            ?? (object["detail"] as? String)
            ?? (object["message"] as? String)
            ?? "Longhouse rejected this message (HTTP \(statusCode))."
        let hasOperationMetadata = rawDisposition != nil
            || rawStatus != nil
            || clientRequestId != nil
            || inputId != nil
            || liveInputId != nil
            || turn != nil
            || errorCode != nil
        guard statusCode == 400 || hasOperationMetadata else { return nil }
        return SessionInputOperationError(
            statusCode: statusCode,
            message: message,
            errorCode: errorCode,
            disposition: disposition,
            deliveryStatus: rawStatus,
            clientRequestId: clientRequestId,
            inputId: inputId,
            liveInputId: liveInputId,
            turn: turn
        )
    }

    /// A plain session-input 400 is a known rejection, not an ambiguous delivery outcome.
    static func parseSessionInputError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        if let object,
           let structured = parseStructuredError(statusCode: statusCode, object: object) {
            return structured
        }
        switch statusCode {
        case 400:
            break
        default:
            return nil
        }
        let detail = (object?["detail"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = detail.flatMap { $0.isEmpty ? nil : $0 }
            ?? "Longhouse rejected this message (HTTP \(statusCode))."
        return .httpRejected(status: statusCode, message: message)
    }


    static func decodeSessionInputResponse(_ data: Data) throws -> SessionInputResponse {
        do {
            let decoded = try JSONDecoder.snakeCase.decode(APISessionInputResponse.self, from: data)
            let base = decoded.sessionInputResponse
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let disposition = (object?["disposition"] as? String)
                .flatMap { SessionInputDisposition(rawValue: $0.lowercased()) }
                ?? (decoded.outcome == "unknown" ? .unknown : .accepted)
            let deliveryStatus = (object?["delivery_status"] as? String)
                ?? (object?["status"] as? String)
            return SessionInputResponse(
                outcome: base.outcome,
                disposition: disposition,
                deliveryStatus: deliveryStatus,
                inputId: base.inputId,
                liveInputId: base.liveInputId,
                clientRequestId: base.clientRequestId,
                turn: base.turn,
                intent: base.intent,
                queued: base.queued
            )
        } catch {
            throw LonghouseAPIError.unexpectedResponse(
                "Longhouse returned an unexpected send response. Refreshing to check whether it landed."
            )
        }
    }

    /// Console unread acknowledgement: mark results seen up to the timestamp
    /// this client actually rendered. Server writes max(existing, read_through).
    func markSessionRead(id: String, readThrough: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/read"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["read_through": readThrough])

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

    func sessionResumeIntent(id: String) async throws -> SessionResumeIntent {
        var request = URLRequest(
            url: baseURL.appendingPathComponent("/api/timeline/sessions/\(id)/resume-intent")
        )
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Accept")

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionResumeIntent.self, from: data)
    }

    func createSessionBranch(id: String, message: String, clientRequestId: String) async throws -> SessionBranch {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/\(id)/branches"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: [
                "message": message,
                // Stable per attempt: the server deduplicates on this, so a
                // retry after a dropped response cannot start a second branch.
                "client_request_id": clientRequestId,
                "launch_surface": "ios",
            ]
        )

        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(SessionBranch.self, from: data)
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

    func postClientDiagnostics(_ payload: ClientDiagnosticsPayload) async {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/telemetry/client-diagnostics"))
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

    /// Refresh a hosted CP session using the per-server native refresh token.
    ///
    /// Access tokens are never refresh credentials. A native refresh request
    /// carries only the rotating refresh token and requires a replacement
    /// refresh token in the response.
    func refreshHostedSession() async throws {
        try await Self.authRefreshCoordinator.run(key: baseURL.absoluteString) {
            try await self.performRefreshHostedSession()
        }
    }

    private func performRefreshHostedSession() async throws {
        let serverURL = baseURL.absoluteString
        let generation = SharedAuthStore.authGeneration(for: serverURL)
        guard let refreshToken = SharedAuthStore.nativeRefreshToken(for: serverURL) else {
            SharedAuthStore.clearRuntimeToken(for: serverURL)
            throw LonghouseAPIError.notAuthenticated
        }

        var request = URLRequest(url: baseURL.appendingPathComponent("/api/auth/refresh-native-session"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.httpShouldHandleCookies = false
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["refresh_token": refreshToken])

        let (data, httpResponse) = try await data(for: request, allowRetry: false)
        guard httpResponse.statusCode == 200 else {
            if httpResponse.statusCode == 401 || httpResponse.statusCode == 403 {
                Self.invalidateNativeSessionIfCurrent(serverURL: serverURL, generation: generation)
                throw LonghouseAPIError.notAuthenticated
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = (json["runtime_token"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty,
              let expiresIn = json["expires_in"] as? Int,
              expiresIn > 0,
              let nextRefreshToken = (json["refresh_token"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !nextRefreshToken.isEmpty,
              let refreshExpiry = json["refresh_token_expires_at"] as? String,
              let refreshExpiresAt = Self.parseServerDate(refreshExpiry),
              refreshExpiresAt > Date() else {
            // A malformed 200 is an upstream contract failure, not proof that
            // the locally held refresh credential was rejected. Preserve the
            // pair so a retry can recover after a deploy or transient proxy bug.
            throw LonghouseAPIError.upstreamFailed
        }

        let expiresAt = Date().addingTimeInterval(TimeInterval(expiresIn))
        guard SharedAuthStore.saveHostedTokens(
            runtimeToken: token,
            runtimeExpiresAt: expiresAt,
            refreshToken: nextRefreshToken,
            refreshExpiresAt: refreshExpiresAt,
            for: serverURL,
            expectedGeneration: generation
        ) else {
            throw LonghouseAPIError.notAuthenticated
        }
    }

    private nonisolated static func invalidateNativeSessionIfCurrent(
        serverURL: String,
        generation: String
    ) {
        guard SharedAuthStore.isAuthGenerationCurrent(generation, for: serverURL) else {
            return
        }
        SharedAuthStore.advanceAuthGeneration(for: serverURL)
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
    }

    /// An access token past its stored expiry is refreshed before the request
    /// rather than discovered by a 401: that cost every cold launch a rejected
    /// round trip and a refresh before the timeline could update. The expiry
    /// is a defaults read; the Keychain is touched only once it has passed.
    /// A failed refresh changes nothing here: the request goes out as before
    /// and the 401 path owns recovery.
    private func refreshExpiredRuntimeTokenIfNeeded(allowRetry: Bool) async {
        let serverURL = baseURL.absoluteString
        guard allowRetry, allowsAuthRefresh,
              let expiresAt = SharedAuthStore.runtimeTokenExpiresAt(for: serverURL),
              expiresAt.timeIntervalSinceNow < 5,
              SharedAuthStore.nativeRefreshToken(for: serverURL) != nil
        else { return }
        try? await refreshHostedSession()
    }

    private func isNativeRefreshRequest(_ request: URLRequest) -> Bool {
        request.url?.path == "/api/auth/refresh-native-session"
    }

    private func isCookieRefreshRequest(_ request: URLRequest) -> Bool {
        request.url?.path == "/api/auth/refresh"
    }

    private func data(for request: URLRequest, allowRetry: Bool = true) async throws -> (Data, HTTPURLResponse) {
        var request = request
        request.timeoutInterval = 15
        let isNativeRefresh = isNativeRefreshRequest(request)
        if isNativeRefresh {
            request.httpShouldHandleCookies = false
        } else if isCookieRefreshRequest(request) {
            if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
                request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
            }
        } else {
            await refreshExpiredRuntimeTokenIfNeeded(allowRetry: allowRetry)
            // Explicit cookie injection: widget extension runs in a separate
            // process without shared HTTPCookieStorage.
            let authorizationHeader = SharedAuthStore.authorizationHeader(for: baseURL.absoluteString)
            if let authorizationHeader {
                request.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
            } else if let cookieHeader = SharedAuthStore.cookieHeader(for: baseURL.absoluteString) {
                request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
            }
        }

        let (data, response) = try await urlSession.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LonghouseAPIError.requestFailed
        }
        persistResponseCookies(from: httpResponse, requestURL: request.url)

        if httpResponse.statusCode == 401 && allowRetry && allowsAuthRefresh {
            do {
                if SharedAuthStore.authorizationHeader(for: baseURL.absoluteString) != nil {
                    try await refreshHostedSession()
                } else {
                    try await refreshSession()
                }
                return try await self.data(for: request, allowRetry: false)
            } catch let refreshError as LonghouseAPIError {
                if case .notAuthenticated = refreshError {
                    throw LonghouseAPIError.notAuthenticated
                }
                throw refreshError
            } catch {
                throw error
            }
        }
        return (data, httpResponse)
    }


    static func parseServerDate(_ rawValue: String?) -> Date? {
        guard let rawValue, !rawValue.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: rawValue) {
            return date
        }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: rawValue)
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
        // Mirror both active and legacy deletions so an older cookie cannot
        // survive sign-out in the process-wide URLSession store.
        for cookie in cookies where SharedAuthStore.managedCookieNames.contains(cookie.name) {
            if let expiresDate = cookie.expiresDate, expiresDate <= Date() {
                HTTPCookieStorage.shared.deleteCookie(cookie)
            } else {
                HTTPCookieStorage.shared.setCookie(cookie)
            }
        }
    }
}

private struct APISemanticSearchResponse: Decodable {
    let sessions: [APISessionResponse]
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

    /// Server-owned, frecency-ranked recent workspaces for the launch picker.
    /// Browser-cookie auth (same surface as ``listMachines``); the
    /// ``/api/agents/*`` sibling requires a device token the iOS client never
    /// holds, so this MUST stay on the ``/api/timeline/*`` path.
    static func workspaceSuggestionsURL(baseURL: URL, deviceId: String, limit: Int = 12) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/machines/\(deviceId)/workspaces"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        return components.url!
    }

    func workspaceSuggestions(deviceId: String, limit: Int = 12) async throws -> [WorkspaceSuggestion] {
        var request = URLRequest(url: Self.workspaceSuggestionsURL(baseURL: baseURL, deviceId: deviceId, limit: limit))
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(WorkspaceSuggestionsResponse.self, from: data).workspaces
    }
    /// Server-owned recent model ids for the launch picker. The provider CLI
    /// remains authoritative; this is only a usage-based history.
    static func recentModelsURL(baseURL: URL, deviceId: String, provider: String, limit: Int = 12) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/api/timeline/machines/\(deviceId)/providers/\(provider)/models"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        return components.url!
    }

    func recentModels(deviceId: String, provider: String, limit: Int = 12) async throws -> [RecentModel] {
        var request = URLRequest(
            url: Self.recentModelsURL(
                baseURL: baseURL,
                deviceId: deviceId,
                provider: provider,
                limit: limit
            )
        )
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(RecentModelsResponse.self, from: data).models
    }


    static func compactWorkspacePath(_ path: String) -> String {
        path.replacingOccurrences(of: #"^/Users/[^/]+"#, with: "~", options: .regularExpression)
    }

    func createConsoleSession(
        deviceId: String,
        provider: String,
        cwd: String,
        model: String? = nil,
        displayName: String? = nil,
        sessionId: String? = nil,
        threadId: String? = nil
    ) async throws -> ConsoleSessionCreateResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/sessions/console"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        var body: [String: Any] = [
            "device_id": deviceId,
            "provider": provider,
            "cwd": cwd,
            "launch_surface": "ios",
        ]
        if let model {
            let normalizedModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
            if !normalizedModel.isEmpty { body["model"] = normalizedModel }
        }
        if let sessionId, !sessionId.isEmpty { body["session_id"] = sessionId }
        if let threadId, !threadId.isEmpty { body["thread_id"] = threadId }
        if let displayName, !displayName.isEmpty { body["display_name"] = displayName }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder.snakeCase.decode(ConsoleSessionCreateResponse.self, from: data)
    }

    /// Start the provider's own login on a machine; returns its URL (and
    /// device code). The credential stays on that machine.
    func startProviderSignIn(deviceId: String, provider: String) async throws -> ProviderSignInStart {
        let path = "/api/timeline/machines/\(deviceId)/providers/\(provider)/sign-in"
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Accept")
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
        return try JSONDecoder().decode(ProviderSignInStart.self, from: data)
    }

    /// Paste-back flows: hand the code from the provider's page to the waiting CLI.
    func submitProviderSignInCode(deviceId: String, attemptId: String, code: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/machines/\(deviceId)/sign-in/\(attemptId)/code"))
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["code": code])
        let (data, httpResponse) = try await data(for: request)
        guard (200..<300).contains(httpResponse.statusCode) else {
            if let structured = Self.parseStructuredError(statusCode: httpResponse.statusCode, data: data) {
                throw structured
            }
            throw LonghouseAPIError.from(statusCode: httpResponse.statusCode)
        }
    }

    func cancelProviderSignIn(deviceId: String, attemptId: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("/api/timeline/machines/\(deviceId)/sign-in/\(attemptId)"))
        request.httpMethod = "DELETE"
        _ = try await data(for: request)
    }

    static func parseLaunchError(statusCode: Int, data: Data) -> LonghouseAPIError? {
        parseStructuredError(statusCode: statusCode, data: data)
    }
}

extension LonghouseAPI: SessionWorkspaceClient {}

struct UserNotificationSettings: Decodable, Equatable {
    let apnsEnabled: Bool
}
