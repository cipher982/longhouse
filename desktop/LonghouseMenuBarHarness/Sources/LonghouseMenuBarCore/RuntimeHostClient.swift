import Foundation
import OSLog

/// Authenticated requests to the Longhouse server (the Runtime Host) with this
/// machine's device token. The session stream, the machines poll and a stop
/// for another machine's session all build requests here, so the plaintext
/// http rule and the token handling cannot drift between them.
enum RuntimeHostRequest {
    private static let logger = Logger(
        subsystem: "ai.longhouse.desktop",
        category: "runtime-host-request"
    )

    static func make(
        connection: RealtimeConnectionSnapshot,
        path: String,
        queryItems: [URLQueryItem] = [],
        method: String = "GET"
    ) throws -> URLRequest {
        guard let rawURL = connection.runtimeUrl,
              let baseURL = URL(string: rawURL),
              let tokenPath = connection.tokenPath
        else { throw URLError(.badURL) }
        // The token rides this request as a header, so a plain-http address is
        // judged by the same rule as every other native client. (https needs
        // no judgement; any other scheme is a test URLProtocol.)
        if baseURL.scheme?.lowercased() == "http" {
            let transport = PlaintextHTTP.check(rawURL, allowInsecureHTTP: connection.allowInsecureHttp == true)
            guard transport.isUsable else {
                throw PlaintextHTTPRefusal(message: PlaintextHTTP.refusalMessage(rawURL, outcome: transport))
            }
            if transport == .allowedWarn {
                logger.warning("\(PlaintextHTTP.insecureWarning(rawURL), privacy: .public)")
            }
        }
        let token = try String(contentsOfFile: tokenPath, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { throw URLError(.userAuthenticationRequired) }

        guard var components = URLComponents(
            url: baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false
        ) else { throw URLError(.badURL) }
        if !queryItems.isEmpty {
            components.queryItems = queryItems
        }
        guard let url = components.url else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(token, forHTTPHeaderField: "X-Agents-Token")
        return request
    }
}

/// The account's other machines, from `/api/agents/machines/summary`.
///
/// The session stream says which sessions are alive; it says nothing about a
/// machine whose engine went offline or broke. The summary is the server's one
/// status vocabulary for machines (the same words the web Machines page shows),
/// so the menu bar reads it rather than inventing its own.
enum RemoteMachinesClient {
    /// A machine going offline is worth a line within a minute; the summary is
    /// a cheap read and this is one request per minute per signed-in Mac.
    static let pollSeconds: TimeInterval = 60

    private struct Response: Decodable {
        let machines: [Entry]
    }

    private struct Entry: Decodable {
        let machine: Machine
        let status: Status?
    }

    private struct Machine: Decodable {
        let deviceId: String
        let machineName: String?
        let online: Bool?
    }

    private struct Status: Decodable {
        let tone: String?
        let label: String?
        let hint: String?
        let quiet: Bool?
    }

    static func fetch(
        connection: RealtimeConnectionSnapshot,
        session: URLSession = .shared
    ) async throws -> [RemoteMachineSnapshot] {
        let offsetMinutes = TimeZone.current.secondsFromGMT() / 60
        var request = try RuntimeHostRequest.make(
            connection: connection,
            path: "/api/agents/machines/summary",
            queryItems: [
                // One day: an offline machine nobody started anything on today
                // is folded as quiet by the server and earns no line here.
                URLQueryItem(name: "days", value: "1"),
                URLQueryItem(name: "utc_offset_minutes", value: String(offsetMinutes)),
            ]
        )
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        return try decode(data, excludingDeviceId: connection.machineName)
    }

    /// Every enrolled machine except this one.
    static func decode(_ data: Data, excludingDeviceId localDeviceId: String?) throws -> [RemoteMachineSnapshot] {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(Response.self, from: data)
        return response.machines.compactMap { entry in
            guard entry.machine.deviceId != localDeviceId else { return nil }
            return RemoteMachineSnapshot(
                deviceId: entry.machine.deviceId,
                machineName: entry.machine.machineName,
                online: entry.machine.online,
                tone: entry.status?.tone,
                label: entry.status?.label,
                hint: entry.status?.hint,
                quiet: entry.status?.quiet
            )
        }
    }
}

/// Asks the server to stop a session on another machine. The server routes the
/// terminate to the machine that owns the session; this Mac never runs a local
/// process command for a session it does not host.
enum RemoteSessionTerminate {
    private static let logger = Logger(
        subsystem: "ai.longhouse.desktop",
        category: "remote-session-terminate"
    )

    static func request(
        connection: RealtimeConnectionSnapshot,
        sessionID: String
    ) throws -> URLRequest {
        let encoded = sessionID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? sessionID
        var request = try RuntimeHostRequest.make(
            connection: connection,
            path: "/api/agents/sessions/\(encoded)/terminate-live",
            method: "POST"
        )
        request.timeoutInterval = 30
        return request
    }

    static func send(_ request: URLRequest, session: URLSession) {
        Task.detached(priority: .userInitiated) {
            do {
                let (_, response) = try await session.data(for: request)
                let status = (response as? HTTPURLResponse)?.statusCode ?? -1
                logger.info("Remote terminate answered HTTP \(status, privacy: .public)")
            } catch {
                logger.error("Remote terminate failed: \(String(describing: error), privacy: .public)")
            }
        }
    }
}
