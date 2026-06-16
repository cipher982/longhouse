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
    private static let runtimeTokenStoragePrefix = "runtime_tokens."
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

    static func saveRuntimeToken(_ token: String, for serverURL: String) {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            clearRuntimeToken(for: serverURL)
            return
        }
        guard let data = value.data(using: .utf8) else {
            return
        }
        saveKeychainData(data, account: runtimeTokenStorageKey(for: serverURL))
        KeychainHelper.saveAuthToken("Bearer \(value)")
    }

    static func runtimeToken(for serverURL: String) -> String? {
        guard let data = loadKeychainData(account: runtimeTokenStorageKey(for: serverURL)),
              let token = String(data: data, encoding: .utf8)?
                  .trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else {
            return nil
        }
        return token
    }

    static func hasRuntimeToken(for serverURL: String) -> Bool {
        runtimeToken(for: serverURL) != nil
    }

    static func authorizationHeader(for serverURL: String) -> String? {
        guard let token = runtimeToken(for: serverURL) else {
            return nil
        }
        return "Bearer \(token)"
    }

    static func clearRuntimeToken(for serverURL: String) {
        deleteKeychainData(account: runtimeTokenStorageKey(for: serverURL))
        KeychainHelper.deleteAuthToken()
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

    /// Load keychain-persisted cookies into `HTTPCookieStorage.shared` so
    /// `URLSession.shared` auto-attaches them to every request. Call on
    /// launch and after any auth flow.
    static func primeSharedCookieStorage(for serverURL: String) {
        let cookies = managedCookies(for: serverURL)
        for cookie in cookies {
            HTTPCookieStorage.shared.setCookie(cookie)
        }
    }

    /// Snapshot cookies currently in `HTTPCookieStorage.shared` back into the
    /// keychain so the widget extension can see them. Call after any request
    /// that mutates auth cookies (`/api/auth/refresh`, `/api/auth/google`, etc.).
    static func captureCookiesFromSharedStorage(for serverURL: String) {
        guard let host = normalizedHost(for: serverURL) else { return }
        let cookies = (HTTPCookieStorage.shared.cookies ?? []).filter {
            managedCookieNames.contains($0.name) && domainMatches($0.domain, host: host)
        }
        setManagedCookies(cookies, for: serverURL)
        if let session = cookies.first(where: { $0.name == sessionCookieName }) {
            KeychainHelper.saveAuthToken("\(session.name)=\(session.value)")
        } else {
            KeychainHelper.deleteAuthToken()
        }
    }

    /// Remove auth cookies from `HTTPCookieStorage.shared` on sign-out or
    /// server switch. Keychain cookies are cleared separately via
    /// `clearManagedCookies(for:)`.
    static func removeSharedCookieStorage(for serverURL: String) {
        guard let host = normalizedHost(for: serverURL) else { return }
        for cookie in HTTPCookieStorage.shared.cookies ?? [] {
            if managedCookieNames.contains(cookie.name) && domainMatches(cookie.domain, host: host) {
                HTTPCookieStorage.shared.deleteCookie(cookie)
            }
        }
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

    private static func runtimeTokenStorageKey(for serverURL: String) -> String {
        runtimeTokenStoragePrefix + (normalizedHost(for: serverURL) ?? serverURL)
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
