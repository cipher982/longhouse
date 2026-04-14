import SwiftUI
import WebKit

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String
    let sessionToken: String

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

        injectCookieAndLoad(webView: webView)

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    private func injectCookieAndLoad(webView: WKWebView) {
        guard let serverHost = URL(string: serverURL)?.host else { return }

        let cookieStore = webView.configuration.websiteDataStore.httpCookieStore
        if let cookie = HTTPCookie(properties: [
            .name: "longhouse_session",
            .value: sessionToken,
            .domain: serverHost,
            .path: "/",
            .secure: "TRUE",
        ]) {
            cookieStore.setCookie(cookie) {
                if let url = URL(string: "\(self.serverURL)/timeline") {
                    webView.load(URLRequest(url: url))
                }
            }
        }
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var webView: WKWebView?
        let serverURL: String

        init(serverURL: String) {
            self.serverURL = serverURL
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                for cookie in cookies where cookie.name == "longhouse_session" {
                    KeychainHelper.saveAuthToken("longhouse_session=\(cookie.value)")
                    return
                }
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
    }
}
