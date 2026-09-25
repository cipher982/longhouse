import AuthenticationServices
import SwiftUI

// `/api/auth/methods` also reports `google`; the iOS app has no Google
// sign-in (hosted tenants use the control-plane web flow), so it is ignored.
private struct AuthMethods: Decodable {
    let password: Bool
    let sso: Bool
    let ssoURL: String?
    let ssoLoginURL: String?

    private enum CodingKeys: String, CodingKey {
        case password
        case sso
        case ssoURL = "sso_url"
        case ssoLoginURL = "sso_login_url"
    }
}

@MainActor
private final class AuthPresentationContextProvider: NSObject, ObservableObject, ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows)
            .first(where: \.isKeyWindow) ?? ASPresentationAnchor()
    }
}

@MainActor
struct LoginView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var authPresentationContext = AuthPresentationContextProvider()
    @State private var authMethods: AuthMethods?
    @State private var hostedAuthSession: ASWebAuthenticationSession?
    @State private var hostedHandoffVerifier: String?
    @State private var hostedCodeVerifier: String?
    @State private var forceEphemeralHostedSignIn = false
    @State private var isLoadingAuthMethods = false
    @State private var isSigningIn = false
    @State private var localErrorMessage: String?
    @State private var password = ""
    private var hasConfiguredServer: Bool {
        !appState.serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        ZStack {
            LoginHearth()

            VStack(spacing: 32) {
                Spacer()

                VStack(spacing: 14) {
                    Image("LonghouseMascot")
                        .resizable()
                        .scaledToFit()
                        .frame(width: 96, height: 96)
                        .shadow(color: LoginInk.gold.opacity(0.35), radius: 28, y: 6)
                        .accessibilityHidden(true)
                        .padding(.bottom, 6)

                    Text("Longhouse")
                        .font(Ember.serif(40, relativeTo: .largeTitle))
                        .foregroundStyle(LoginInk.parchment)

                    Text("Mission control for your AI agents")
                        .font(Ember.serif(17, relativeTo: .subheadline, italic: true))
                        .foregroundStyle(LoginInk.clay)
                }

                Spacer()

                VStack(spacing: 16) {
                    if !hasConfiguredServer {
                        hostedBootstrapControls
                    } else if isLoadingAuthMethods && authMethods == nil {
                        ProgressView()
                            .tint(LoginInk.gold)
                            .scaleEffect(1.2)
                    } else if isSigningIn {
                        ProgressView()
                            .tint(LoginInk.gold)
                            .scaleEffect(1.2)
                    } else if let authMethods {
                        authControls(for: authMethods)
                    } else {
                        Button(action: { Task { await loadAuthMethods() } }) {
                            Text("Retry Sign In")
                                .font(.system(size: 16, weight: .medium))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 14)
                                .background(LoginInk.gold.opacity(0.13))
                                .foregroundStyle(LoginInk.parchment)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 12)
                                        .strokeBorder(LoginInk.gold.opacity(0.5), lineWidth: 1)
                                )
                        }
                    }

                    if let errorMessage = displayedErrorMessage {
                        Text(errorMessage)
                            .font(.caption)
                            .foregroundStyle(LoginInk.ember)
                            .multilineTextAlignment(.center)
                    }
                }
                .padding(.horizontal, 40)

                Spacer()
                    .frame(height: 60)
            }
        }
        // Always the hall at night, so the status bar must be light too.
        .environment(\.colorScheme, .dark)
        .preferredColorScheme(.dark)
        .task(id: appState.serverURL) {
            await loadAuthMethods()
        }
    }

    @ViewBuilder
    private func authControls(for methods: AuthMethods) -> some View {
        // Hosted tenants: one button, one path. The CP /auth/start
        // route (or /auth/native/open-instance for the iOS deep-link
        // flow) handles Google / GitHub / email. The /api/auth/methods
        // response does not advertise password on hosted tenants.
        if methods.sso {
            Button(action: { startHostedSignIn(methods) }) {
                HStack(spacing: 10) {
                    Image(systemName: "arrow.up.forward.square.fill")
                        .font(.system(size: 18))
                    Text("Continue with Longhouse")
                        .font(.system(size: 16, weight: .medium))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(LoginInk.gold.opacity(0.13))
                .foregroundStyle(LoginInk.parchment)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(LoginInk.gold.opacity(0.5), lineWidth: 1)
                )
            }
            .accessibilityIdentifier("login.continueWithLonghouse")

            Text("Hosted instances sign in through the Longhouse control plane.")
                .font(.caption)
                .foregroundStyle(LoginInk.muted)
                .multilineTextAlignment(.center)
            if forceEphemeralHostedSignIn {
                Button("Sign in with a different Longhouse account") {
                    startHostedSignIn(methods, ephemeral: true)
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(LoginInk.clay)
                .accessibilityIdentifier("login.switchLonghouseAccount")
            }
        } else {
            // Self-host fallback: password form. This path is exercised
            // only on installs with no CONTROL_PLANE_URL set.
            legacyAuthControls(for: methods)
        }
    }

    @ViewBuilder
    private func legacyAuthControls(for methods: AuthMethods) -> some View {
        if methods.password {
            SecureField("Password", text: $password)
                .textContentType(.password)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(LoginInk.gold.opacity(0.13))
                .foregroundStyle(LoginInk.parchment)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(LoginInk.gold.opacity(0.5), lineWidth: 1)
                )

            Button(action: signInWithPassword) {
                Text("Sign in")
                    .font(.system(size: 16, weight: .medium))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(LoginInk.gold.opacity(0.13))
                    .foregroundStyle(LoginInk.parchment)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .strokeBorder(LoginInk.gold.opacity(0.5), lineWidth: 1)
                    )
            }
            .disabled(password.isEmpty)
            .opacity(password.isEmpty ? 0.6 : 1)
        }

        if !methods.sso && !methods.password {
            Text("This Longhouse server does not advertise a supported sign-in method.")
                .font(.caption)
                .foregroundStyle(LoginInk.muted)
                .multilineTextAlignment(.center)
        }
    }

    @ViewBuilder
    private var hostedBootstrapControls: some View {
        Button(action: { startHostedBootstrapSignIn() }) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.up.forward.square.fill")
                    .font(.system(size: 18))
                Text("Continue with Longhouse")
                    .font(.system(size: 16, weight: .medium))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(LoginInk.gold.opacity(0.13))
            .foregroundStyle(LoginInk.parchment)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(LoginInk.gold.opacity(0.5), lineWidth: 1)
            )
        }
        .accessibilityIdentifier("login.continueWithLonghouse")

        Text("Hosted Longhouse accounts sign in through the control plane. Custom or self-hosted servers can still be set from the server icon.")
            .font(.caption)
            .foregroundStyle(LoginInk.muted)
            .multilineTextAlignment(.center)

        if let hostedAuthAttemptURL = appState.hostedAuthAttemptURL,
           UITestHooks.shouldCaptureHostedAuthAttempt {
            Text(hostedAuthAttemptURL)
                .font(.caption2)
                .foregroundStyle(LoginInk.muted)
                .multilineTextAlignment(.center)
                .accessibilityIdentifier("login.hostedAuthAttemptURL")
        }
    }

    private var displayedErrorMessage: String? {
        localErrorMessage ?? appState.authError
    }

    private func loadAuthMethods() async {
        guard hasConfiguredServer else {
            await MainActor.run {
                isLoadingAuthMethods = false
                authMethods = nil
                localErrorMessage = nil
                password = ""
                appState.clearAuthError()
            }
            return
        }

        guard let baseURL = URL(string: appState.serverURL),
              baseURL.scheme?.lowercased() == "https" || baseURL.host == "localhost" || baseURL.host == "127.0.0.1" else {
            await MainActor.run {
                isLoadingAuthMethods = false
                authMethods = nil
                localErrorMessage = "Server URL must use HTTPS"
            }
            return
        }

        guard let url = URL(string: "\(appState.serverURL)/api/auth/methods") else {
            await MainActor.run {
                isLoadingAuthMethods = false
                authMethods = nil
                localErrorMessage = "Invalid server URL"
            }
            return
        }

        await MainActor.run {
            isLoadingAuthMethods = true
            authMethods = nil
            localErrorMessage = nil
            password = ""
            appState.clearAuthError()
        }

        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                let message = Self.apiErrorMessage(from: data) ?? "Failed to load sign-in options (\(statusCode))"
                await MainActor.run {
                    isLoadingAuthMethods = false
                    localErrorMessage = message
                }
                return
            }

            let methods = try JSONDecoder().decode(AuthMethods.self, from: data)

            await MainActor.run {
                authMethods = methods
            }
        } catch {
            await MainActor.run {
                isLoadingAuthMethods = false
                localErrorMessage = "Network error: \(error.localizedDescription)"
            }
            return
        }

        await MainActor.run {
            isLoadingAuthMethods = false
        }
    }

    private func startHostedSignIn(_ methods: AuthMethods, ephemeral: Bool = false) {
        guard methods.ssoURL != nil else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        guard let tenant = tenantSubdomain(from: appState.serverURL) else {
            localErrorMessage = "Invalid Longhouse server URL"
            return
        }
        let expectedServerURL = appState.serverURL

        let handoffVerifier = HostedAuthFlow.makeHandoffVerifier()
        let codeVerifier = HostedAuthFlow.makeCodeVerifier()
        let codeChallenge = HostedAuthFlow.codeChallenge(for: codeVerifier)
        guard let authURL = HostedAuthFlow.openInstanceURL(
            tenant: tenant,
            handoffVerifier: handoffVerifier,
            codeChallenge: codeChallenge
        ) else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        forceEphemeralHostedSignIn = false
        startHostedAuthSession(
            authURL,
            handoffVerifier: handoffVerifier,
            codeVerifier: codeVerifier,
            expectedTenant: tenant,
            expectedServerURL: expectedServerURL,
            ephemeral: ephemeral
        )
    }

    private func startHostedBootstrapSignIn(ephemeral: Bool = false) {
        let handoffVerifier = HostedAuthFlow.makeHandoffVerifier()
        let codeVerifier = HostedAuthFlow.makeCodeVerifier()
        let codeChallenge = HostedAuthFlow.codeChallenge(for: codeVerifier)
        guard let authURL = HostedAuthFlow.openInstanceURL(
            handoffVerifier: handoffVerifier,
            codeChallenge: codeChallenge
        ) else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        startHostedAuthSession(
            authURL,
            handoffVerifier: handoffVerifier,
            codeVerifier: codeVerifier,
            expectedTenant: nil,
            expectedServerURL: nil,
            ephemeral: ephemeral
        )
    }

    private func startHostedAuthSession(
        _ authURL: URL,
        handoffVerifier: String,
        codeVerifier: String,
        expectedTenant: String?,
        expectedServerURL: String?,
        ephemeral: Bool = false
    ) {
        appState.clearAuthError()
        localErrorMessage = nil
        hostedHandoffVerifier = handoffVerifier
        hostedCodeVerifier = codeVerifier

        if UITestHooks.shouldCaptureHostedAuthAttempt {
            appState.recordHostedAuthAttempt(authURL)
            return
        }

        isSigningIn = true

        let session = ASWebAuthenticationSession(
            url: authURL,
            callbackURLScheme: LonghouseAuthConfig.hostedCallbackScheme
        ) { callbackURL, error in
            Task { @MainActor in
                hostedAuthSession = nil
                defer { isSigningIn = false }

                if let error {
                    hostedHandoffVerifier = nil
                    hostedCodeVerifier = nil
                    if (error as NSError).code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        return
                    }
                    localErrorMessage = error.localizedDescription
                    return
                }

                guard let callbackURL else {
                    hostedHandoffVerifier = nil
                    hostedCodeVerifier = nil
                    localErrorMessage = "Hosted sign-in did not return to the app"
                    return
                }

                await handleHostedAuthCallback(
                    callbackURL,
                    expectedTenant: expectedTenant,
                    expectedServerURL: expectedServerURL
                )
            }
        }

        session.presentationContextProvider = authPresentationContext
        session.prefersEphemeralWebBrowserSession = ephemeral
        hostedAuthSession = session

        if !session.start() {
            hostedAuthSession = nil
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            isSigningIn = false
            localErrorMessage = "Failed to start hosted sign-in"
        }
    }

    private func handleHostedAuthCallback(
        _ callbackURL: URL,
        expectedTenant: String?,
        expectedServerURL: String?
    ) async {
        guard let payload = HostedAuthFlow.callbackPayload(from: callbackURL) else {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned an invalid callback"
            return
        }

        // Error callbacks intentionally may omit tenant_state: the control
        // plane has not minted a handoff code, so there is no authenticated
        // success to accept. Surface account-switch and retryable errors before
        // applying the success-only verifier binding.
        if let error = payload.error {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            forceEphemeralHostedSignIn = error == "tenant_not_owned"
            localErrorMessage = friendlyHostedError(error)
            return
        }

        guard let verifier = hostedHandoffVerifier,
              let codeVerifier = hostedCodeVerifier else {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned without a verifier"
            return
        }
        guard payload.tenantState == verifier else {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned an invalid state"
            return
        }
        if let expectedTenant,
           payload.tenant?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() != expectedTenant.lowercased() {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned an unexpected tenant"
            return
        }

        guard let rawInstanceURL = payload.instanceURL,
              let instanceURL = HostedAuthFlow.validatedInstanceURL(
                  rawInstanceURL,
                  tenant: payload.tenant,
                  expectedServerURL: expectedServerURL
              ) else {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned an unexpected instance"
            return
        }
        await appState.prepareServerForHostedLogin(instanceURL)

        guard let code = payload.code else {
            hostedHandoffVerifier = nil
            hostedCodeVerifier = nil
            localErrorMessage = "Hosted sign-in returned without a handoff code"
            return
        }
        let sessionEstablished = await appState.exchangeHostedHandoffCode(
            code,
            handoffVerifier: verifier,
            codeVerifier: codeVerifier
        )
        hostedHandoffVerifier = nil
        hostedCodeVerifier = nil
        if !sessionEstablished {
            localErrorMessage = appState.authError ?? "Hosted sign-in failed"
        }
    }


    private func signInWithPassword() {
        guard !password.isEmpty else {
            return
        }

        appState.clearAuthError()
        isSigningIn = true
        localErrorMessage = nil

        Task {
            await exchangePassword()
        }
    }

    private func exchangePassword() async {
        guard let url = URL(string: "\(appState.serverURL)/api/auth/password") else {
            await MainActor.run { isSigningIn = false; localErrorMessage = "Invalid server URL" }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: ["password": password])

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                let message = Self.apiErrorMessage(from: data) ?? "Sign in failed (\(statusCode))"
                await MainActor.run {
                    isSigningIn = false
                    localErrorMessage = message
                }
                return
            }

            let sessionEstablished = await appState.finishLoginFromSharedCookies()

            await MainActor.run {
                if !sessionEstablished {
                    localErrorMessage = appState.authError ?? "Signed in, but failed to restore the app session"
                } else {
                    password = ""
                }
                isSigningIn = false
            }
        } catch {
            await MainActor.run {
                isSigningIn = false
                localErrorMessage = "Network error: \(error.localizedDescription)"
            }
        }
    }

    private func tenantSubdomain(from serverURL: String) -> String? {
        guard let host = URL(string: serverURL)?.host?.lowercased() else {
            return nil
        }

        let parts = host.split(separator: ".")
        guard let first = parts.first, !first.isEmpty else {
            return nil
        }
        return String(first)
    }

    private func friendlyHostedError(_ rawValue: String) -> String {
        switch rawValue {
        case "instance_not_found":
            return "This Longhouse server does not belong to the authenticated control-plane account."
        default:
            return rawValue.replacingOccurrences(of: "_", with: " ")
        }
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

/// The sign-in screen is always the hall at night, whatever the system
/// appearance: the first thing anyone sees is the fire, not a form.
enum LoginInk {
    static let parchment = Ember.uiHex(0xF6EBD6)
    static let clay = Ember.uiHex(0xC4B096)
    static let muted = Ember.uiHex(0x8A7862)
    static let gold = Ember.uiHex(0xE9B949)
    static let ember = Ember.uiHex(0xE4572E)
}

private struct LoginHearth: View {
    var body: some View {
        ZStack {
            Ember.uiHex(0x0B0908)
            RadialGradient(
                colors: [LoginInk.gold.opacity(0.20), LoginInk.gold.opacity(0.05), .clear],
                center: UnitPoint(x: 0.5, y: 0.36),
                startRadius: 0,
                endRadius: 380
            )
            LinearGradient(
                colors: [.clear, Ember.uiHex(0xF08A24).opacity(0.07)],
                startPoint: .center,
                endPoint: .bottom
            )
        }
        .ignoresSafeArea()
    }
}
