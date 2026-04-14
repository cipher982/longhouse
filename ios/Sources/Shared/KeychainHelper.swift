import Foundation
import Security

enum KeychainHelper {
    private static let service = "ai.longhouse.ios"
    private static let authTokenKey = "longhouse_auth_token"
    private static let serverURLKey = "longhouse_server_url"

    static func saveAuthToken(_ token: String) {
        save(key: authTokenKey, value: token)
    }

    static func loadAuthToken() -> String? {
        load(key: authTokenKey)
    }

    static func deleteAuthToken() {
        delete(key: authTokenKey)
    }

    static func saveServerURL(_ url: String) {
        save(key: serverURLKey, value: url)
        SharedAuthStore.saveServerURL(url)
    }

    static func deleteServerURL() {
        delete(key: serverURLKey)
        SharedAuthStore.clearServerURL()
    }

    static func loadServerURL() -> String? {
        if let url = load(key: serverURLKey) {
            SharedAuthStore.saveServerURL(url)
            return url
        }
        return SharedAuthStore.loadServerURL()
    }

    private static func save(key: String, value: String) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
        var addQuery = query
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(addQuery as CFDictionary, nil)
    }

    private static func load(key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func delete(key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
