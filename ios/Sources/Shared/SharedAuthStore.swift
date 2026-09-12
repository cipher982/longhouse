import Foundation
import Security

struct SharedAuthDebugState: Sendable {
    let appGroupAvailable: Bool
    let containerPath: String?
    let serverURL: String?
    let host: String?
    let cookieNames: [String]
    let hasRuntimeToken: Bool
    let hasNativeRefreshToken: Bool

    var cookieCount: Int {
        cookieNames.count
    }

    var hasCredentials: Bool {
        cookieCount > 0 || hasRuntimeToken || hasNativeRefreshToken
    }
}

enum SharedAuthStore {
    private struct HostedTokenBundle: Codable {
        let runtimeToken: String
        let runtimeExpiresAt: Date?
        let refreshToken: String
        let refreshExpiresAt: Date?
        let generation: String
    }

    static let appGroupIdentifier = "group.ai.longhouse.shared"
    static let sessionCookieName = "__Host-lh_session"
    static let refreshCookieName = "__Host-lh_refresh"
    static let legacySessionCookieName = "longhouse_session"
    static let legacyRefreshCookieName = "longhouse_refresh"
    // Includes both generations so callers can accept a server response while
    // the URL-aware persistence boundary chooses the correct active pair.
    static let managedCookieNames: Set<String> = [
        sessionCookieName,
        refreshCookieName,
        legacySessionCookieName,
        legacyRefreshCookieName,
    ]

    static func cookieNames(for serverURL: String) -> Set<String> {
        let secure = URL(string: serverURL)?.scheme?.lowercased() == "https"
        return secure
            ? [sessionCookieName, refreshCookieName]
            : [legacySessionCookieName, legacyRefreshCookieName]
    }

    static func activeSessionCookieName(for serverURL: String) -> String {
        cookieNames(for: serverURL).contains(sessionCookieName)
            ? sessionCookieName
            : legacySessionCookieName
    }

    static func activeRefreshCookieName(for serverURL: String) -> String {
        cookieNames(for: serverURL).contains(refreshCookieName)
            ? refreshCookieName
            : legacyRefreshCookieName
    }

    private static let serverURLKey = "longhouse_server_url"
    private static let cookieStoragePrefix = "managed_cookies."
    private static let runtimeTokenStoragePrefix = "runtime_tokens."
    private static let runtimeTokenExpiryPrefix = "runtime_token_expires_at."
    private static let nativeRefreshTokenStoragePrefix = "native_refresh_tokens."
    private static let nativeRefreshTokenExpiryPrefix = "native_refresh_token_expires_at."
    private static let hostedTokenBundlePrefix = "hosted_token_bundles."
    private static let pendingNativeRevocationPrefix = "pending_native_revocations."
    private static let authGenerationPrefix = "auth_generations."
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

    /// Generation-fences asynchronous auth work. A response captured before
    /// logout or server switching may finish later, but it cannot become the
    /// active credential pair after the generation changes.
    static func authGeneration(for serverURL: String) -> String {
        defaults?.string(forKey: authGenerationKey(for: serverURL)) ?? "initial"
    }

    @discardableResult
    static func advanceAuthGeneration(for serverURL: String) -> String {
        let generation = UUID().uuidString
        defaults?.set(generation, forKey: authGenerationKey(for: serverURL))
        return generation
    }

