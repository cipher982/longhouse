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
    @Environment(\.scenePhase) private var scenePhase

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
                .onChange(of: scenePhase) { _, phase in
                    RunBreadcrumb.shared.updateScene(Self.sceneName(phase))
                }
                .task {
                    await RunBreadcrumb.shared.begin(scene: Self.sceneName(scenePhase))
#if DEBUG
                    MainThreadStallMonitor.shared.startIfEnabled()
#endif
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
                        await appState.adoptHeadlessCredentialsIfProvided()
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

    private static func sceneName(_ phase: ScenePhase) -> String {
        switch phase {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "unknown"
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
    private struct LocalCredentialSnapshot: Sendable {
        let hasRefreshCookie: Bool
        let hasSessionCookie: Bool
        let hasRuntimeToken: Bool
        let hasNativeRefreshToken: Bool

        var hasCandidate: Bool {
            hasRuntimeToken || hasNativeRefreshToken || hasSessionCookie || hasRefreshCookie
        }
    }

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
        // App-group defaults are the fast launch source. Keychain reads can
        // block for seconds after device unlock and must not run in init on the
        // main actor; restoreSession validates credentials asynchronously.
        let savedServerURL = SharedAuthStore.loadServerURL() ?? ""
        let trimmedServerURL = savedServerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        // A configured server is enough to paint the cached shell immediately.
        // Missing/expired credentials are resolved by restoreSession moments
        // later without freezing timeline interaction.
        let hasCandidate = !trimmedServerURL.isEmpty
        self.serverURL = savedServerURL
        self.hasLocalSessionCandidate = hasCandidate
        self.isAuthenticated = false
        self.isValidating = hasCandidate
        logger.info("local session candidate loaded has_server=\((!trimmedServerURL.isEmpty), privacy: .public) candidate=\(hasCandidate, privacy: .public)")
    }

    var shouldShowAuthenticatedShell: Bool {
        isAuthenticated || hasLocalSessionCandidate
    }

    /// Headless sign-in for the simulator lane: a Debug launch can carry the
    /// server URL and a runtime token in its environment, so an agent gets an
    /// authenticated app without tapping through Google. The credential is
    /// stored exactly where the hosted sign-in stores it; `restoreSession`
    /// then treats it like any other locally trusted token.
    static let headlessServerURLEnvironmentKey = "LONGHOUSE_HEADLESS_SERVER_URL"
    static let headlessAuthTokenEnvironmentKey = "LONGHOUSE_HEADLESS_AUTH_TOKEN"
    /// A session to open once the timeline is up, through the same pending-id
    /// path a push or deep link uses. A URL opened from outside the app would
    /// stop at the system's "Open in Longhouse?" prompt; the environment does not.
    static let headlessOpenSessionEnvironmentKey = "LONGHOUSE_HEADLESS_OPEN_SESSION"

    func adoptHeadlessCredentialsIfProvided() async {
        #if DEBUG
        let environment = ProcessInfo.processInfo.environment
        if let sessionID = environment[Self.headlessOpenSessionEnvironmentKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !sessionID.isEmpty {
            PushNotificationStore.storePendingSessionID(sessionID)
            logger.info("headless open session queued session=\(sessionID, privacy: .public)")
        }
        guard let url = environment[Self.headlessServerURLEnvironmentKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
              let token = environment[Self.headlessAuthTokenEnvironmentKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
              !url.isEmpty, !token.isEmpty, URL(string: url) != nil
        else { return }
        serverURL = url
        KeychainHelper.saveServerURL(url)
        SharedAuthStore.saveServerURL(url)
        SharedAuthStore.advanceAuthGeneration(for: url)
        SharedAuthStore.clearManagedCookies(for: url)
        SharedAuthStore.removeSharedCookieStorage(for: url)
        SharedAuthStore.saveRuntimeToken(token, for: url)
        logger.info("headless credentials adopted server=\(url, privacy: .public)")
        #endif
    }

    func restoreSession() async {
        let startedAt = Date()
        let capturedServerURL = serverURL
        let expectedGeneration = SharedAuthStore.authGeneration(for: capturedServerURL)
        isValidating = true
        hostedAuthAttemptURL = nil
        SharedAuthStore.saveServerURL(capturedServerURL)
        let trimmedServerURL = capturedServerURL.trimmingCharacters(in: .whitespacesAndNewlines)
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

        await retryPendingNativeRevocation(for: trimmedServerURL)
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(expectedGeneration, for: capturedServerURL) else {
            return
        }
        // Security.framework calls are synchronous and occasionally take
        // several seconds on a freshly-unlocked physical device. Load all
        // credential state off the main actor so the cached timeline remains
        // scrollable while authentication restores.
        let credentialLoadStartedAt = Date()
        let credentials = await Self.loadCredentialSnapshot(serverURL: trimmedServerURL)
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(expectedGeneration, for: capturedServerURL) else {
            return
        }
        logger.info("auth credentials loaded elapsed_ms=\(Int(Date().timeIntervalSince(credentialLoadStartedAt) * 1000), privacy: .public) runtime_token=\(credentials.hasRuntimeToken, privacy: .public) session_cookie=\(credentials.hasSessionCookie, privacy: .public)")
        let hasRefresh = credentials.hasRefreshCookie
        let hasSession = credentials.hasSessionCookie
        let hasRuntimeToken = credentials.hasRuntimeToken
        let hasNativeRefreshToken = credentials.hasNativeRefreshToken
        hasLocalSessionCandidate = credentials.hasCandidate

        let result: SessionRestoreResult
        if hasSession || (hasRuntimeToken && hasNativeRefreshToken) {
            // The cached timeline is already useful and every real API request
            // handles 401 + token/cookie refresh. A separate cold-start verify
            // duplicated the first network round trip and, on physical devices,
            // could spend 5-8 seconds bringing up CFNetwork before the app felt
            // interactive. Trust only a complete hosted credential pair.
            result = .authenticated
            logger.info("auth restore accepted local credential network_verify=false")
        } else if hasNativeRefreshToken {
            switch await refreshHostedSessionProactively(
                host: capturedServerURL,
                expectedGeneration: expectedGeneration,
                requireExistingRuntimeToken: false
            ) {
            case .refreshed:
                result = .authenticated
            case .rejected:
                result = .unauthenticated
            case .deferred:
                result = .indeterminate
            }
        } else if hasRefresh {
            result = await refreshBrowserSession(
                serverURL: capturedServerURL,
                expectedGeneration: expectedGeneration
            )
        } else {
            result = .unauthenticated
        }

        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(expectedGeneration, for: capturedServerURL) else {
            return
        }
        switch result {
        case .authenticated:
            isAuthenticated = true
            hasLocalSessionCandidate = true
            authError = nil
            if hasRuntimeToken && hasNativeRefreshToken {
                scheduleRuntimeTokenRefresh()
            }
            Task { [weak self] in
                await self?.syncStoredAPNSTokenIfPossible()
            }
        case .indeterminate:
            isAuthenticated = hasRuntimeToken || hasNativeRefreshToken || hasSession || hasRefresh
            hasLocalSessionCandidate = hasRuntimeToken || hasNativeRefreshToken || hasSession || hasRefresh
        case .unauthenticated:
            await clearLocalSession(preservePendingNativeRevocation: true)
        }
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
        logger.info("restore session finished result=\(String(describing: result), privacy: .public) authenticated=\(self.isAuthenticated, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
    }

    func finishLoginFromSharedCookies() async -> Bool {
        let capturedServerURL = serverURL
        let generation = SharedAuthStore.authGeneration(for: capturedServerURL)
        SharedAuthStore.saveServerURL(capturedServerURL)
        if capturedServerURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            authError = "Set your Longhouse server first"
            isAuthenticated = false
            hasLocalSessionCandidate = false
            isValidating = false
            return false
        }

        await Task.detached(priority: .userInitiated) {
            SharedAuthStore.captureCookiesFromSharedStorage(for: capturedServerURL)
        }.value
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
            return false
        }
        let isSignedIn = SharedAuthStore.hasManagedCookies(for: capturedServerURL)

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
        if previousURL != trimmed, !previousURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            SharedAuthStore.advanceAuthGeneration(for: previousURL)
        }
        serverURL = trimmed
        SharedAuthStore.advanceAuthGeneration(for: trimmed)
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
        let capturedServerURL = serverURL
        let generation = SharedAuthStore.authGeneration(for: capturedServerURL)
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
        guard let url = URL(string: "\(capturedServerURL)/api/auth/accept-native-handoff") else {
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
            guard serverURL == capturedServerURL,
                  SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
                return false
            }
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                authError = Self.apiErrorMessage(from: data) ?? "Hosted sign-in failed"
                return false
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let runtimeToken = (json["runtime_token"] as? String)?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                  !runtimeToken.isEmpty,
                  let expiresIn = json["expires_in"] as? Int,
                  expiresIn > 0,
                  let refreshToken = (json["refresh_token"] as? String)?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                  !refreshToken.isEmpty,
                  let refreshExpiry = json["refresh_token_expires_at"] as? String,
                  let refreshExpiresAt = LonghouseAPI.parseServerDate(refreshExpiry),
                  refreshExpiresAt > Date() else {
                authError = "Hosted sign-in returned incomplete session credentials"
                return false
            }
            let expiresAt = Date().addingTimeInterval(TimeInterval(expiresIn))
            return await finishHostedRuntimeToken(
                runtimeToken,
                expiresAt: expiresAt,
                refreshToken: refreshToken,
                refreshExpiresAt: refreshExpiresAt,
                expectedGeneration: generation
            )
        } catch {
            guard serverURL == capturedServerURL,
                  SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
                return false
            }
            authError = "Network error: \(error.localizedDescription)"
            return false
        }
    }

    func finishHostedRuntimeToken(
        _ runtimeToken: String,
        expiresAt: Date? = nil,
        refreshToken: String,
        refreshExpiresAt: Date? = nil,
        expectedGeneration: String? = nil
    ) async -> Bool {
        let capturedServerURL = serverURL
        let generation = expectedGeneration ?? SharedAuthStore.authGeneration(for: capturedServerURL)
        let token = runtimeToken.trimmingCharacters(in: .whitespacesAndNewlines)
        let refresh = refreshToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, !refresh.isEmpty else {
            authError = "Hosted sign-in returned incomplete session credentials"
            return false
        }
        guard URL(string: capturedServerURL) != nil else {
            authError = "Invalid server URL"
            return false
        }
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
            return false
        }

        SharedAuthStore.clearManagedCookies(for: capturedServerURL)
        SharedAuthStore.removeSharedCookieStorage(for: capturedServerURL)
        guard SharedAuthStore.saveHostedTokens(
            runtimeToken: token,
            runtimeExpiresAt: expiresAt,
            refreshToken: refresh,
            refreshExpiresAt: refreshExpiresAt,
            for: capturedServerURL,
            expectedGeneration: generation
        ) else {
            return false
        }

        let result = await verifyBrowserSession(serverURL: capturedServerURL)
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
            return false
        }
        guard result == .authenticated else {
            if result == .unauthenticated {
                SharedAuthStore.clearRuntimeToken(for: capturedServerURL)
                SharedAuthStore.clearNativeRefreshToken(for: capturedServerURL)
            }
            authError = result == .indeterminate
                ? "Hosted sign-in could not be verified. Try again when the instance is available."
                : "Hosted sign-in failed"
            isAuthenticated = false
            hasLocalSessionCandidate = result == .indeterminate
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
        if previousURL != trimmed {
            SharedAuthStore.advanceAuthGeneration(for: previousURL)
        }
        serverURL = trimmed
        SharedAuthStore.advanceAuthGeneration(for: trimmed)
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

    private func refreshBrowserSession(
        serverURL: String,
        expectedGeneration: String
    ) async -> SessionRestoreResult {
        let startedAt = Date()
        guard let url = URL(string: "\(serverURL)/api/auth/refresh") else {
            return .unauthenticated
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 8

        do {
            guard let statusCode = try await Self.performAuthRequest(request) else {
                logger.info("auth refresh finished result=indeterminate elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
                return .indeterminate
            }
            guard self.serverURL == serverURL,
                  SharedAuthStore.isAuthGenerationCurrent(expectedGeneration, for: serverURL) else {
                return .indeterminate
            }

            if statusCode == 200 {
                await Task.detached(priority: .userInitiated) {
                    SharedAuthStore.captureCookiesFromSharedStorage(for: serverURL)
                }.value
                logger.info("auth refresh finished result=authenticated status=200 elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
                return .authenticated
            }
            logger.info("auth refresh finished result=\(statusCode == 401 ? "unauthenticated" : "indeterminate", privacy: .public) status=\(statusCode, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return statusCode == 401 ? .unauthenticated : .indeterminate
        } catch {
            logger.info("auth refresh finished result=indeterminate transport_error=true elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return .indeterminate
        }
    }

    private func verifyBrowserSession(serverURL: String) async -> SessionRestoreResult {
        let startedAt = Date()
        guard let url = URL(string: "\(serverURL)/api/auth/verify") else {
            return .unauthenticated
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        let authorizationHeader = await Task.detached(priority: .userInitiated) {
            SharedAuthStore.authorizationHeader(for: serverURL)
        }.value
        if let authorizationHeader {
            request.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
        }

        do {
            guard let statusCode = try await Self.performAuthRequest(request) else {
                logger.info("auth verify finished result=indeterminate elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
                return .indeterminate
            }

            if statusCode == 204 {
                logger.info("auth verify finished result=authenticated status=204 elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
                return .authenticated
            }
            logger.info("auth verify finished result=\(statusCode == 401 ? "unauthenticated" : "indeterminate", privacy: .public) status=\(statusCode, privacy: .public) elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return statusCode == 401 ? .unauthenticated : .indeterminate
        } catch {
            logger.info("auth verify finished result=indeterminate transport_error=true elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return .indeterminate
        }
    }

    private nonisolated static func performAuthRequest(_ request: URLRequest) async throws -> Int? {
        let (_, response) = try await URLSession.shared.data(for: request)
        return (response as? HTTPURLResponse)?.statusCode
    }

    private nonisolated static func loadCredentialSnapshot(serverURL: String) async -> LocalCredentialSnapshot {
        await Task.detached(priority: .userInitiated) {
            let cookies = SharedAuthStore.managedCookies(for: serverURL)
            let runtimeToken = SharedAuthStore.runtimeToken(for: serverURL)
            let nativeRefreshToken = SharedAuthStore.nativeRefreshToken(for: serverURL)
            for cookie in cookies {
                HTTPCookieStorage.shared.setCookie(cookie)
            }
            return LocalCredentialSnapshot(
                hasRefreshCookie: cookies.contains {
                    $0.name == SharedAuthStore.activeRefreshCookieName(for: serverURL)
                },
                hasSessionCookie: cookies.contains {
                    $0.name == SharedAuthStore.activeSessionCookieName(for: serverURL)
                },
                hasRuntimeToken: runtimeToken != nil,
                hasNativeRefreshToken: nativeRefreshToken != nil
            )
        }.value
    }

    /// Schedule a proactive hosted-session refresh ~60s before the stored
    /// access-token expiry. If expiry is unavailable, the authenticated API
    /// path still refreshes after a 401.
    private func scheduleRuntimeTokenRefresh() {
        runtimeTokenRefreshTask?.cancel()
        guard let expiresAt = SharedAuthStore.runtimeTokenExpiresAt(for: serverURL) else {
            return
        }
        let leadTime: TimeInterval = 60
        let delay = max(expiresAt.timeIntervalSinceNow - leadTime, 5)
        runtimeTokenRefreshTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            // Don't refresh after signout — the guard prevents resurrecting a
            // cleared session if the task fired before cancellation landed.
            // restoreSession calls refreshHostedSessionProactively directly
            // and does not go through this task path.
            guard self?.isAuthenticated == true else { return }
            _ = await self?.refreshHostedSessionProactively()
        }
    }
    private func refreshHostedSessionProactively(
        host: String? = nil,
        expectedGeneration: String? = nil,
        requireExistingRuntimeToken: Bool = true
    ) async -> RuntimeTokenRefreshResult {
        let startedAt = Date()
        // Snapshot the server URL and generation: signout/server-switch may
        // land during the await and must fence the response.
        let capturedServerURL = host ?? serverURL
        let generation = expectedGeneration ?? SharedAuthStore.authGeneration(for: capturedServerURL)
        guard serverURL == capturedServerURL,
              SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL),
              let api = LonghouseAPI(host: capturedServerURL) else {
            return .deferred
        }
        do {
            try await api.refreshHostedSession()
            guard serverURL == capturedServerURL,
                  SharedAuthStore.isAuthGenerationCurrent(generation, for: capturedServerURL) else {
                return .deferred
            }
            guard !requireExistingRuntimeToken || SharedAuthStore.hasRuntimeToken(for: capturedServerURL) else {
                return .deferred
            }
            scheduleRuntimeTokenRefresh()
            logger.info("hosted session refresh finished result=refreshed elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return .refreshed
        } catch LonghouseAPIError.notAuthenticated {
            // ``LonghouseAPI`` fences and clears a definitive 401/403. Do
            // not let an older refresh response restore the app shell after
            // that invalidation, but also do not overwrite a newly-established
            // session that won the race.
            if serverURL == capturedServerURL,
               !SharedAuthStore.hasRuntimeToken(for: capturedServerURL),
               !SharedAuthStore.hasNativeRefreshToken(for: capturedServerURL) {
                isAuthenticated = false
                hasLocalSessionCandidate = false
                isValidating = false
            }
            logger.info("hosted session refresh finished result=rejected elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return .rejected
        } catch {
            // Leave the existing pair in place; a network/deploy blip should
            // not erase a usable cached shell.
            logger.error("proactive hosted session refresh failed error=\(error.localizedDescription, privacy: .public)")
            logger.info("hosted session refresh finished result=deferred elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000), privacy: .public)")
            return .deferred
        }
    }

    private func retryPendingNativeRevocation(for serverURL: String) async {
        guard let token = SharedAuthStore.pendingNativeRevocationToken(for: serverURL),
              let url = URL(string: "\(serverURL)/api/auth/revoke-native-session") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["refresh_token": token])
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  (200..<300).contains(httpResponse.statusCode) else {
                return
            }
            SharedAuthStore.clearPendingNativeRevocationToken(for: serverURL)
            if SharedAuthStore.nativeRefreshToken(for: serverURL) == token {
                SharedAuthStore.clearNativeRefreshToken(for: serverURL)
            }
        } catch {
            logger.warning("pending native revocation retry deferred error=\(error.localizedDescription, privacy: .public)")
        }
    }

    private func signOutLocallyAndRemotely() async {
        let capturedServerURL = serverURL
        let nativeRefreshToken = SharedAuthStore.nativeRefreshToken(for: capturedServerURL)
        var nativeRevocationConfirmed = true

        if let nativeRefreshToken, let url = URL(string: "\(capturedServerURL)/api/auth/revoke-native-session") {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["refresh_token": nativeRefreshToken])
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                nativeRevocationConfirmed = (response as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
            } catch {
                nativeRevocationConfirmed = false
                logger.error("native signout revocation failed error=\(error.localizedDescription, privacy: .public)")
            }
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

        // A concurrent server switch owns the old credential slot. Never let
        // this sign-out finish by clearing or reporting state for the new one.
        guard serverURL == capturedServerURL else {
            return
        }
        GIDSignIn.sharedInstance.signOut()
        await clearLocalSession(clearNativeRefreshToken: nativeRevocationConfirmed)
        authError = nativeRevocationConfirmed
            ? nil
            : "Sign-out could not be confirmed. Try again when the account service is available."
        isValidating = false
        WidgetCenter.shared.reloadAllTimelines()
    }

    private func clearLocalSession(
        clearNativeRefreshToken: Bool = true,
        preservePendingNativeRevocation: Bool = false
    ) async {
        let capturedServerURL = serverURL
        let retainedRefreshToken = SharedAuthStore.nativeRefreshToken(for: capturedServerURL)
        let pendingRefreshToken = SharedAuthStore.pendingNativeRevocationToken(for: capturedServerURL)
        SharedAuthStore.advanceAuthGeneration(for: capturedServerURL)
        runtimeTokenRefreshTask?.cancel()
        runtimeTokenRefreshTask = nil
        SharedAuthStore.clearManagedCookies(for: capturedServerURL)
        SharedAuthStore.removeSharedCookieStorage(for: capturedServerURL)
        SharedAuthStore.clearRuntimeToken(for: capturedServerURL)
        // Never retain a failed-revocation token in the active credential slot.
        // It is retry-only state and is deliberately unreadable by restore.
        SharedAuthStore.clearNativeRefreshToken(for: capturedServerURL)
        if clearNativeRefreshToken && !preservePendingNativeRevocation {
            SharedAuthStore.clearPendingNativeRevocationToken(for: capturedServerURL)
        } else if let token = retainedRefreshToken ?? pendingRefreshToken {
            SharedAuthStore.savePendingNativeRevocationToken(token, for: capturedServerURL)
        }
        WidgetSessionSnapshotStore.clear()
        TimelineCacheStore.clear(serverURL: capturedServerURL)
        TranscriptSnapshotStore.shared.clear(serverURL: capturedServerURL)
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
