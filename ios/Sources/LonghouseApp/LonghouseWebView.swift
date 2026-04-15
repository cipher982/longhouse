import SwiftUI
import WebKit

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String
    /// Called when the web app redirects to /login — native shell takes over auth.
    let onLoginRedirect: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(serverURL: serverURL, onLoginRedirect: onLoginRedirect)
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.websiteDataStore = .default()

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor

        if let url = URL(string: "\(serverURL)/timeline") {
            webView.load(URLRequest(url: url))
        }

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        guard let url = URL(string: "\(serverURL)/timeline") else {
            return
        }

        if let currentURL = webView.url, currentURL.host == url.host {
            return
        }

        webView.load(URLRequest(url: url))
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        let serverURL: String
        let onLoginRedirect: () -> Void

        init(serverURL: String, onLoginRedirect: @escaping () -> Void) {
            self.serverURL = serverURL
            self.onLoginRedirect = onLoginRedirect
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            Task { @MainActor in
                await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)
                await BrowserSessionStore.persistAccessTokenFromWebKit(for: serverURL)
            }
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

            // Intercept /login navigations — cancel the WebView load and hand off
            // to the native shell's login flow. This prevents the desktop web login
            // UI from rendering inside the app when a session expires.
            // Match /login exactly (with or without query string) to avoid
            // accidentally intercepting unrelated paths like /login-help.
            if let serverHost = URL(string: serverURL)?.host,
               url.host == serverHost,
               url.path == "/login" {
                decisionHandler(.cancel)
                onLoginRedirect()
                return
            }

            // Open external links in Safari rather than inside the WebView.
            if let serverHost = URL(string: serverURL)?.host,
               let targetHost = url.host,
               targetHost != serverHost,
               navigationAction.navigationType == .linkActivated {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }

            decisionHandler(.allow)
        }
    }
}
