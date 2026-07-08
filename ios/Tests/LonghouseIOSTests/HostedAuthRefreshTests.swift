import Foundation
import Testing
@testable import Longhouse

@Suite(.serialized)
struct HostedAuthRefreshTests {
    @Test
    func nativeRefreshUsesRefreshEndpointAndRotatesStoredTokens() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://native-refresh-api-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveNativeRefreshToken("old-refresh", for: serverURL)
        let recorder = RequestRecorder()
        let api = makeAPI(serverURL: serverURL) { request in
            recorder.record(request)
            return jsonResponse(for: request, statusCode: 200, body: [
                "runtime_token": "runtime-new",
                "expires_in": 120,
                "refresh_token": "refresh-new",
                "refresh_token_expires_at": "2026-07-08T12:34:56Z",
                "device_session_id": "device-1",
            ])
        }

        try await api.refreshRuntimeToken()

        let requests = recorder.requests()
        #expect(requests.count == 1)
        #expect(requests.first?.url?.path == "/api/auth/refresh-native-session")
        #expect(requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(SharedAuthStore.runtimeToken(for: serverURL) == "runtime-new")
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-new")
        #expect(SharedAuthStore.nativeRefreshTokenExpiresAt(for: serverURL) != nil)
        clearTokens(serverURL)
    }

    @Test
    func legacyBearerRefreshUpgradesStoredNativeRefreshToken() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://legacy-refresh-api-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveRuntimeToken("runtime-old", for: serverURL)
        let recorder = RequestRecorder()
        let api = makeAPI(serverURL: serverURL) { request in
            recorder.record(request)
            return jsonResponse(for: request, statusCode: 200, body: [
                "runtime_token": "runtime-new",
                "expires_in": 120,
                "refresh_token": "refresh-upgrade",
                "refresh_token_expires_at": "2026-07-08T12:34:56Z",
            ])
        }

        try await api.refreshRuntimeToken()

        let requests = recorder.requests()
        #expect(requests.count == 1)
        #expect(requests.first?.url?.path == "/api/auth/refresh-runtime-token")
        #expect(requests.first?.value(forHTTPHeaderField: "Authorization") == "Bearer runtime-old")
        #expect(SharedAuthStore.runtimeToken(for: serverURL) == "runtime-new")
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-upgrade")
        clearTokens(serverURL)
    }

    @Test
    func nativeRefreshResponseWithoutReplacementClearsStaleRefreshToken() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://missing-refresh-api-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveNativeRefreshToken("old-refresh", for: serverURL)
        let api = makeAPI(serverURL: serverURL) { request in
            jsonResponse(for: request, statusCode: 200, body: [
                "runtime_token": "runtime-new",
                "expires_in": 120,
            ])
        }

        try await api.refreshRuntimeToken()

        #expect(SharedAuthStore.runtimeToken(for: serverURL) == "runtime-new")
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == nil)
        clearTokens(serverURL)
    }

    @Test
    func nativeRefreshUnauthorizedThrowsNotAuthenticated() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://rejected-refresh-api-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveNativeRefreshToken("bad-refresh", for: serverURL)
        let api = makeAPI(serverURL: serverURL) { request in
            jsonResponse(for: request, statusCode: 401, body: ["detail": "rejected"])
        }

        do {
            try await api.refreshRuntimeToken()
            Issue.record("expected notAuthenticated")
        } catch LonghouseAPIError.notAuthenticated {
            #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "bad-refresh")
        } catch {
            Issue.record("expected notAuthenticated, got \(error)")
        }
        clearTokens(serverURL)
    }

    @Test
    func concurrentRefreshesShareOneNativeRotation() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://single-flight-refresh-api-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveNativeRefreshToken("old-refresh", for: serverURL)
        let recorder = RequestRecorder()
        let api = makeAPI(serverURL: serverURL) { request in
            recorder.record(request)
            Thread.sleep(forTimeInterval: 0.15)
            return jsonResponse(for: request, statusCode: 200, body: [
                "runtime_token": "runtime-new",
                "expires_in": 120,
                "refresh_token": "refresh-new",
                "refresh_token_expires_at": "2026-07-08T12:34:56Z",
            ])
        }

        async let first: Void = api.refreshRuntimeToken()
        async let second: Void = api.refreshRuntimeToken()
        _ = try await (first, second)

        #expect(recorder.requests().count == 1)
        #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-new")
        clearTokens(serverURL)
    }

    @Test
    func clientWithAuthRefreshDisabledDoesNotRefreshOnUnauthorizedResponse() async throws {
        guard SharedAuthStore.isAppGroupAvailable else {
            return
        }
        let serverURL = "https://widget-readonly-auth-test.longhouse.ai"
        clearTokens(serverURL)
        SharedAuthStore.saveNativeRefreshToken("refresh-token", for: serverURL)
        SharedAuthStore.saveRuntimeToken("runtime-old", for: serverURL)
        let recorder = RequestRecorder()
        let api = makeAPI(serverURL: serverURL, allowsAuthRefresh: false) { request in
            recorder.record(request)
            return jsonResponse(for: request, statusCode: 401, body: ["detail": "expired"])
        }

        do {
            _ = try await api.recentSessions(limit: 1)
            Issue.record("expected notAuthenticated")
        } catch LonghouseAPIError.notAuthenticated {
            #expect(recorder.requests().count == 1)
            #expect(recorder.requests().first?.url?.path == "/api/timeline/sessions")
            #expect(SharedAuthStore.nativeRefreshToken(for: serverURL) == "refresh-token")
        } catch {
            Issue.record("expected notAuthenticated, got \(error)")
        }
        clearTokens(serverURL)
    }

    private func makeAPI(
        serverURL: String,
        allowsAuthRefresh: Bool = true,
        handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> LonghouseAPI {
        NativeAuthMockURLProtocol.handler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeAuthMockURLProtocol.self]
        return LonghouseAPI(
            baseURL: URL(string: serverURL)!,
            allowsAuthRefresh: allowsAuthRefresh,
            urlSession: URLSession(configuration: configuration)
        )
    }

    private func clearTokens(_ serverURL: String) {
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
        SharedAuthStore.clearManagedCookies(for: serverURL)
    }
}

private final class RequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var storedRequests: [URLRequest] = []

    func record(_ request: URLRequest) {
        lock.lock()
        storedRequests.append(request)
        lock.unlock()
    }

    func requests() -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return storedRequests
    }
}

private final class NativeAuthMockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private func jsonResponse(
    for request: URLRequest,
    statusCode: Int,
    body: [String: Any]
) -> (HTTPURLResponse, Data) {
    let data = try! JSONSerialization.data(withJSONObject: body)
    let response = HTTPURLResponse(
        url: request.url!,
        statusCode: statusCode,
        httpVersion: nil,
        headerFields: ["Content-Type": "application/json"]
    )!
    return (response, data)
}
