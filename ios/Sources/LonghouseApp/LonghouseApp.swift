import GoogleSignIn
import SwiftUI

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
                    await appState.validateTokenIfNeeded()
                }
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var serverURL: String
    @Published var isAuthenticated = false
    @Published var sessionToken: String = ""

    init() {
        self.serverURL = KeychainHelper.loadServerURL() ?? "https://david010.longhouse.ai"

        if let stored = KeychainHelper.loadAuthToken(),
           stored.hasPrefix("longhouse_session=") {
            let token = String(stored.dropFirst("longhouse_session=".count))
            self.sessionToken = token
            self.isAuthenticated = true
        }
    }

    func validateTokenIfNeeded() async {
        guard isAuthenticated, !sessionToken.isEmpty else { return }

        guard let url = URL(string: "\(serverURL)/api/health") else {
            signOut()
            return
        }

        var request = URLRequest(url: url)
        request.addValue("longhouse_session=\(sessionToken)", forHTTPHeaderField: "Cookie")
        request.timeoutInterval = 5

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 401 {
                signOut()
            }
        } catch {
            // Network error — keep token, user might be offline
        }
    }

    func setServer(_ url: String) {
        serverURL = url
        KeychainHelper.saveServerURL(url)
    }

    func signOut() {
        GIDSignIn.sharedInstance.signOut()
        KeychainHelper.deleteAuthToken()
        sessionToken = ""
        isAuthenticated = false
    }
}
