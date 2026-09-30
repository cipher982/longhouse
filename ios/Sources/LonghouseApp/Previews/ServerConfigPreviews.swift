import SwiftUI

// The server settings for each kind of address the plaintext-http rule
// (`PlaintextHTTP`) tells apart: a LAN address offers the opt-in switch, a
// Tailscale address needs nothing, and sign-in explains a refused one.

@MainActor
private func previewAppState(serverURL: String) -> AppState {
    let appState = AppState()
    appState.serverURL = serverURL
    return appState
}

#Preview("Server settings: LAN address offers the opt-in") {
    ServerConfigSheet()
        .environmentObject(previewAppState(serverURL: "http://192.168.68.78:8080"))
        .preferredColorScheme(.dark)
}

#Preview("Server settings: Tailscale address needs none") {
    ServerConfigSheet()
        .environmentObject(previewAppState(serverURL: "http://100.111.121.118:8080"))
        .preferredColorScheme(.dark)
}

#Preview("Sign in: LAN address refused without the opt-in") {
    LoginView()
        .environmentObject(previewAppState(serverURL: "http://192.168.68.78:8080"))
}
