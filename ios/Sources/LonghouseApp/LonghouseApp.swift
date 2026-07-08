import GoogleSignIn
import OSLog
import SwiftUI
import UIKit
import WidgetKit

@main
struct LonghouseApp: App {
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Startup")

    @StateObject private var appState = AppState()
    @UIApplicationDelegateAdaptor(LonghousePushAppDelegate.self) private var pushDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .applyUITestAppearanceOverride()
                .onOpenURL { url in
                    if !handleLonghouseURL(url) {
                        GIDSignIn.sharedInstance.handle(url)
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: .longhouseAPNSDeviceTokenUpdated).receive(on: DispatchQueue.main)) { _ in
                    Task {
                        await appState.syncStoredAPNSTokenIfPossible()
                    }
                }
                .task {
                    let startedAt = Date()
                    let environment = ProcessInfo.processInfo.environment
                    logger.info("launch task started reset=\(UITestHooks.shouldResetState, privacy: .public) widget_probe_only=\((environment["LONGHOUSE_WIDGET_PROBE_ONLY"] == "1"), privacy: .public)")
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
                    logger.info("launch task finished elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
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

private extension View {
    @ViewBuilder
    func applyUITestAppearanceOverride() -> some View {
#if DEBUG
        modifier(UITestAppearanceOverrideModifier())
#else
        self
#endif
    }
}

#if DEBUG
private struct UITestAppearanceOverrideModifier: ViewModifier {
    private var colorScheme: ColorScheme? {
        switch UITestHooks.appearanceOverride {
        case "light":
            return .light
        case "dark":
            return .dark
        default:
            return nil
        }
    }

    private var interfaceStyle: UIUserInterfaceStyle? {
        switch UITestHooks.appearanceOverride {
        case "light":
            return .light
        case "dark":
            return .dark
        default:
            return nil
        }
    }

    func body(content: Content) -> some View {
        content
            .preferredColorScheme(colorScheme)
            .onAppear {
                applyInterfaceStyleOverride()
            }
    }

    @MainActor
    private func applyInterfaceStyleOverride() {
        guard let interfaceStyle else { return }
        for scene in UIApplication.shared.connectedScenes {
            guard let windowScene = scene as? UIWindowScene else { continue }
            for window in windowScene.windows {
                window.overrideUserInterfaceStyle = interfaceStyle
            }
        }
    }
}
#endif

@MainActor
final class AppState: ObservableObject {
    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "Startup")

    @Published var serverURL: String
    @Published var isAuthenticated: Bool
    @Published var isValidating: Bool
    @Published private(set) var hasLocalSessionCandidate: Bool
    @Published var authError: String?
    @Published var hostedAuthAttemptURL: String?
    private var apnsSyncInFlightSignature: String?
    private var runtimeTokenRefreshTask: Task<Void, Never>?

    init() {
        let savedServerURL = KeychainHelper.loadServerURL() ?? ""
        let trimmedServerURL = savedServerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasCandidate: Bool
        if !trimmedServerURL.isEmpty {
            SharedAuthStore.saveServerURL(trimmedServerURL)
            SharedAuthStore.primeSharedCookieStorage(for: trimmedServerURL)
            hasCandidate = SharedAuthStore.hasManagedCookies(for: trimmedServerURL)
                || SharedAuthStore.hasRuntimeToken(for: trimmedServerURL)
                || SharedAuthStore.hasNativeRefreshToken(for: trimmedServerURL)
        } else {
            hasCandidate = false
        }
        self.serverURL = savedServerURL
        self.hasLocalSessionCandidate = hasCandidate
        self.isAuthenticated = false
        self.isValidating = hasCandidate
        logger.info("local session candidate loaded has_server=\((!trimmedServerURL.isEmpty), privacy: .public) candidate=\(hasCandidate, privacy: .public)")
    }

    var shouldShowAuthenticatedShell: Bool {
        isAuthenticated || hasLocalSessionCandidate
    }

    func restoreSession() async {
        let startedAt = Date()
        isValidating = true
        hostedAuthAttemptURL = nil
        SharedAuthStore.saveServerURL(serverURL)
        let trimmedServerURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        logger.info("restore session started has_server=\((!trimmedServerURL.isEmpty), privacy: .public)")
        if trimmedServerURL.isEmpty {
            isAuthenticated = false
            hasLocalSessionCandidate = false
            authError = nil
            isValidating = false
            WidgetCenter.shared.reloadAllTimelines()
            logger.info("restore session finished result=no_server elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return
        }

        SharedAuthStore.primeSharedCookieStorage(for: serverURL)
        let cookies = SharedAuthStore.managedCookies(for: serverURL)
        let hasRefresh = cookies.contains(where: { $0.name == SharedAuthStore.refreshCookieName })
        let hasSession = cookies.contains(where: { $0.name == SharedAuthStore.sessionCookieName })
        let hasRuntimeToken = SharedAuthStore.hasRuntimeToken(for: serverURL)
        let hasNativeRefreshToken = SharedAuthStore.hasNativeRefreshToken(for: serverURL)
        hasLocalSessionCandidate = hasRuntimeToken || hasNativeRefreshToken || hasSession || hasRefresh

        var result: SessionRestoreResult
        if hasRuntimeToken {
            result = await verifyBrowserSession()
            if result == .unauthenticated {
                // Token may be expired but still inside the CP refresh leeway
                // (e.g. app suspended past expiry). Try one refresh before
                // dropping the session so an overnight-suspended app doesn't
                // hard-log-out when the token is still refreshable.
                switch await refreshRuntimeTokenProactively() {
                case .refreshed:
                    result = await verifyBrowserSession()
                case .rejected:
                    result = .unauthenticated
                case .deferred:
                    result = .indeterminate
                }
            }
        } else if hasNativeRefreshToken {
            switch await refreshRuntimeTokenProactively(requireExistingRuntimeToken: false) {
            case .refreshed:
                result = await verifyBrowserSession()
            case .rejected:
                result = .unauthenticated
            case .deferred:
                result = .indeterminate
            }
        } else if hasRefresh {
            result = await refreshBrowserSession()
        } else if hasSession {
            result = await verifyBrowserSession()
        } else {
            result = .unauthenticated
        }

        switch result {
        case .authenticated:
            isAuthenticated = true
            hasLocalSessionCandidate = true
            authError = nil
            if SharedAuthStore.hasRuntimeToken(for: serverURL) {
                scheduleRuntimeTokenRefresh()
            }
            Task { [weak self] in
                await self?.syncStoredAPNSTokenIfPossible()
            }
        case .indeterminate:
            isAuthenticated = hasRuntimeToken || hasNativeRefreshToken || hasSession || hasRefresh
            hasLocalSessionCandidate = hasRuntimeToken || hasNativeRefreshToken || hasSession || hasRefresh
        case .unauthenticated:
            await clearLocalSession()
        }
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
        logger.info("restore session finished result=\(String(describing: result), privacy: .public) authenticated=\(self.isAuthenticated, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
    }

    func finishLoginFromSharedCookies() async -> Bool {
        SharedAuthStore.saveServerURL(serverURL)
        if serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            authError = "Set your Longhouse server first"
            isAuthenticated = false
            hasLocalSessionCandidate = false
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
        hasLocalSessionCandidate = isSignedIn
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
        isAuthenticated = false
        hasLocalSessionCandidate = false
        authError = nil
        runtimeTokenRefreshTask?.cancel()
        runtimeTokenRefreshTask = nil

        if previousURL != trimmed, !previousURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            SharedAuthStore.clearManagedCookies(for: previousURL)
            SharedAuthStore.removeSharedCookieStorage(for: previousURL)
            TimelineCacheStore.clear(serverURL: previousURL)
            TranscriptSnapshotStore.shared.clear(serverURL: previousURL)
            PushNotificationStore.clearAPNSDeviceSyncState()
            SharedAuthStore.clearRuntimeToken(for: previousURL)
            SharedAuthStore.clearNativeRefreshToken(for: previousURL)
            KeychainHelper.deleteAuthToken()
        }
        SharedAuthStore.primeSharedCookieStorage(for: trimmed)
    }

    func exchangeHostedHandoffCode(_ code: String, handoffVerifier: String) async -> Bool {
        let trimmedCode = code.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedVerifier = handoffVerifier.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedCode.isEmpty else {
            authError = "Hosted sign-in returned without a handoff code"
            return false
        }
        guard !trimmedVerifier.isEmpty else {
            authError = "Hosted sign-in returned without a handoff verifier"
            return false
        }
        guard let url = URL(string: "\(serverURL)/api/auth/accept-native-handoff") else {
            authError = "Invalid server URL"
            return false
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10

        do {
            request.httpBody = try JSONSerialization.data(
                withJSONObject: ["code": trimmedCode, "tenant_state": trimmedVerifier]
            )
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                authError = Self.apiErrorMessage(from: data) ?? "Hosted sign-in failed"
                return false
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let runtimeToken = json["runtime_token"] as? String else {
                authError = "Hosted sign-in returned without a session token"
                return false
            }
            let expiresIn = json["expires_in"] as? Int
            let expiresAt = expiresIn.map { Date().addingTimeInterval(TimeInterval($0)) }
            let refreshToken = json["refresh_token"] as? String
            let refreshExpiresAt = LonghouseAPI.parseServerDate(json["refresh_token_expires_at"] as? String)
            return await finishHostedRuntimeToken(
                runtimeToken,
                expiresAt: expiresAt,
                refreshToken: refreshToken,
                refreshExpiresAt: refreshExpiresAt
            )
        } catch {
            authError = "Network error: \(error.localizedDescription)"
            return false
        }
    }

    func finishHostedRuntimeToken(
        _ runtimeToken: String,
        expiresAt: Date? = nil,
        refreshToken: String? = nil,
        refreshExpiresAt: Date? = nil
    ) async -> Bool {
        let token = runtimeToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else {
            authError = "Hosted sign-in returned without a session token"
            return false
        }
        guard URL(string: serverURL) != nil else {
            authError = "Invalid server URL"
            return false
        }

        SharedAuthStore.clearManagedCookies(for: serverURL)
        SharedAuthStore.removeSharedCookieStorage(for: serverURL)
        SharedAuthStore.saveHostedTokens(
            runtimeToken: token,
            runtimeExpiresAt: expiresAt,
            refreshToken: refreshToken,
            refreshExpiresAt: refreshExpiresAt,
            for: serverURL
        )

        let result = await verifyBrowserSession()
        guard result == .authenticated else {
            SharedAuthStore.clearRuntimeToken(for: serverURL)
            SharedAuthStore.clearNativeRefreshToken(for: serverURL)
            authError = "Hosted sign-in failed"
            isAuthenticated = false
            hasLocalSessionCandidate = false
            isValidating = false
            return false
        }

        authError = nil
        isAuthenticated = true
        hasLocalSessionCandidate = true
        isValidating = false
        scheduleRuntimeTokenRefresh()
        await syncStoredAPNSTokenIfPossible()
        WidgetCenter.shared.reloadAllTimelines()
        return true
    }

    func clearAuthError() {
        authError = nil
    }

    func handleExpiredSession() {
        Task {
            await clearLocalSession()
            isValidating = false
            WidgetCenter.shared.reloadAllTimelines()
        }
    }

    func resetForUITests() async {
        let previousURL = serverURL

        serverURL = ""
        isAuthenticated = false
        hasLocalSessionCandidate = false
        isValidating = false
        authError = nil
        hostedAuthAttemptURL = nil
        runtimeTokenRefreshTask?.cancel()
        runtimeTokenRefreshTask = nil

        if !previousURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            SharedAuthStore.clearManagedCookies(for: previousURL)
            SharedAuthStore.removeSharedCookieStorage(for: previousURL)
            SharedAuthStore.clearRuntimeToken(for: previousURL)
            SharedAuthStore.clearNativeRefreshToken(for: previousURL)
        }

        KeychainHelper.deleteAuthToken()
        KeychainHelper.deleteServerURL()
        PushNotificationStore.clearAPNSDeviceSyncState()
        TimelineCacheStore.clear()
        TranscriptSnapshotStore.shared.clearAll()
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
        isAuthenticated = false
        hasLocalSessionCandidate = false
        authError = nil
        // Cancel any in-flight proactive refresh for the previous server before
        // switching — its post-await guard checks serverURL equality, but
        // cancelling avoids a wasted network call and a stale reschedule.
        runtimeTokenRefreshTask?.cancel()
        runtimeTokenRefreshTask = nil

        Task {
            if previousURL != trimmed {
                SharedAuthStore.clearManagedCookies(for: previousURL)
                SharedAuthStore.removeSharedCookieStorage(for: previousURL)
                TimelineCacheStore.clear(serverURL: previousURL)
                TranscriptSnapshotStore.shared.clear(serverURL: previousURL)
                PushNotificationStore.clearAPNSDeviceSyncState()
                SharedAuthStore.clearRuntimeToken(for: previousURL)
                SharedAuthStore.clearNativeRefreshToken(for: previousURL)
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
        let startedAt = Date()
        guard isAuthenticated, let api = LonghouseAPI(host: serverURL) else {
            return
        }
        guard let deviceToken = PushNotificationStore.storedDeviceToken() else {
            return
        }
        let signature = PushNotificationStore.apnsDeviceRegistrationSignature(
            serverURL: serverURL,
            deviceToken: deviceToken,
            pushEnvironment: PushNotificationStore.pushEnvironment,
            appBuildId: PushNotificationStore.currentAppBuildID,
            platform: "ios"
        )
        guard apnsSyncInFlightSignature != signature else {
            logger.debug("apns sync skipped reason=in_flight")
            return
        }
        guard PushNotificationStore.shouldSyncAPNSDevice(signature: signature) else {
            logger.debug("apns sync skipped reason=fresh")
            return
        }
        apnsSyncInFlightSignature = signature
        defer {
            if apnsSyncInFlightSignature == signature {
                apnsSyncInFlightSignature = nil
            }
        }
        do {
            try await api.registerAPNSDevice(
                deviceToken: deviceToken,
                pushEnvironment: PushNotificationStore.pushEnvironment,
                appBuildId: PushNotificationStore.currentAppBuildID
            )
            PushNotificationStore.markAPNSDeviceSynced(signature: signature)
            logger.debug("apns sync finished elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
        } catch LonghouseAPIError.notAuthenticated {
            return
        } catch {
            logger.error("apns sync failed elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    private enum SessionRestoreResult: Equatable {
        case authenticated
        case unauthenticated
        case indeterminate
    }

    private enum RuntimeTokenRefreshResult: Equatable {
        case refreshed
        case rejected
        case deferred
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
        if let authorizationHeader = SharedAuthStore.authorizationHeader(for: serverURL) {
            request.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
        }

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

    /// Schedule a proactive runtime-token refresh ~60s before the stored
    /// expiry. Falls back to the 401-retry path in ``LonghouseAPI`` when the
    /// expiry is unknown or the proactive refresh fails.
    private func scheduleRuntimeTokenRefresh() {
        runtimeTokenRefreshTask?.cancel()
        guard let expiresAt = SharedAuthStore.runtimeTokenExpiresAt(for: serverURL) else {
            // Expiry unknown (e.g. legacy direct-token handoff). The 401-retry
            // path in LonghouseAPI will refresh when the token eventually 401s.
            return
        }
        let leadTime: TimeInterval = 60
        let delay = max(expiresAt.timeIntervalSinceNow - leadTime, 5)
        runtimeTokenRefreshTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            // Don't refresh after signout — the guard prevents resurrecting a
            // cleared session if the task fired before cancellation landed.
            // restoreSession calls refreshRuntimeTokenProactively directly and
            // does not go through this task path.
            guard await self?.isAuthenticated == true else { return }
            _ = await self?.refreshRuntimeTokenProactively()
        }
    }

    private func refreshRuntimeTokenProactively(requireExistingRuntimeToken: Bool = true) async -> RuntimeTokenRefreshResult {
        // Snapshot the server URL: signout/server-switch may land during the
        // await and we must not write a refreshed token back into a cleared or
        // switched keychain slot.
        let capturedServerURL = serverURL
        guard let api = LonghouseAPI(host: capturedServerURL) else {
            return .rejected
        }
        do {
            try await api.refreshRuntimeToken()
            guard serverURL == capturedServerURL else {
                // Session was cleared or server switched while we were refreshing.
                return .deferred
            }
            guard !requireExistingRuntimeToken || SharedAuthStore.hasRuntimeToken(for: capturedServerURL) else {
                return .deferred
            }
            scheduleRuntimeTokenRefresh()
            return .refreshed
        } catch LonghouseAPIError.notAuthenticated {
            return .rejected
        } catch {
            // Leave the existing token in place; the 401-retry path handles it
            // when the token finally expires. During restore this keeps the
            // cached shell visible instead of logging out on a deploy/network
            // blip.
            logger.error("proactive runtime token refresh failed error=\(error.localizedDescription, privacy: .public)")
            return .deferred
        }
    }

    private func signOutLocallyAndRemotely() async {
        let capturedServerURL = serverURL
        let nativeRefreshToken = SharedAuthStore.nativeRefreshToken(for: capturedServerURL)

        if let nativeRefreshToken, let url = URL(string: "\(capturedServerURL)/api/auth/revoke-native-session") {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["refresh_token": nativeRefreshToken])
            _ = try? await URLSession.shared.data(for: request)
        }

        // Fire-and-forget the server logout while cookies are still present.
        if let url = URL(string: "\(capturedServerURL)/api/auth/logout") {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            if let authorizationHeader = SharedAuthStore.authorizationHeader(for: capturedServerURL) {
                request.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
            }
            _ = try? await URLSession.shared.data(for: request)
        }

        GIDSignIn.sharedInstance.signOut()
        await clearLocalSession()
        authError = nil
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
    }

    private func clearLocalSession() async {
        runtimeTokenRefreshTask?.cancel()
        runtimeTokenRefreshTask = nil
        SharedAuthStore.clearManagedCookies(for: serverURL)
        SharedAuthStore.removeSharedCookieStorage(for: serverURL)
        SharedAuthStore.clearRuntimeToken(for: serverURL)
        SharedAuthStore.clearNativeRefreshToken(for: serverURL)
        WidgetSessionSnapshotStore.clear()
        TimelineCacheStore.clear(serverURL: serverURL)
        TranscriptSnapshotStore.shared.clear(serverURL: serverURL)
        PushNotificationStore.clearAPNSDeviceSyncState()
        KeychainHelper.deleteAuthToken()
        isAuthenticated = false
        hasLocalSessionCandidate = false
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
