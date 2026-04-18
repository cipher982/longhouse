import SwiftUI
import WebKit

private enum LonghouseNativeAuthBridge {
    static let messageHandlerName = "longhouseNativeAuth"
    static let userScriptSource = #"""
    (() => {
      if (window.LonghouseNativeAuth?.requestAuth) {
        return;
      }

      window.LonghouseNativeAuth = Object.freeze({
        requestAuth(payload = {}) {
          try {
            window.webkit.messageHandlers.longhouseNativeAuth.postMessage(payload);
          } catch (_) {}
        }
      });
    })();
    """#
}

private enum LonghouseWebRouteObserver {
    static let messageHandlerName = "longhouseRouteObserver"
    static let userScriptSource = #"""
    (() => {
      if (window.__longhouseRouteObserverInstalled) {
        return;
      }
      window.__longhouseRouteObserverInstalled = true;

      const notify = () => {
        try {
          window.webkit.messageHandlers.longhouseRouteObserver.postMessage(window.location.href);
        } catch (_) {}
      };

      const wrapHistory = (methodName) => {
        const original = history[methodName];
        if (typeof original !== "function") {
          return;
        }
        history[methodName] = function () {
          const result = original.apply(this, arguments);
          notify();
          return result;
        };
      };

      wrapHistory("pushState");
      wrapHistory("replaceState");
      window.addEventListener("popstate", notify);
      window.addEventListener("hashchange", notify);
      notify();
    })();
    """#
}

private struct LonghouseNativeAuthRequest {
    let postLoginPath: String

    static func fromMessageBody(_ body: Any) -> LonghouseNativeAuthRequest? {
        guard let payload = body as? [String: Any] else {
            return nil
        }

        let rawReturnTo = payload["return_to"] as? String
        return LonghouseNativeAuthRequest(
            postLoginPath: LonghouseWebNavigation.postLoginPath(fromReturnTo: rawReturnTo)
        )
    }
}

struct LonghouseWebView: UIViewRepresentable {
    let serverURL: String
    /// Path to load on initial mount (e.g. /timeline or /timeline/abc-123).
    let initialPath: String
    /// Called when the shell needs to take over auth instead of rendering a web login page.
    let onAuthRedirect: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(serverURL: serverURL, onAuthRedirect: onAuthRedirect)
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.websiteDataStore = .default()
        let userContentController = WKUserContentController()
        let nativeAuthBridgeScript = WKUserScript(
            source: LonghouseNativeAuthBridge.userScriptSource,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        let routeObserverScript = WKUserScript(
            source: LonghouseWebRouteObserver.userScriptSource,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        userContentController.addUserScript(nativeAuthBridgeScript)
        userContentController.addUserScript(routeObserverScript)
        userContentController.add(context.coordinator, name: LonghouseNativeAuthBridge.messageHandlerName)
        userContentController.add(context.coordinator, name: LonghouseWebRouteObserver.messageHandlerName)
        config.userContentController = userContentController

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
        let onAuthRedirect: (String) -> Void
        private var authHandoffInFlight = false

        init(serverURL: String, onAuthRedirect: @escaping (String) -> Void) {
            self.serverURL = serverURL
            self.onAuthRedirect = onAuthRedirect
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            Task { @MainActor in
                await BrowserSessionStore.syncWebKitCookiesToShared(for: serverURL)
                await BrowserSessionStore.persistAccessTokenFromWebKit(for: serverURL)
            }
            handleObservedRoute(webView.url, in: webView)
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
                takeOverAuth(
                    postLoginPath: LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL),
                    in: webView
                )
            case .externalBrowser:
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            }
        }

        private func handleObservedRoute(_ url: URL?, in webView: WKWebView) {
            guard let url else {
                return
            }
            guard LonghouseWebNavigation.decision(for: url, serverURL: serverURL) == .nativeAuth else {
                return
            }
            takeOverAuth(
                postLoginPath: LonghouseWebNavigation.postLoginPath(from: url, serverURL: serverURL),
                in: webView
            )
        }

        private func takeOverAuth(postLoginPath: String, in webView: WKWebView) {
            guard !authHandoffInFlight else {
                return
            }

            authHandoffInFlight = true
            webView.stopLoading()
            webView.loadHTMLString("", baseURL: nil)
            onAuthRedirect(postLoginPath)
        }
    }
}

extension LonghouseWebView.Coordinator: WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let webView = message.webView else {
            return
        }

        switch message.name {
        case LonghouseNativeAuthBridge.messageHandlerName:
            guard let request = LonghouseNativeAuthRequest.fromMessageBody(message.body) else {
                return
            }
            takeOverAuth(postLoginPath: request.postLoginPath, in: webView)
        case LonghouseWebRouteObserver.messageHandlerName:
            guard let rawURL = message.body as? String,
                  let url = URL(string: rawURL) else {
                return
            }
            handleObservedRoute(url, in: webView)
        default:
            return
        }
    }
}
