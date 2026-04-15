import SwiftUI
import WebKit

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String
    /// Path to load on initial mount (e.g. /timeline or /timeline/abc-123).
    let initialPath: String
    /// Called when the shell needs to take over auth instead of rendering a web login page.
    let onAuthRedirect: (URL) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(serverURL: serverURL, onAuthRedirect: onAuthRedirect)
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

        if let url = URL(string: "\(serverURL)\(initialPath)") {
            webView.load(URLRequest(url: url))
        }

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        guard let url = URL(string: "\(serverURL)\(initialPath)") else {
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
        let onAuthRedirect: (URL) -> Void

        init(serverURL: String, onAuthRedirect: @escaping (URL) -> Void) {
            self.serverURL = serverURL
            self.onAuthRedirect = onAuthRedirect
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

            if navigationAction.targetFrame?.isMainFrame == false {
                decisionHandler(.allow)
                return
            }

            switch LonghouseWebNavigation.decision(for: url, serverURL: serverURL) {
            case .allow:
                decisionHandler(.allow)
            case .nativeAuth:
                decisionHandler(.cancel)
                onAuthRedirect(url)
            case .externalBrowser:
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            }
        }
    }
}
