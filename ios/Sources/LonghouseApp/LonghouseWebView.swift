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

        let contentController = WKUserContentController()
        contentController.add(context.coordinator, name: "nativeAuth")

        let replaceGoogleButton = WKUserScript(
            source: Self.buttonReplacementJS,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        )
        contentController.addUserScript(replaceGoogleButton)
        config.userContentController = contentController

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic

        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor

        context.coordinator.webView = webView

        Self.installContentBlocker(on: config) {
            if let url = URL(string: self.serverURL + "/timeline") {
                webView.load(URLRequest(url: url))
            }
        }

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    private static func installContentBlocker(on config: WKWebViewConfiguration, then: @escaping () -> Void) {
        let rules = """
        [{
            "trigger": { "url-filter": "accounts\\\\.google\\\\.com" },
            "action": { "type": "block" }
        }]
        """
        WKContentRuleList.compile(from: rules, identifier: "block-google-auth") { ruleList, error in
            if let ruleList {
                config.userContentController.add(ruleList)
            }
            DispatchQueue.main.async { then() }
        }
    }

    private static let buttonReplacementJS = """
    (function() {
        function replaceGoogleButton() {
            var container = document.getElementById('google-signin-button');
            if (!container || container.dataset.nativeBound) return;
            container.dataset.nativeBound = 'true';
            container.innerHTML = '';
            var btn = document.createElement('button');
            btn.textContent = 'Sign in with Google';
            btn.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:12px 24px;border:1px solid rgba(255,255,255,0.2);border-radius:20px;background:rgba(255,255,255,0.06);color:#fff;font-size:14px;font-weight:500;font-family:-apple-system,system-ui,sans-serif;cursor:pointer;backdrop-filter:blur(8px);transition:background 0.15s;';
            btn.onmouseover = function() { btn.style.background = 'rgba(255,255,255,0.12)'; };
            btn.onmouseout = function() { btn.style.background = 'rgba(255,255,255,0.06)'; };
            btn.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                window.webkit.messageHandlers.nativeAuth.postMessage('google');
            };
            container.appendChild(btn);
        }

        replaceGoogleButton();

        new MutationObserver(function() { replaceGoogleButton(); })
            .observe(document.documentElement, { childList: true, subtree: true });
    })();
    """

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler, ASWebAuthenticationPresentationContextProviding {
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

        nonisolated func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "nativeAuth" else { return }
            Task { @MainActor in
                self.startNativeAuth()
            }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            extractAndStoreAuthToken(from: webView)
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

            if url.host?.contains("accounts.google.com") == true {
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

            guard let ssoToken = queryItems.first(where: { $0.name == "sso_token" })?.value else {
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
