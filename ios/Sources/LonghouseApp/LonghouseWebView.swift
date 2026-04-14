import SwiftUI
import WebKit

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic

        // Match the app background while loading
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
    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var webView: WKWebView?

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            extractAndStoreAuthToken(from: webView)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            if let url = navigationAction.request.url {
                // Open external links (OAuth, docs) in Safari
                if let host = url.host,
                   let webViewHost = webView.url?.host,
                   host != webViewHost,
                   navigationAction.navigationType == .linkActivated {
                    UIApplication.shared.open(url)
                    decisionHandler(.cancel)
                    return
                }
            }
            decisionHandler(.allow)
        }

        private func extractAndStoreAuthToken(from webView: WKWebView) {
            guard let url = webView.url else { return }

            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                for cookie in cookies {
                    if cookie.name == "longhouse_session" && url.host == cookie.domain.replacingOccurrences(of: ".", with: "", options: .anchored) || cookie.domain.contains(url.host ?? "") {
                        let tokenValue = "longhouse_session=\(cookie.value)"
                        KeychainHelper.saveAuthToken(tokenValue)
                        return
                    }
                }
            }
        }
    }
}