    static func isAuthGenerationCurrent(_ generation: String, for serverURL: String) -> Bool {
        authGeneration(for: serverURL) == generation
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
            cookieNames(for: serverURL).contains(cookie.name)
                && domainMatches(cookie.domain, host: host)
                && !isExpired(cookie)
        }
    }

    static func hasManagedCookies(for serverURL: String) -> Bool {
        !managedCookies(for: serverURL).isEmpty
    }

    static func saveRuntimeToken(_ token: String, expiresAt: Date? = nil, for serverURL: String) {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            clearRuntimeToken(for: serverURL)
            return
        }
        guard let data = value.data(using: .utf8) else {
            return
        }
        saveKeychainData(data, account: runtimeTokenStorageKey(for: serverURL))
        saveRuntimeTokenExpiry(expiresAt, for: serverURL)
    }

    @discardableResult
    static func saveHostedTokens(
        runtimeToken: String,
        runtimeExpiresAt: Date?,
        refreshToken: String,
        refreshExpiresAt: Date?,
        for serverURL: String,
        expectedGeneration: String? = nil
    ) -> Bool {
        let token = runtimeToken.trimmingCharacters(in: .whitespacesAndNewlines)
        let refresh = refreshToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, !refresh.isEmpty else {
            return false
        }
        let generation = expectedGeneration ?? authGeneration(for: serverURL)
        guard isAuthGenerationCurrent(generation, for: serverURL) else {
            return false
        }
        let bundle = HostedTokenBundle(
            runtimeToken: token,
            runtimeExpiresAt: runtimeExpiresAt,
            refreshToken: refresh,
            refreshExpiresAt: refreshExpiresAt,
            generation: generation
        )
        guard let data = try? JSONEncoder().encode(bundle) else {
            return false
        }
        return saveKeychainData(data, account: hostedTokenBundleStorageKey(for: serverURL))
    }

    private static func hostedTokenBundle(for serverURL: String) -> HostedTokenBundle? {
        guard let data = loadKeychainData(account: hostedTokenBundleStorageKey(for: serverURL)) else {
            return nil
        }
        return try? JSONDecoder().decode(HostedTokenBundle.self, from: data)
    }

    static func runtimeToken(for serverURL: String) -> String? {
        if let bundle = hostedTokenBundle(for: serverURL) {
            guard bundle.generation == authGeneration(for: serverURL) else { return nil }
            return bundle.runtimeToken
        }
        guard let data = loadKeychainData(account: runtimeTokenStorageKey(for: serverURL)),
              let token = String(data: data, encoding: .utf8)?
                  .trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else {
            return nil
        }
        return token
    }

    static func runtimeTokenExpiresAt(for serverURL: String) -> Date? {
        if let bundle = hostedTokenBundle(for: serverURL) {
            guard bundle.generation == authGeneration(for: serverURL) else { return nil }
            return bundle.runtimeExpiresAt
        }
        let key = runtimeTokenExpiryKey(for: serverURL)
        let ts = defaults?.double(forKey: key) ?? 0
        guard ts > 0 else { return nil }
        return Date(timeIntervalSince1970: ts)
    }

    static func saveRuntimeTokenExpiry(_ expiresAt: Date?, for serverURL: String) {
        let key = runtimeTokenExpiryKey(for: serverURL)
        if let expiresAt {
            defaults?.set(expiresAt.timeIntervalSince1970, forKey: key)
        } else {
            defaults?.removeObject(forKey: key)
        }
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
        deleteKeychainData(account: hostedTokenBundleStorageKey(for: serverURL))
        deleteKeychainData(account: runtimeTokenStorageKey(for: serverURL))
        saveRuntimeTokenExpiry(nil, for: serverURL)
    }

    static func saveNativeRefreshToken(_ token: String, expiresAt: Date? = nil, for serverURL: String) {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            clearNativeRefreshToken(for: serverURL)
            return
        }
        guard let data = value.data(using: .utf8) else {
            return
        }
        saveKeychainData(data, account: nativeRefreshTokenStorageKey(for: serverURL))
        saveNativeRefreshTokenExpiry(expiresAt, for: serverURL)
    }

    static func nativeRefreshToken(for serverURL: String) -> String? {
        if let bundle = hostedTokenBundle(for: serverURL) {
            guard bundle.generation == authGeneration(for: serverURL) else { return nil }
            return bundle.refreshToken
        }
        guard let data = loadKeychainData(account: nativeRefreshTokenStorageKey(for: serverURL)),
              let token = String(data: data, encoding: .utf8)?
                  .trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else {
            return nil
        }
        return token
    }

    static func hasNativeRefreshToken(for serverURL: String) -> Bool {
        nativeRefreshToken(for: serverURL) != nil
    }

    static func nativeRefreshTokenExpiresAt(for serverURL: String) -> Date? {
        if let bundle = hostedTokenBundle(for: serverURL) {
            guard bundle.generation == authGeneration(for: serverURL) else { return nil }
            return bundle.refreshExpiresAt
        }
        let key = nativeRefreshTokenExpiryKey(for: serverURL)
        let ts = defaults?.double(forKey: key) ?? 0
        guard ts > 0 else { return nil }
        return Date(timeIntervalSince1970: ts)
    }

    static func saveNativeRefreshTokenExpiry(_ expiresAt: Date?, for serverURL: String) {
        let key = nativeRefreshTokenExpiryKey(for: serverURL)
        if let expiresAt {
            defaults?.set(expiresAt.timeIntervalSince1970, forKey: key)
        } else {
            defaults?.removeObject(forKey: key)
        }
    }

    static func clearNativeRefreshToken(for serverURL: String) {
        deleteKeychainData(account: hostedTokenBundleStorageKey(for: serverURL))
        deleteKeychainData(account: nativeRefreshTokenStorageKey(for: serverURL))
        saveNativeRefreshTokenExpiry(nil, for: serverURL)
    }

    static func savePendingNativeRevocationToken(_ token: String, for serverURL: String) {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        var tokens = pendingNativeRevocationTokens(for: serverURL)
        if !tokens.contains(value) {
            tokens.append(value)
        }
        guard let data = try? JSONSerialization.data(withJSONObject: tokens) else { return }
        _ = saveKeychainData(data, account: pendingNativeRevocationStorageKey(for: serverURL))
    }

    static func pendingNativeRevocationTokens(for serverURL: String) -> [String] {
        guard let data = loadKeychainData(account: pendingNativeRevocationStorageKey(for: serverURL)) else {
            return []
        }
        if let tokens = try? JSONSerialization.jsonObject(with: data) as? [String] {
            return tokens.filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
        // Read the pre-set format so an upgrade never drops an unresolved
        // revocation obligation.
        guard let legacy = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !legacy.isEmpty else {
            return []
        }
        return [legacy]
    }

    static func pendingNativeRevocationToken(for serverURL: String) -> String? {
        pendingNativeRevocationTokens(for: serverURL).first
    }

    static func clearPendingNativeRevocationToken(_ token: String, for serverURL: String) {
        let remaining = pendingNativeRevocationTokens(for: serverURL).filter { $0 != token }
        if remaining.isEmpty {
            clearPendingNativeRevocationToken(for: serverURL)
            return
        }
        guard let data = try? JSONSerialization.data(withJSONObject: remaining) else { return }
        _ = saveKeychainData(data, account: pendingNativeRevocationStorageKey(for: serverURL))
    }

    static func clearPendingNativeRevocationToken(for serverURL: String) {
        deleteKeychainData(account: pendingNativeRevocationStorageKey(for: serverURL))
    }

    static func setManagedCookies(_ cookies: [HTTPCookie], for serverURL: String) {
        let validCookies = cookies.filter { cookie in
            cookieNames(for: serverURL).contains(cookie.name)
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
        removeSharedCookieStorage(for: serverURL)
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
        let activeNames = cookieNames(for: serverURL)
        let cookies = (HTTPCookieStorage.shared.cookies ?? []).filter {
            activeNames.contains($0.name) && domainMatches($0.domain, host: host)
        }
        setManagedCookies(cookies, for: serverURL)
    }

    /// Remove both current and legacy auth cookies from
    /// `HTTPCookieStorage.shared` on sign-out or server switch. Keychain
    /// cookies are cleared separately via `clearManagedCookies(for:)`.
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
            cookieNames: cookies.map(\.name).sorted(),
            hasRuntimeToken: resolvedServerURL.map(hasRuntimeToken(for:)) ?? false,
            hasNativeRefreshToken: resolvedServerURL.map(hasNativeRefreshToken(for:)) ?? false
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

    private static func runtimeTokenExpiryKey(for serverURL: String) -> String {
        runtimeTokenExpiryPrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func nativeRefreshTokenStorageKey(for serverURL: String) -> String {
        nativeRefreshTokenStoragePrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func nativeRefreshTokenExpiryKey(for serverURL: String) -> String {
        nativeRefreshTokenExpiryPrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func hostedTokenBundleStorageKey(for serverURL: String) -> String {
        hostedTokenBundlePrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func pendingNativeRevocationStorageKey(for serverURL: String) -> String {
        pendingNativeRevocationPrefix + (normalizedHost(for: serverURL) ?? serverURL)
    }

    private static func authGenerationKey(for serverURL: String) -> String {
        authGenerationPrefix + (normalizedHost(for: serverURL) ?? serverURL)
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

    /// Always `ThisDeviceOnly`: everything stored here is a credential, and a
    /// non-`ThisDeviceOnly` item rides an encrypted backup and restores onto a
    /// different device.
    @discardableResult
    private static func saveKeychainData(_ data: Data, account: String) -> Bool {
        let query = keychainQuery(account: account)
        var attributes: [String: Any] = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            return true
        }
        guard updateStatus == errSecItemNotFound else {
            return false
        }
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        var addQuery = query
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(addQuery as CFDictionary, nil) == errSecSuccess
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
