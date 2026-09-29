import Foundation

enum LonghouseAuthConfig {
    static let hostedCallbackScheme = "ai.longhouse.ios"
    static let hostedControlPlaneURL = "https://control.longhouse.ai"
    /// The public demo runs open (no sign-in) on synthetic data; "Explore the demo"
    /// is the only way the app treats a server as signed in without credentials.
    static let demoServerURL = "https://longhouse.ai"
    static let hostedControlPlaneHost = URL(string: hostedControlPlaneURL)?.host?.lowercased()
}
