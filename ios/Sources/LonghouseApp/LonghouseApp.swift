import GoogleSignIn
import SwiftUI
import WidgetKit

@main
struct LonghouseApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .onOpenURL { url in
                    GIDSignIn.sharedInstance.handle(url)
                }
                .task {
                    let environment = ProcessInfo.processInfo.environment
                    if UITestHooks.shouldResetState {
                        await appState.resetForUITests()
                    }
                    if environment["LONGHOUSE_WIDGET_PROBE_ONLY"] == "1" {
                        let result = await WidgetSessionLoader.load()
                        WidgetSessionLoader.logProbeResult(result, source: "launch-probe-only")
                    } else if UITestHooks.shouldResetState {
                        appState.isValidating = false
                    } else {
                        await appState.restoreSession()
                        if environment["LONGHOUSE_WIDGET_PROBE_ON_LAUNCH"] == "1" {
                            let result = await WidgetSessionLoader.load()
                            WidgetSessionLoader.logProbeResult(result, source: "launch-probe")
                        }
                    }
                }
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var serverURL: String
    @Published var isAuthenticated = false
    @Published var isValidating = true
    @Published var authError: String?
    @Published var hostedAuthAttemptURL: String?

    init() {
        self.serverURL = KeychainHelper.loadServerURL() ?? ""
        if !self.serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            SharedAuthStore.saveServerURL(self.serverURL)
        }
    }

    func restoreSession() async {
        isValidating = true
        hostedAuthAttemptURL = nil
        SharedAuthStore.saveServerURL(serverURL)
        if serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            isAuthenticated = false
            authError = nil
            isValidating = false
            WidgetCenter.shared.reloadAllTimelines()
            return
        }

        var session = await BrowserSessionStore.webKitSession(for: serverURL)
        if !session.hasCookies {
            await BrowserSessionStore.syncSharedCookiesToWebKit(for: serverURL)
            session = await BrowserSessionStore.webKitSession(for: serverURL)
        }

        let result: SessionRestoreResult
        if session.refreshCookie != nil {
            result = await refreshBrowserSession()
        } else if session.sessionCookie != nil {
            result = await verifyBrowserSession()
        } else {
            result = .unauthenticated
        }

        switch result {
        case .authenticated:
            isAuthenticated = true
            authError = nil
            await BrowserSessionStore.persistAccessTokenFromWebKit(for: serverURL)
        case .indeterminate:
            isAuthenticated = session.hasCookies
            await BrowserSessionStore.persistAccessTokenFromWebKit(for: serverURL)
        case .unauthenticated:
            await clearLocalSession()
        }
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
    }

    func finishLoginFromSharedCookies() async -> Bool {
        SharedAuthStore.saveServerURL(serverURL)
        if serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            authError = "Set your Longhouse server first"
            isAuthenticated = false
            isValidating = false
            return false
        }
        await BrowserSessionStore.syncSharedCookiesToWebKit(for: serverURL)
        let session = await BrowserSessionStore.webKitSession(for: serverURL)
        let isSignedIn = session.hasCookies

        if isSignedIn {
            await BrowserSessionStore.persistAccessTokenFromWebKit(for: serverURL)
            authError = nil
        } else {
            KeychainHelper.deleteAuthToken()
            authError = "Signed in, but failed to restore the browser session"
        }

        isAuthenticated = isSignedIn
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
        return isSignedIn
    }

    func prepareServerForHostedLogin(_ url: String) async {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }

        let previousURL = serverURL
        serverURL = trimmed
        KeychainHelper.saveServerURL(trimmed)
        authError = nil

        if previousURL != trimmed, !previousURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            await BrowserSessionStore.clearAll(for: previousURL)
            KeychainHelper.deleteAuthToken()
        }
    }

    func exchangeHostedSSOToken(_ ssoToken: String) async -> Bool {
        guard let url = URL(string: "\(serverURL)/api/auth/accept-token") else {
            authError = "Invalid server URL"
            return false
        }

        await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: ["token": ssoToken])

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                let fallback = "Hosted sign-in failed"
                authError = Self.apiErrorMessage(from: data) ?? fallback
                return false
            }

            return await finishLoginFromSharedCookies()
        } catch {
            authError = "Network error: \(error.localizedDescription)"
            return false
        }
    }

    func clearAuthError() {
        authError = nil
    }

    func resetForUITests() async {
        let previousURL = serverURL

        serverURL = ""
        isAuthenticated = false
        isValidating = false
        authError = nil
        hostedAuthAttemptURL = nil

        if !previousURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            await BrowserSessionStore.clearAll(for: previousURL)
        }

        KeychainHelper.deleteAuthToken()
        KeychainHelper.deleteServerURL()
        WidgetCenter.shared.reloadAllTimelines()
    }

    func recordHostedAuthAttempt(_ url: URL) {
        hostedAuthAttemptURL = url.absoluteString
    }

    func setServer(_ url: String) {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }

        let previousURL = serverURL
        serverURL = trimmed
        KeychainHelper.saveServerURL(trimmed)
        authError = nil

        Task {
            if previousURL != trimmed {
                await BrowserSessionStore.clearAll(for: previousURL)
                KeychainHelper.deleteAuthToken()
            }
            await restoreSession()
        }
    }

    func signOut() {
        Task {
            await signOutLocallyAndRemotely()
        }
    }

    /// Called by the WebView delegate when the web app redirects to /login.
    /// Runs the full sign-out cleanup so no stale state is left behind,
    /// then drops back to the native LoginView.
    func signOutAndReturnToLogin() async {
        await signOutLocallyAndRemotely()
    }

    private enum SessionRestoreResult {
        case authenticated
        case unauthenticated
        case indeterminate
    }

    private func refreshBrowserSession() async -> SessionRestoreResult {
        guard let url = URL(string: "\(serverURL)/api/auth/refresh") else {
            return .unauthenticated
        }

        await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 8

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .indeterminate
            }

            if http.statusCode == 200 {
                await BrowserSessionStore.syncSharedCookiesToWebKit(for: serverURL)
                return .authenticated
            }
            return http.statusCode == 401 ? .unauthenticated : .indeterminate
        } catch {
            return .indeterminate
        }
    }

    private func verifyBrowserSession() async -> SessionRestoreResult {
        guard let url = URL(string: "\(serverURL)/api/auth/verify") else {
            return .unauthenticated
        }

        await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)

        var request = URLRequest(url: url)
        request.timeoutInterval = 5

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .indeterminate
            }

            if http.statusCode == 204 {
                return .authenticated
            }
            return http.statusCode == 401 ? .unauthenticated : .indeterminate
        } catch {
            return .indeterminate
        }
    }

    private func signOutLocallyAndRemotely() async {
        if let url = URL(string: "\(serverURL)/api/auth/logout") {
            await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)

            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            _ = try? await URLSession.shared.data(for: request)
        }

        GIDSignIn.sharedInstance.signOut()
        await clearLocalSession()
        authError = nil
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
    }

    private func clearLocalSession() async {
        await BrowserSessionStore.clearAll(for: serverURL)
        KeychainHelper.deleteAuthToken()
        isAuthenticated = false
    }

    private static func apiErrorMessage(from data: Data) -> String? {
        guard !data.isEmpty else {
            return nil
        }

        if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let detail = json["detail"] as? String,
           !detail.isEmpty {
            return detail
        }

        if let body = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !body.isEmpty {
            return body
        }

        return nil
    }
}
