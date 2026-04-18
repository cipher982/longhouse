import Foundation

enum LonghouseWebNavigationDecision: Equatable {
    case allow
    case nativeAuth
    case externalBrowser
}

enum LonghouseWebNavigation {
    static let defaultPostLoginPath = "/timeline"

    static func postLoginPath(fromReturnTo rawReturnTo: String?) -> String {
        guard let rawReturnTo else {
            return defaultPostLoginPath
        }
        return sanitizePostLoginPath(rawReturnTo)
    }

    static func decision(for requestURL: URL?, serverURL: String) -> LonghouseWebNavigationDecision {
        guard let requestURL,
              let serverHost = normalizedHost(from: URL(string: serverURL)?.host),
              let targetHost = normalizedHost(from: requestURL.host) else {
            return .allow
        }

        if targetHost == serverHost {
            return requestURL.path == "/login" ? .nativeAuth : .allow
        }

        if isHostedControlPlaneAuthURL(requestURL) {
            return .nativeAuth
        }

        return .externalBrowser
    }

    static func postLoginPath(from interceptedURL: URL?, serverURL: String) -> String {
        guard let interceptedURL else {
            return defaultPostLoginPath
        }

        let shouldPreserveReturnTo =
            isTenantLoginURL(interceptedURL, serverURL: serverURL) ||
            isHostedOpenInstanceURL(interceptedURL)

        guard shouldPreserveReturnTo,
              let components = URLComponents(url: interceptedURL, resolvingAgainstBaseURL: false),
              let rawReturnTo = components.queryItems?.first(where: { $0.name == "return_to" })?.value else {
            return defaultPostLoginPath
        }

        return postLoginPath(fromReturnTo: rawReturnTo)
    }

    private static func sanitizePostLoginPath(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed.hasPrefix("/"),
              !trimmed.hasPrefix("//"),
              !trimmed.hasPrefix("/\\"),
              let url = URL(string: trimmed, relativeTo: URL(string: "https://longhouse.invalid")) else {
            return defaultPostLoginPath
        }

        let path = url.path.isEmpty ? "/" : url.path
        if path == "/login" || path == "/auth" || path.hasPrefix("/auth/") {
            return defaultPostLoginPath
        }

        var sanitized = path
        if let query = url.query, !query.isEmpty {
            sanitized += "?\(query)"
        }
        if let fragment = url.fragment, !fragment.isEmpty {
            sanitized += "#\(fragment)"
        }
        return sanitized
    }

    private static func isTenantLoginURL(_ url: URL, serverURL: String) -> Bool {
        guard url.path == "/login",
              let serverHost = normalizedHost(from: URL(string: serverURL)?.host),
              let targetHost = normalizedHost(from: url.host) else {
            return false
        }
        return targetHost == serverHost
    }

    private static func isHostedOpenInstanceURL(_ url: URL) -> Bool {
        guard let controlPlaneHost = LonghouseAuthConfig.hostedControlPlaneHost,
              normalizedHost(from: url.host) == controlPlaneHost else {
            return false
        }
        return url.path == "/dashboard/open-instance"
    }

    private static func isHostedControlPlaneAuthURL(_ url: URL) -> Bool {
        guard let controlPlaneHost = LonghouseAuthConfig.hostedControlPlaneHost,
              normalizedHost(from: url.host) == controlPlaneHost else {
            return false
        }

        return url.path == "/" ||
            url.path == "/signup" ||
            url.path == "/auth" ||
            url.path.hasPrefix("/auth/") ||
            url.path == "/dashboard/open-instance"
    }

    private static func normalizedHost(from rawHost: String?) -> String? {
        rawHost?.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
    }
}
