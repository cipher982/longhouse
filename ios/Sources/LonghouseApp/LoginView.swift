import AuthenticationServices
import GoogleSignIn
import SwiftUI

private struct AuthMethods: Decodable {
    let google: Bool
    let password: Bool
    let sso: Bool
    let ssoURL: String?
    let ssoLoginURL: String?

    private enum CodingKeys: String, CodingKey {
        case google
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
    @State private var isLoadingAuthMethods = false
    @State private var isSigningIn = false
    @State private var localErrorMessage: String?
    @State private var password = ""
    private var hasConfiguredServer: Bool {
        !appState.serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        ZStack {
            Color(red: 0.04, green: 0.04, blue: 0.06)
                .ignoresSafeArea()

            VStack(spacing: 32) {
                Spacer()

                VStack(spacing: 12) {
                    Image(systemName: "house.lodge.fill")
                        .font(.system(size: 48))
                        .foregroundStyle(.white.opacity(0.9))

                    Text("Longhouse")
                        .font(.largeTitle.weight(.bold))
                        .foregroundStyle(.white)

                    Text("Mission control for your AI agents")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.6))
                }

                Spacer()

                VStack(spacing: 16) {
                    if !hasConfiguredServer {
                        hostedBootstrapControls
                    } else if isLoadingAuthMethods && authMethods == nil {
                        ProgressView()
                            .tint(.white)
                            .scaleEffect(1.2)
                    } else if isSigningIn {
                        ProgressView()
                            .tint(.white)
                            .scaleEffect(1.2)
                    } else if let authMethods {
                        authControls(for: authMethods)
                    } else {
                        Button(action: { Task { await loadAuthMethods() } }) {
                            Text("Retry Sign In")
                                .font(.system(size: 16, weight: .medium))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 14)
                                .background(.white.opacity(0.08))
                                .foregroundStyle(.white)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 12)
                                        .strokeBorder(.white.opacity(0.15), lineWidth: 1)
                                )
                        }
                    }

