import GoogleSignIn
import GoogleSignInSwift
import SwiftUI

struct LoginView: View {
    @EnvironmentObject var appState: AppState
    @State private var isSigningIn = false
    @State private var errorMessage: String?

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
                    if isSigningIn {
                        ProgressView()
                            .tint(.white)
                            .scaleEffect(1.2)
                    } else {
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

                    if let errorMessage {
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
    }

    private func signInWithGoogle() {
        guard let windowScene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
              let rootVC = windowScene.windows.first?.rootViewController else {
            errorMessage = "Cannot find root view controller"
            return
        }

        isSigningIn = true
        errorMessage = nil

        GIDSignIn.sharedInstance.signIn(withPresenting: rootVC) { result, error in
            if let error {
                isSigningIn = false
                if (error as NSError).code == GIDSignInError.canceled.rawValue {
                    return
                }
                errorMessage = error.localizedDescription
                return
            }

            guard let idToken = result?.user.idToken?.tokenString else {
                isSigningIn = false
                errorMessage = "No ID token received from Google"
                return
            }

            Task {
                await exchangeToken(idToken)
            }
        }
    }

    private func exchangeToken(_ idToken: String) async {
        let url = URL(string: "\(appState.serverURL)/api/auth/google")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: ["id_token": idToken])

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                let body = String(data: data, encoding: .utf8) ?? ""
                await MainActor.run {
                    isSigningIn = false
                    errorMessage = "Auth failed (\(statusCode)): \(body)"
                }
                return
            }

            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let accessToken = json["access_token"] as? String else {
                await MainActor.run {
                    isSigningIn = false
                    errorMessage = "Invalid response from server"
                }
                return
            }

            await MainActor.run {
                KeychainHelper.saveAuthToken("longhouse_session=\(accessToken)")
                appState.sessionToken = accessToken
                appState.isAuthenticated = true
                isSigningIn = false
            }
        } catch {
            await MainActor.run {
                isSigningIn = false
                errorMessage = "Network error: \(error.localizedDescription)"
            }
        }
    }
}
