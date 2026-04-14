import SwiftUI

@main
struct LonghouseApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var serverURL: String
    @Published var isAuthenticated = false

    init() {
        self.serverURL = KeychainHelper.loadServerURL() ?? "https://david010.longhouse.ai"
    }

    func setServer(_ url: String) {
        serverURL = url
        KeychainHelper.saveServerURL(url)
    }
}
