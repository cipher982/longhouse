import Foundation

struct HostedAuthCallbackPayload: Equatable {
    let tenant: String?
    let instanceURL: String?
    let runtimeToken: String?
    let tenantState: String?
    let error: String?
}

enum HostedAuthFlow {
    static func openInstanceURL(tenant: String? = nil) -> URL? {
        guard var components = URLComponents(
            string: "\(LonghouseAuthConfig.hostedControlPlaneURL)/auth/native/open-instance"
        ) else {
            return nil
        }

        let normalizedTenant = tenant?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        if let normalizedTenant, !normalizedTenant.isEmpty {
            components.queryItems = [URLQueryItem(name: "tenant", value: normalizedTenant)]
        }
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
            runtimeToken: value("runtime_token") ?? value("sso_token"),
            tenantState: value("tenant_state"),
            error: value("error")
        )
    }
}
