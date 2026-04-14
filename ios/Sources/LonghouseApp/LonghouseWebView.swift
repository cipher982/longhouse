import AuthenticationServices
import SwiftUI
import WebKit

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String

    func makeCoordinator() -> Coordinator {
        Coordinator(serverURL: serverURL)
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic

        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor

        context.coordinator.webView = webView

        if let url = URL(string: serverURL + "/timeline") {
            webView.load(URLRequest(url: url))
        }

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        let targetURL = serverURL + "/timeline"
        if let current = webView.url?.absoluteString, !current.hasPrefix(serverURL) {
            if let url = URL(string: targetURL) {
                webView.load(URLRequest(url: url))
            }
        }
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, ASWebAuthenticationPresentationContextProviding {
        weak var webView: WKWebView?
        let serverURL: String
        private var authInProgress = false

        init(serverURL: String) {
            self.serverURL = serverURL
        }

        func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
            UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }
                .flatMap(\.windows)
                .first(where: \.isKeyWindow) ?? ASPresentationAnchor()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            extractAndStoreAuthToken(from: webView)
            injectNativeAuthBridge(into: webView)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }

            if url.host?.contains("accounts.google.com") == true && url.path.contains("oauth") {
                decisionHandler(.cancel)
                if !authInProgress {
                    startNativeAuth()
                }
                return
            }

            if let host = url.host,
               let webViewHost = webView.url?.host,
               host != webViewHost,
               navigationAction.navigationType == .linkActivated {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }

            decisionHandler(.allow)
        }

        func startNativeAuth() {
            guard !authInProgress else { return }
            authInProgress = true

            let authURL = URL(string: "\(serverURL)/api/auth/google/redirect")!
            let callbackScheme = "longhouse"

            let session = ASWebAuthenticationSession(
                url: authURL,
                callbackURLScheme: callbackScheme
            ) { [weak self] callbackURL, error in
                guard let self else { return }
                self.authInProgress = false

                if let error {
                    if (error as NSError).code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        return
                    }
                    print("Auth error: \(error.localizedDescription)")
                    return
                }

                guard let callbackURL else { return }

                Task { @MainActor in
                    self.handleAuthCallback(callbackURL)
                }
            }

            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            session.start()
        }

        private func handleAuthCallback(_ url: URL) {
            guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let queryItems = components.queryItems else {
                reloadTimeline()
                return
            }

            let accessToken = queryItems.first(where: { $0.name == "at" })?.value
            let refreshToken = queryItems.first(where: { $0.name == "rt" })?.value

            guard let accessToken, let webView else {
                reloadTimeline()
                return
            }

            KeychainHelper.saveAuthToken("longhouse_session=\(accessToken)")

            guard let serverHost = URL(string: serverURL)?.host else {
                reloadTimeline()
                return
            }

            let cookieStore = webView.configuration.websiteDataStore.httpCookieStore
            var cookiesToSet: [HTTPCookie] = []

            if let sessionCookie = HTTPCookie(properties: [
                .name: "longhouse_session",
                .value: accessToken,
                .domain: serverHost,
                .path: "/",
                .secure: "TRUE",
            ]) {
                cookiesToSet.append(sessionCookie)
            }

            if let refreshToken,
               let refreshCookie = HTTPCookie(properties: [
                .name: "longhouse_refresh",
                .value: refreshToken,
                .domain: serverHost,
                .path: "/api/auth",
                .secure: "TRUE",
               ]) {
                cookiesToSet.append(refreshCookie)
            }

            setCookiesSequentially(cookieStore: cookieStore, cookies: cookiesToSet) {
                self.reloadTimeline()
            }
        }

        private func setCookiesSequentially(cookieStore: WKHTTPCookieStore, cookies: [HTTPCookie], completion: @escaping () -> Void) {
            guard let cookie = cookies.first else {
                completion()
                return
            }
            cookieStore.setCookie(cookie) {
                let remaining = Array(cookies.dropFirst())
                self.setCookiesSequentially(cookieStore: cookieStore, cookies: remaining, completion: completion)
            }
        }

        private func reloadTimeline() {
            guard let webView, let url = URL(string: "\(serverURL)/timeline") else { return }
            webView.load(URLRequest(url: url))
        }

        private func injectNativeAuthBridge(into webView: WKWebView) {
            let js = """
            (function() {
                if (window.__longhouseNativeAuthInjected) return;
                window.__longhouseNativeAuthInjected = true;

                document.addEventListener('click', function(e) {
                    var target = e.target;
                    while (target && target !== document.body) {
                        if (target.getAttribute && (
                            target.getAttribute('data-testid') === 'google-signin-button' ||
                            target.classList.contains('google-signin') ||
                            target.id === 'google-signin-button' ||
                            (target.textContent && target.textContent.includes('Sign in with Google'))
                        )) {
                            e.preventDefault();
                            e.stopPropagation();
                            window.location.href = '/api/auth/google/redirect';
                            return;
                        }
                        target = target.parentElement;
                    }
                }, true);
            })();
            """;
            webView.evaluateJavaScript(js) { _, _ in }
        }

        private func extractAndStoreAuthToken(from webView: WKWebView) {
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                for cookie in cookies where cookie.name == "longhouse_session" {
                    let tokenValue = "longhouse_session=\(cookie.value)"
                    KeychainHelper.saveAuthToken(tokenValue)
                    return
                }
            }
        }
    }
}
