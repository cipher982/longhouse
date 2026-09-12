import CryptoKit
import Foundation
import Security

struct HostedAuthCallbackPayload: Equatable {
    let tenant: String?
    let instanceURL: String?
    let code: String?
    let tenantState: String?
    let error: String?
}

enum HostedAuthFlow {
    static func makeHandoffVerifier() -> String {
        makeURLSafeRandom(byteCount: 32)
    }

    static func makeCodeVerifier() -> String {
        // RFC 7636 allows 43–128 URL-safe characters. Keep the verifier out
        // of the callback URL; only its S256 challenge is sent to the CP.
        makeURLSafeRandom(byteCount: 64)
    }

    static func codeChallenge(for verifier: String) -> String {
        let digest = SHA256.hash(data: Data(verifier.utf8))
        return Data(digest)
            .base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func makeURLSafeRandom(byteCount: Int) -> String {
        var bytes = [UInt8](repeating: 0, count: byteCount)
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

    static func openInstanceURL(
        tenant: String? = nil,
        handoffVerifier: String? = nil,
        codeChallenge: String? = nil
    ) -> URL? {
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
        let challenge = codeChallenge?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let challenge, !challenge.isEmpty {
            queryItems.append(URLQueryItem(name: "code_challenge", value: challenge))
            queryItems.append(URLQueryItem(name: "code_challenge_method", value: "S256"))
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url
    }

    static func validatedInstanceURL(
        _ rawValue: String,
        tenant: String?,
        expectedServerURL: String?
    ) -> String? {
        guard let components = URLComponents(string: rawValue.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme?.lowercased() == "https",
              components.user == nil,
              components.password == nil,
              components.port == nil,
              components.query == nil,
              components.fragment == nil,
              components.path.isEmpty || components.path == "/",
              let host = components.host?.lowercased(),
              let hostedControlPlaneHost = LonghouseAuthConfig.hostedControlPlaneHost else {
            return nil
        }

        let controlLabels = hostedControlPlaneHost.split(separator: ".")
        guard controlLabels.count >= 3 else { return nil }
        let rootDomain = controlLabels.dropFirst().joined(separator: ".")
        let rootSuffix = ".\(rootDomain)"
        guard host.hasSuffix(rootSuffix),
              host != hostedControlPlaneHost,
              host.dropLast(rootSuffix.count).contains(".") == false else {
            return nil
        }

        guard let tenant,
              !tenant.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        let normalizedTenant = tenant.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard host == "\(normalizedTenant)\(rootSuffix)" else {
            return nil
        }

        let canonical = "https://\(host)"
        if let expectedServerURL,
           let expected = URL(string: expectedServerURL.trimmingCharacters(in: .whitespacesAndNewlines)),
           let expectedHost = expected.host?.lowercased(),
           (expected.scheme?.lowercased() != "https" || expected.port != nil || expectedHost != host) {
            return nil
        }
        if expectedServerURL != nil,
           URL(string: expectedServerURL?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "")?.host == nil {
            return nil
        }
        return canonical
    }


    static func callbackPayload(from callbackURL: URL) -> HostedAuthCallbackPayload? {
        guard let components = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == LonghouseAuthConfig.hostedCallbackScheme.lowercased(),
              components.host?.lowercased() == "auth-callback" else {
            return nil
        }

        let knownNames = Set(["tenant", "instance_url", "code", "tenant_state", "error"])
        var values: [String: String] = [:]
        for item in components.queryItems ?? [] where knownNames.contains(item.name) {
            guard let value = item.value, values[item.name] == nil else {
                return nil
            }
            values[item.name] = value
        }

        return HostedAuthCallbackPayload(
            tenant: values["tenant"],
            instanceURL: values["instance_url"],
            code: values["code"],
            tenantState: values["tenant_state"],
            error: values["error"]
        )
    }
}
