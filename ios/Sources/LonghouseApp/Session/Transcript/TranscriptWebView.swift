import Foundation
import SwiftUI
import UIKit
import WebKit
import OSLog

/// The transcript WebView's height is not constant: the floating control card,
/// the keyboard, and the safe area all resize it through SwiftUI. UIScrollView
/// does not re-clamp `contentOffset` when its bounds change, so a pinned
/// transcript ends up short of (or past) its last row — the reported symptom
/// was a black band the exact height of a dismissed keyboard, which is the
/// viewport delta, cleared by a drag because a drag reconciles the offset.
///
/// The DOM's `resize` listener already tried to cover this, but only if WebKit
/// delivers the event; its regression test dispatches the event by hand, so the
/// trigger was never under test. `layoutSubviews` is the one trigger UIKit
/// guarantees on a frame change.
final class TranscriptWebView: WKWebView {
    let transcriptInstanceID = UUID().uuidString.prefix(8)
    /// (previousHeight, newHeight). Only fires on a real height change.
    var onViewportHeightChange: ((CGFloat, CGFloat) -> Void)?
    /// Seeded to zero rather than "unset": treating the first layout as a
    /// baseline to record silently swallows a change when the first layout pass
    /// IS the change, which is exactly what happens to a pooled WebView adopted
    /// into a session whose chrome is already a different height.
    private var observedHeight: CGFloat = 0

    override func layoutSubviews() {
        super.layoutSubviews()
        let height = bounds.height
        guard height > 0, abs(height - observedHeight) > 0.5 else { return }
        let previous = observedHeight
        observedHeight = height
        onViewportHeightChange?(previous, height)
    }

    func prepareForTranscriptReuse() {
        onViewportHeightChange = nil
        observedHeight = 0
    }
}