                    if let errorMessage = displayedErrorMessage {
                        Text(errorMessage)
                            .font(.caption)
                            .foregroundStyle(.red.opacity(0.8))
                            .multilineTextAlignment(.center)
                    }
                }
                .padding(.horizontal, 40)

                Spacer()
                    .frame(height: 60)
            }
        }
        .task(id: appState.serverURL) {
            await loadAuthMethods()
        }
    }

    @ViewBuilder
    private func authControls(for methods: AuthMethods) -> some View {
        // SSO takes the top slot when available.
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
                .background(.white.opacity(0.08))
                .foregroundStyle(.white)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(.white.opacity(0.15), lineWidth: 1)
                )
            }
            .accessibilityIdentifier("login.continueWithLonghouse")

            Text("Hosted instances sign in through the Longhouse control plane.")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.45))
                .multilineTextAlignment(.center)
        }

        // Google is shown on non-SSO servers. On SSO servers it is usually absent,
        // but render it if the server explicitly advertises it.
        if methods.google && !methods.sso {
            Button(action: signInWithGoogle) {
                HStack(spacing: 10) {
                    Image(systemName: "g.circle.fill")
                        .font(.system(size: 20))
                    Text("Sign in with Google")
                        .font(.system(size: 16, weight: .medium))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(.white.opacity(0.08))
                .foregroundStyle(.white)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(.white.opacity(0.15), lineWidth: 1)
                )
            }
        }

        // Password is always shown when advertised, regardless of SSO.
        if methods.password {
            if methods.sso || methods.google {
                Divider()
                    .background(.white.opacity(0.15))
            }

            SecureField("Password", text: $password)
                .textContentType(.password)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(.white.opacity(0.08))
                .foregroundStyle(.white)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(.white.opacity(0.15), lineWidth: 1)
                )

            Button(action: signInWithPassword) {
                Text("Sign in")
                    .font(.system(size: 16, weight: .medium))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(.white.opacity(0.08))
                    .foregroundStyle(.white)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .strokeBorder(.white.opacity(0.15), lineWidth: 1)
                    )
            }
            .disabled(password.isEmpty)
            .opacity(password.isEmpty ? 0.6 : 1)
        }

        if !methods.sso && !methods.google && !methods.password {
            Text("This Longhouse server does not advertise a supported sign-in method.")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.45))
                .multilineTextAlignment(.center)
        }
    }

    @ViewBuilder
    private var hostedBootstrapControls: some View {
        Button(action: startHostedBootstrapSignIn) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.up.forward.square.fill")
                    .font(.system(size: 18))
                Text("Continue with Longhouse")
                    .font(.system(size: 16, weight: .medium))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(.white.opacity(0.08))
            .foregroundStyle(.white)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(.white.opacity(0.15), lineWidth: 1)
            )
        }
        .accessibilityIdentifier("login.continueWithLonghouse")

        Text("Hosted Longhouse accounts sign in through the control plane. Custom or self-hosted servers can still be set from the server icon.")
            .font(.caption)
            .foregroundStyle(.white.opacity(0.45))
            .multilineTextAlignment(.center)

        if let hostedAuthAttemptURL = appState.hostedAuthAttemptURL,
           UITestHooks.shouldCaptureHostedAuthAttempt {
            Text(hostedAuthAttemptURL)
                .font(.caption2)
                .foregroundStyle(.white.opacity(0.45))
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

    private func startHostedSignIn(_ methods: AuthMethods) {
        guard methods.ssoURL != nil else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        guard let tenant = tenantSubdomain(from: appState.serverURL) else {
            localErrorMessage = "Invalid Longhouse server URL"
            return
        }

        guard let authURL = HostedAuthFlow.openInstanceURL(tenant: tenant) else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        startHostedAuthSession(authURL)
    }

    private func startHostedBootstrapSignIn() {
        guard let authURL = HostedAuthFlow.openInstanceURL() else {
            localErrorMessage = "Hosted sign-in is not configured"
            return
        }

        startHostedAuthSession(authURL)
    }

    private func startHostedAuthSession(_ authURL: URL) {
        appState.clearAuthError()
        localErrorMessage = nil

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
                    if (error as NSError).code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        return
                    }
                    localErrorMessage = error.localizedDescription
                    return
                }

                guard let callbackURL else {
                    localErrorMessage = "Hosted sign-in did not return to the app"
                    return
                }

                await handleHostedAuthCallback(callbackURL)
            }
        }

        session.presentationContextProvider = authPresentationContext
        session.prefersEphemeralWebBrowserSession = false
        hostedAuthSession = session

        if !session.start() {
            hostedAuthSession = nil
            isSigningIn = false
            localErrorMessage = "Failed to start hosted sign-in"
        }
    }

    private func handleHostedAuthCallback(_ callbackURL: URL) async {
        guard let payload = HostedAuthFlow.callbackPayload(from: callbackURL) else {
            localErrorMessage = "Hosted sign-in returned an invalid callback"
            return
        }

        if let error = payload.error {
            localErrorMessage = friendlyHostedError(error)
            return
        }

        guard let ssoToken = payload.ssoToken else {
            localErrorMessage = "Hosted sign-in returned without a session token"
            return
        }

        if let instanceURL = payload.instanceURL,
           !instanceURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            await appState.prepareServerForHostedLogin(instanceURL)
        }

        let sessionEstablished = await appState.exchangeHostedSSOToken(ssoToken)
        if !sessionEstablished {
            localErrorMessage = appState.authError ?? "Hosted sign-in failed"
        }
    }

    private func signInWithGoogle() {
        guard let windowScene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
              let rootVC = windowScene.windows.first?.rootViewController else {
            localErrorMessage = "Cannot find root view controller"
            return
        }

        appState.clearAuthError()
        isSigningIn = true
        localErrorMessage = nil

        GIDSignIn.sharedInstance.signIn(withPresenting: rootVC) { result, error in
            if let error {
                Task { @MainActor in
                    isSigningIn = false
                    if (error as NSError).code == GIDSignInError.canceled.rawValue {
                        return
                    }
                    localErrorMessage = error.localizedDescription
                }
                return
            }

            guard let idToken = result?.user.idToken?.tokenString else {
                Task { @MainActor in
                    isSigningIn = false
                    localErrorMessage = "No ID token received from Google"
                }
                return
            }

            Task {
                await exchangeGoogleToken(idToken)
            }
        }
    }

    private func exchangeGoogleToken(_ idToken: String) async {
        guard let url = URL(string: "\(appState.serverURL)/api/auth/google") else {
            await MainActor.run { isSigningIn = false; localErrorMessage = "Invalid server URL" }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: ["id_token": idToken])

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                let message = Self.apiErrorMessage(from: data) ?? "Auth failed (\(statusCode))"
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
