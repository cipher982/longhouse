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
