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

        let blockGIS = WKUserScript(
            source: Self.earlyInjectionJS,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        )
        contentController.addUserScript(blockGIS)
        config.userContentController = contentController

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

    private static let earlyInjectionJS = """
    (function() {
        // Block GIS script from loading — it doesn't work in WKWebView
        var origCreateElement = document.createElement.bind(document);
        document.createElement = function(tag) {
            var el = origCreateElement(tag);
            if (tag.toLowerCase() === 'script') {
                var origSetAttribute = el.setAttribute.bind(el);
                var origSrcDescriptor = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, 'src');
                Object.defineProperty(el, 'src', {
                    set: function(val) {
                        if (val && val.indexOf('accounts.google.com/gsi/client') !== -1) {
                            console.log('[Longhouse] Blocked GIS script, using native auth');
                            return;
                        }
                        origSrcDescriptor.set.call(el, val);
                    },
                    get: function() { return origSrcDescriptor.get.call(el); },
                    configurable: true
                });
            }
            return el;
        };

        // Intercept google.accounts.id.renderButton to inject our native button
        var _googleProxy = undefined;
        Object.defineProperty(window, 'google', {
            get: function() { return _googleProxy; },
            set: function(val) {
                _googleProxy = val;
                if (val && val.accounts && val.accounts.id) {
                    val.accounts.id.renderButton = function(container, options) {
                        if (!container) return;
                        container.innerHTML = '';
                        var btn = document.createElement('button');
                        btn.textContent = 'Sign in with Google';
                        btn.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:10px 16px;border:1px solid #444;border-radius:8px;background:#1a1a1a;color:#fff;font-size:14px;font-family:-apple-system,system-ui,sans-serif;cursor:pointer;';
                        btn.onmouseover = function() { btn.style.background = '#2a2a2a'; };
                        btn.onmouseout = function() { btn.style.background = '#1a1a1a'; };
                        btn.onclick = function(e) {
                            e.preventDefault();
                            e.stopPropagation();
                            window.webkit.messageHandlers.nativeAuth.postMessage('google');
                        };
                        container.appendChild(btn);
                    };
                    val.accounts.id.initialize = function() {};
                    val.accounts.id.prompt = function() {};
                }
            },
            configurable: true
        });

        // Fallback: watch for the button container and replace if GIS somehow renders
        var observer = new MutationObserver(function(mutations) {
            var container = document.getElementById('google-signin-button');
            if (container && !container.dataset.nativeBound) {
                container.dataset.nativeBound = 'true';
                // If GIS managed to render, replace its contents
                setTimeout(function() {
                    var iframes = container.querySelectorAll('iframe');
                    if (iframes.length > 0 || container.querySelector('[role="button"]')) {
                        container.innerHTML = '';
                        var btn = document.createElement('button');
                        btn.textContent = 'Sign in with Google';
                        btn.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:10px 16px;border:1px solid #444;border-radius:8px;background:#1a1a1a;color:#fff;font-size:14px;font-family:-apple-system,system-ui,sans-serif;cursor:pointer;';
                        btn.onclick = function(e) {
                            e.preventDefault();
                            window.webkit.messageHandlers.nativeAuth.postMessage('google');
                        };
                        container.appendChild(btn);
                    }
                }, 500);
            }
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
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

            // Catch any direct navigations to Google OAuth as a safety net
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
