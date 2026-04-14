import Foundation
import Security

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
    private static let keychainService = "ai.longhouse.shared-cookies"

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

        guard let data = loadKeychainData(account: cookieStorageKeyForHost(host)),
              let rawCookies = try? PropertyListSerialization.propertyList(
                  from: data, format: nil
              ) as? [[String: Any]] else {
            return []
        }

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
            managedCookieNames.contains(cookie.name)
                && domainMatches(cookie.domain, host: normalizedHost(for: serverURL))
                && !isExpired(cookie)
        }
        let encoded = validCookies.compactMap(cookieDictionary(from:))
        guard let data = try? PropertyListSerialization.data(
            fromPropertyList: encoded, format: .binary, options: 0
        ) else {
            return
        }
        saveKeychainData(data, account: cookieStorageKey(for: serverURL))
    }

    static func clearManagedCookies(for serverURL: String) {
        deleteKeychainData(account: cookieStorageKey(for: serverURL))
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

    // MARK: - Keychain storage (shared via app group entitlement)

    private static func keychainQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: appGroupIdentifier,
        ]
    }

    private static func saveKeychainData(_ data: Data, account: String) {
        let query = keychainQuery(account: account)
        SecItemDelete(query as CFDictionary)
        var addQuery = query
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(addQuery as CFDictionary, nil)
    }

    private static func loadKeychainData(account: String) -> Data? {
        var query = keychainQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else { return nil }
        return result as? Data
    }

    private static func deleteKeychainData(account: String) {
        SecItemDelete(keychainQuery(account: account) as CFDictionary)
    }
}
