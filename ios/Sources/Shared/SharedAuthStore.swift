import Foundation

struct SharedAuthDebugState: Sendable {
    let appGroupAvailable: Bool
    let containerPath: String?
    let serverURL: String?
    let host: String?
    let cookieNames: [String]

    var cookieCount: Int {
        cookieNames.count
    }
}

enum SharedAuthStore {
    static let appGroupIdentifier = "group.ai.longhouse.shared"
    static let sessionCookieName = "longhouse_session"
    static let refreshCookieName = "longhouse_refresh"
    static let managedCookieNames: Set<String> = [sessionCookieName, refreshCookieName]

    private static let serverURLKey = "longhouse_server_url"
    private static let cookieStoragePrefix = "managed_cookies."

    private static var defaults: UserDefaults? {
        UserDefaults(suiteName: appGroupIdentifier)
    }

    private static var containerURL: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupIdentifier)
    }

    static var isAppGroupAvailable: Bool {
        defaults != nil && containerURL != nil
    }

    static func saveServerURL(_ url: String) {
        let value = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            clearServerURL()
            return
        }
        defaults?.set(value, forKey: serverURLKey)
    }

    static func loadServerURL() -> String? {
        let value = defaults?.string(forKey: serverURLKey)?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value, !value.isEmpty else {
            return nil
        }
        return value
    }

    static func clearServerURL() {
        defaults?.removeObject(forKey: serverURLKey)
    }

    static func managedCookies(for serverURL: String) -> [HTTPCookie] {
        guard let host = normalizedHost(for: serverURL) else {
            return []
        }

        let rawCookies = defaults?.array(forKey: cookieStorageKeyForHost(host)) as? [[String: Any]] ?? []
        return rawCookies.compactMap { dictionary in
            let properties = Dictionary(
                uniqueKeysWithValues: dictionary.map { (HTTPCookiePropertyKey($0.key), $0.value) }
            )
            return HTTPCookie(properties: properties)
        }.filter { cookie in
            managedCookieNames.contains(cookie.name)
                && domainMatches(cookie.domain, host: host)
                && !isExpired(cookie)
        }
    }

    static func hasManagedCookies(for serverURL: String) -> Bool {
        !managedCookies(for: serverURL).isEmpty
    }

    static func setManagedCookies(_ cookies: [HTTPCookie], for serverURL: String) {
        let validCookies = cookies.filter { cookie in
            managedCookieNames.contains(cookie.name) && domainMatches(cookie.domain, host: normalizedHost(for: serverURL))
        }
        let encoded = validCookies.compactMap(cookieDictionary(from:))
        defaults?.set(encoded, forKey: cookieStorageKey(for: serverURL))
    }

    static func clearManagedCookies(for serverURL: String) {
        defaults?.removeObject(forKey: cookieStorageKey(for: serverURL))
    }

    static func cookieHeader(for serverURL: String) -> String? {
        let cookies = managedCookies(for: serverURL)
        guard !cookies.isEmpty else {
            return nil
        }
        return cookies
            .sorted { $0.name < $1.name }
            .map { "\($0.name)=\($0.value)" }
            .joined(separator: "; ")
    }

    private static func normalizedHost(for serverURL: String) -> String? {
        URL(string: serverURL)?.host?.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
    }

    static func debugState(for serverURL: String?) -> SharedAuthDebugState {
        let resolvedServerURL = serverURL ?? loadServerURL()
        let cookies = resolvedServerURL.map(managedCookies(for:)) ?? []

        return SharedAuthDebugState(
            appGroupAvailable: isAppGroupAvailable,
            containerPath: containerURL?.path,
            serverURL: resolvedServerURL,
            host: resolvedServerURL.flatMap(normalizedHost(for:)),
            cookieNames: cookies.map(\.name).sorted()
        )
    }

    private static func cookieStorageKey(for serverURL: String) -> String {
        cookieStoragePrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func cookieStorageKeyForHost(_ host: String) -> String {
        cookieStoragePrefix + host
    }

    private static func cookieDictionary(from cookie: HTTPCookie) -> [String: Any]? {
        guard let properties = cookie.properties else {
            return nil
        }

        var dictionary: [String: Any] = [:]
        for (key, value) in properties {
            dictionary[key.rawValue] = value
        }
        return dictionary
    }

    private static func isExpired(_ cookie: HTTPCookie) -> Bool {
        guard let expiresDate = cookie.expiresDate else {
            return false
        }
        return expiresDate <= Date()
    }

    private static func domainMatches(_ rawDomain: String, host: String?) -> Bool {
        guard let host, !host.isEmpty else {
            return false
        }

        let domain = rawDomain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        guard !domain.isEmpty else {
            return false
        }
        return host == domain || host.hasSuffix(".\(domain)")
    }
}
