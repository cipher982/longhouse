#if DEBUG
import Foundation
import OSLog
import WebKit

/// Measures, inside the real WKWebView, whether the row a reader is looking at
/// moves when content changes around it (L2 of specs/ios-transcript-stability.md).
///
/// The reader holds still once the probe starts (the UI test finishes its drag
/// first), so the anchor row's position on screen must not change. That is the
/// primary number, `maxDriftPt`, and it needs nothing from the renderer: it
/// measures what a person looking at the screen would see, so it can judge any
/// renderer, including an old one.
///
/// `maxLedgerDriftPt` is a second view for renderers that keep a correction
/// ledger (`window.__lhScrollStats().corrected`, the points the page scrolled
/// itself): movement in the document minus corrections. It equals the first
/// when the reader holds still and differs only if they scroll mid-probe.
/// Only meaningful while the transcript is not pinned to the bottom.
///
/// Installed only when the chat fixture asks for a probe file, so a normal
/// Debug launch and every Release build carry none of this.
@MainActor
enum TranscriptStabilityProbe {
    struct Result: Equatable {
        let anchor: String
        let frames: Int
        /// Frames in which the anchor row could not be found.
        let lost: Int
        let maxDriftPt: Double
        let maxLedgerDriftPt: Double
        /// Worst distance from the bottom while the probe ran (informational).
        let maxGapPt: Double
    }

    private static weak var webView: WKWebView?

    static var isEnabled: Bool { UITestHooks.chatFixtureProbePath != nil }

    /// `LONGHOUSE_STABILITY_LOG=1` makes the app log, when a session closes, how
    /// many layout shifts the page absorbed for the reader (real-data tours).
    static var isLogging: Bool { ProcessInfo.processInfo.environment["LONGHOUSE_STABILITY_LOG"] == "1" }

    private static let logger = Logger(subsystem: "ai.longhouse.ios", category: "TranscriptStability")

    /// Cumulative counters of the page's scroll owner since the document loaded.
    static func logStats(reason: String, webView: WKWebView) {
        guard isLogging else { return }
        webView.evaluateJavaScript("window.__lhScrollStats ? JSON.stringify(window.__lhScrollStats()) : 'none'") { value, _ in
            logger.info("stability stats reason=\(reason, privacy: .public) \((value as? String) ?? "unavailable", privacy: .public)")
        }
    }

    static func install(on configuration: WKWebViewConfiguration) {
        guard isEnabled else { return }
        configuration.userContentController.addUserScript(
            WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true)
        )
    }

    static func attach(_ view: WKWebView) {
        guard isEnabled || isLogging else { return }
        webView = view
    }

    /// Picks the topmost visible row as the anchor and starts sampling every frame.
    static func start() async -> String? {
        guard let webView else { return nil }
        let value = try? await webView.evaluateJavaScript("window.__lhProbe && window.__lhProbe.start()")
        return value as? String
    }

    static func stop() async -> Result? {
        guard let webView,
              let json = try? await webView.evaluateJavaScript("window.__lhProbe && window.__lhProbe.stop()") as? String,
              let data = json.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return Result(
            anchor: object["anchor"] as? String ?? "",
            frames: object["frames"] as? Int ?? 0,
            lost: object["lost"] as? Int ?? 0,
            maxDriftPt: object["maxDrift"] as? Double ?? 0,
            maxLedgerDriftPt: object["maxLedgerDrift"] as? Double ?? 0,
            maxGapPt: object["maxGap"] as? Double ?? 0
        )
    }

    private static let script = """
    window.__lhProbe = (function () {
      var root = document.getElementById('root');
      var st = null;
      function sig(e) { return e.textContent.replace(/\\s+/g, ' ').trim().slice(0, 60); }
      function topRow() {
        var c = root.children;
        for (var i = 0; i < c.length; i++) { if (c[i].getBoundingClientRect().bottom > 40) return c[i]; }
        return null;
      }
      function sample() {
        var el = null;
        if (st.sig) { for (var i = 0; i < root.children.length; i++) { if (sig(root.children[i]) === st.sig) { el = root.children[i]; break; } } }
        var y = window.scrollY;
        var maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        var corr = window.__lhScrollStats ? window.__lhScrollStats().corrected : 0;
        var top = el ? el.getBoundingClientRect().top : null;
        return { top: top, docY: el ? top + y : null, gap: maxY - y, corr: corr };
      }
      return {
        start: function () {
          var a = topRow();
          st = { sig: a ? sig(a) : null, frames: [], run: true, raf: 0 };
          var f = sample();
          st.top0 = f.top; st.docY0 = f.docY; st.corr0 = f.corr; st.frames.push(f);
          (function loop() { if (!st.run) return; st.frames.push(sample()); st.raf = requestAnimationFrame(loop); })();
          return st.sig;
        },
        stop: function () {
          st.run = false; cancelAnimationFrame(st.raf);
          var lost = 0, maxDrift = 0, maxLedger = 0, maxGap = 0;
          st.frames.forEach(function (f) {
            if (Math.abs(f.gap) > maxGap) maxGap = Math.abs(f.gap);
            if (f.docY === null || st.docY0 === null) { lost++; return; }
            var d = Math.abs(f.top - st.top0);
            if (d > maxDrift) maxDrift = d;
            var l = Math.abs((f.docY - st.docY0) - (f.corr - st.corr0));
            if (l > maxLedger) maxLedger = l;
          });
          return JSON.stringify({ anchor: st.sig, frames: st.frames.length, lost: lost, maxDrift: maxDrift, maxLedgerDrift: maxLedger, maxGap: maxGap });
        }
      };
    })();
    """
}
#endif
