import GoogleSignIn
import SwiftUI
import WidgetKit

@main
struct LonghouseApp: App {
    @StateObject private var appState = AppState()
    @UIApplicationDelegateAdaptor(LonghousePushAppDelegate.self) private var pushDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .onOpenURL { url in
                    if !handleLonghouseURL(url) {
                        GIDSignIn.sharedInstance.handle(url)
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: .longhouseAPNSDeviceTokenUpdated)) { _ in
                    Task {
                        await appState.syncStoredAPNSTokenIfPossible()
                    }
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

    private func handleLonghouseURL(_ url: URL) -> Bool {
        guard url.scheme == "ai.longhouse.ios" else { return false }
        let sessionID: String?
        if url.host == "session" {
            sessionID = url.pathComponents.dropFirst().first
        } else if url.pathComponents.dropFirst().first == "session" {
            sessionID = url.pathComponents.dropFirst(2).first
        } else {
            sessionID = nil
        }
        guard let sessionID, !sessionID.isEmpty else { return true }
        PushNotificationStore.storePendingSessionID(sessionID)
        return true
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
            SharedAuthStore.primeSharedCookieStorage(for: self.serverURL)
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

        SharedAuthStore.primeSharedCookieStorage(for: serverURL)
        let cookies = SharedAuthStore.managedCookies(for: serverURL)
        let hasRefresh = cookies.contains(where: { $0.name == SharedAuthStore.refreshCookieName })
        let hasSession = cookies.contains(where: { $0.name == SharedAuthStore.sessionCookieName })

        let result: SessionRestoreResult
        if hasRefresh {
            result = await refreshBrowserSession()
        } else if hasSession {
            result = await verifyBrowserSession()
        } else {
            result = .unauthenticated
        }

        switch result {
        case .authenticated:
            isAuthenticated = true
            authError = nil
            await syncStoredAPNSTokenIfPossible()
        case .indeterminate:
            isAuthenticated = hasSession || hasRefresh
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

        SharedAuthStore.captureCookiesFromSharedStorage(for: serverURL)
        let isSignedIn = SharedAuthStore.hasManagedCookies(for: serverURL)

        if isSignedIn {
            authError = nil
            await syncStoredAPNSTokenIfPossible()
        } else {
            KeychainHelper.deleteAuthToken()
            authError = "Signed in, but failed to restore the session"
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
            SharedAuthStore.clearManagedCookies(for: previousURL)
            SharedAuthStore.removeSharedCookieStorage(for: previousURL)
            KeychainHelper.deleteAuthToken()
        }
        SharedAuthStore.primeSharedCookieStorage(for: trimmed)
    }

    func exchangeHostedSSOToken(_ ssoToken: String) async -> Bool {
        guard let url = URL(string: "\(serverURL)/api/auth/accept-token") else {
            authError = "Invalid server URL"
            return false
        }

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
            SharedAuthStore.clearManagedCookies(for: previousURL)
            SharedAuthStore.removeSharedCookieStorage(for: previousURL)
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
                SharedAuthStore.clearManagedCookies(for: previousURL)
                SharedAuthStore.removeSharedCookieStorage(for: previousURL)
                KeychainHelper.deleteAuthToken()
            }
            SharedAuthStore.primeSharedCookieStorage(for: trimmed)
            await restoreSession()
        }
    }

    func signOut() {
        Task {
            await signOutLocallyAndRemotely()
        }
    }

    func ensurePushRegistrationIfPossible() async {
        guard isAuthenticated else {
            return
        }
        let granted = await PushNotificationStore.ensureAuthorizedAndRegister()
        guard granted else {
            return
        }
        await syncStoredAPNSTokenIfPossible()
    }

    func syncStoredAPNSTokenIfPossible() async {
        guard isAuthenticated, let api = LonghouseAPI(host: serverURL) else {
            return
        }
        guard let deviceToken = PushNotificationStore.storedDeviceToken() else {
            return
        }
        do {
            try await api.registerAPNSDevice(
                deviceToken: deviceToken,
                pushEnvironment: PushNotificationStore.pushEnvironment,
                appBuildId: PushNotificationStore.currentAppBuildID
            )
        } catch LonghouseAPIError.notAuthenticated {
            return
        } catch {
            NSLog("Longhouse APNs sync failed: %@", error.localizedDescription)
        }
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

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 8

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .indeterminate
            }

            if http.statusCode == 200 {
                SharedAuthStore.captureCookiesFromSharedStorage(for: serverURL)
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
        // Fire-and-forget the server logout while cookies are still present.
        if let url = URL(string: "\(serverURL)/api/auth/logout") {
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
        SharedAuthStore.clearManagedCookies(for: serverURL)
        SharedAuthStore.removeSharedCookieStorage(for: serverURL)
        WidgetSessionSnapshotStore.clear()
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
