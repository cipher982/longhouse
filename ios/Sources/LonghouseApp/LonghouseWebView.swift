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

        private var tenant: String {
            URL(string: serverURL)?.host?.components(separatedBy: ".").first ?? ""
        }

        private var controlPlaneURL: String {
            guard let host = URL(string: serverURL)?.host else { return serverURL }
            let parts = host.components(separatedBy: ".")
            guard parts.count >= 2 else { return serverURL }
            let rootDomain = parts.suffix(2).joined(separator: ".")
            return "https://control.\(rootDomain)"
        }

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

            let authURL = URL(string: "\(controlPlaneURL)/auth/native/google?tenant=\(tenant)")!
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
                    await self.handleAuthCallback(callbackURL)
                }
            }

            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            session.start()
        }

        private func handleAuthCallback(_ url: URL) async {
            guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let queryItems = components.queryItems else {
                reloadTimeline()
                return
            }

            let ssoToken = queryItems.first(where: { $0.name == "sso_token" })?.value

            guard let ssoToken else {
                reloadTimeline()
                return
            }

            do {
                let tokenData = try await exchangeSSOToken(ssoToken)
                guard let accessToken = tokenData["access_token"] as? String else {
                    reloadTimeline()
                    return
                }

                KeychainHelper.saveAuthToken("longhouse_session=\(accessToken)")

                guard let serverHost = URL(string: serverURL)?.host, let webView else {
                    reloadTimeline()
                    return
                }

                let cookieStore = webView.configuration.websiteDataStore.httpCookieStore
                if let cookie = HTTPCookie(properties: [
                    .name: "longhouse_session",
                    .value: accessToken,
                    .domain: serverHost,
                    .path: "/",
                    .secure: "TRUE",
                ]) {
                    cookieStore.setCookie(cookie) {
                        self.reloadTimeline()
                    }
                } else {
                    reloadTimeline()
                }
            } catch {
                print("SSO token exchange failed: \(error)")
                reloadTimeline()
            }
        }

        private func exchangeSSOToken(_ ssoToken: String) async throws -> [String: Any] {
            let url = URL(string: "\(serverURL)/api/auth/accept-token")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: ["token": ssoToken])

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                throw LonghouseAPIError.requestFailed
            }

            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw LonghouseAPIError.requestFailed
            }

            return json
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
