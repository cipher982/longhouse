import Foundation
import WebKit

struct BrowserSession {
    let sessionCookie: HTTPCookie?
    let refreshCookie: HTTPCookie?

    var hasCookies: Bool {
        sessionCookie != nil || refreshCookie != nil
    }
}

@MainActor
enum BrowserSessionStore {
    // The iOS shell wraps the web app, so the browser cookie jar is the
    // durable auth boundary. Native login only bootstraps that session.
    private static let sessionCookieName = SharedAuthStore.sessionCookieName
    private static let refreshCookieName = SharedAuthStore.refreshCookieName
    private static let managedCookieNames = SharedAuthStore.managedCookieNames

    static func webKitSession(for serverURL: String) async -> BrowserSession {
        let cookies = await webKitCookies(for: serverURL)
        return BrowserSession(
            sessionCookie: cookies.first(where: { $0.name == sessionCookieName }),
            refreshCookie: cookies.first(where: { $0.name == refreshCookieName })
        )
    }

    static func syncSharedCookiesToWebKit(for serverURL: String) async {
        await setWebKitCookies(mergedSharedCookies(for: serverURL))
    }

    static func syncWebKitCookiesToShared(for serverURL: String) async {
        let sharedStore = HTTPCookieStorage.shared
        let cookies = await webKitCookies(for: serverURL)
        for cookie in cookies {
            sharedStore.setCookie(cookie)
        }
        SharedAuthStore.setManagedCookies(cookies, for: serverURL)
    }

    static func persistAccessTokenFromWebKit(for serverURL: String) async {
        await syncWebKitCookiesToShared(for: serverURL)
        let session = await webKitSession(for: serverURL)
        if let sessionCookie = session.sessionCookie {
            KeychainHelper.saveAuthToken("\(sessionCookie.name)=\(sessionCookie.value)")
        } else {
            KeychainHelper.deleteAuthToken()
        }
    }

    static func clearAll(for serverURL: String) async {
        let webKitManagedCookies = await webKitCookies(for: serverURL)
        await deleteWebKitCookies(webKitManagedCookies)

        let sharedStore = HTTPCookieStorage.shared
        for cookie in sharedCookies(for: serverURL) {
            sharedStore.deleteCookie(cookie)
        }
        SharedAuthStore.clearManagedCookies(for: serverURL)
    }

    private static func mergedSharedCookies(for serverURL: String) -> [HTTPCookie] {
        var cookiesByID: [String: HTTPCookie] = [:]
        for cookie in sharedCookies(for: serverURL) + SharedAuthStore.managedCookies(for: serverURL) {
            cookiesByID[cookieIdentity(cookie)] = cookie
        }
        return Array(cookiesByID.values)
    }

    private static func sharedCookies(for serverURL: String) -> [HTTPCookie] {
        guard let host = normalizedHost(for: serverURL) else {
            return []
        }

        return (HTTPCookieStorage.shared.cookies ?? []).filter { cookie in
            managedCookieNames.contains(cookie.name) && domainMatches(cookie.domain, host: host)
        }
    }

    private static func webKitCookies(for serverURL: String) async -> [HTTPCookie] {
        guard let host = normalizedHost(for: serverURL) else {
            return []
        }

        let cookies = await withCheckedContinuation { continuation in
            WKWebsiteDataStore.default().httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }

        return cookies.filter { cookie in
            managedCookieNames.contains(cookie.name) && domainMatches(cookie.domain, host: host)
        }
    }

    private static func setWebKitCookies(_ cookies: [HTTPCookie]) async {
        let cookieStore = WKWebsiteDataStore.default().httpCookieStore
        for cookie in cookies {
            await withCheckedContinuation { continuation in
                cookieStore.setCookie(cookie) {
                    continuation.resume()
                }
            }
        }
    }

    private static func deleteWebKitCookies(_ cookies: [HTTPCookie]) async {
        let cookieStore = WKWebsiteDataStore.default().httpCookieStore
        for cookie in cookies {
            await withCheckedContinuation { continuation in
                cookieStore.delete(cookie) {
                    continuation.resume()
                }
            }
        }
    }

    private static func normalizedHost(for serverURL: String) -> String? {
        URL(string: serverURL)?.host?.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
    }

    private static func cookieIdentity(_ cookie: HTTPCookie) -> String {
        "\(cookie.name)|\(cookie.domain)|\(cookie.path)"
    }

    private static func domainMatches(_ rawDomain: String, host: String) -> Bool {
        let domain = rawDomain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        guard !domain.isEmpty else {
            return false
        }
        return host == domain || host.hasSuffix(".\(domain)")
    }
}
