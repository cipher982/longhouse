import Foundation

enum LonghouseAuthConfig {
    static let hostedCallbackScheme = "ai.longhouse.ios"
    static let hostedControlPlaneURL = "https://control.longhouse.ai"
    static let hostedControlPlaneHost = URL(string: hostedControlPlaneURL)?.host?.lowercased()
}
