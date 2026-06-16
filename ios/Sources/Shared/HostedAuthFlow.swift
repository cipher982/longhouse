import Foundation
import Security

struct HostedAuthCallbackPayload: Equatable {
    let tenant: String?
    let instanceURL: String?
    let code: String?
    let runtimeToken: String?
    let tenantState: String?
    let error: String?
}

enum HostedAuthFlow {
    static func makeHandoffVerifier() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        if status != errSecSuccess {
            return "\(UUID().uuidString)-\(UUID().uuidString)"
        }
        return Data(bytes)
            .base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func openInstanceURL(tenant: String? = nil, handoffVerifier: String? = nil) -> URL? {
        guard var components = URLComponents(
            string: "\(LonghouseAuthConfig.hostedControlPlaneURL)/auth/native/open-instance"
        ) else {
            return nil
        }

        var queryItems: [URLQueryItem] = []
        let normalizedTenant = tenant?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        if let normalizedTenant, !normalizedTenant.isEmpty {
            queryItems.append(URLQueryItem(name: "tenant", value: normalizedTenant))
        }
        let verifier = handoffVerifier?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let verifier, !verifier.isEmpty {
            queryItems.append(URLQueryItem(name: "tenant_state", value: verifier))
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url
    }

    static func callbackPayload(from callbackURL: URL) -> HostedAuthCallbackPayload? {
        guard let components = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == LonghouseAuthConfig.hostedCallbackScheme.lowercased(),
              components.host?.lowercased() == "auth-callback" else {
            return nil
        }

        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first(where: { $0.name == name })?.value
        }

        return HostedAuthCallbackPayload(
            tenant: value("tenant"),
            instanceURL: value("instance_url"),
            code: value("code"),
            runtimeToken: value("runtime_token") ?? value("sso_token"),
            tenantState: value("tenant_state"),
            error: value("error")
        )
    }
}
