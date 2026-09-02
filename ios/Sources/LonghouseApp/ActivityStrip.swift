import SwiftUI

/// The dock's activity instrument: one bar per frame the phone received on
/// the session stream, drifting left over a twelve-second window and fading
/// as it ages. Height encodes the kind of frame (tool boundary, message,
/// text delta). Nothing loops — when nothing arrives the strip goes flat,
/// which is exactly the state a pulsing dot could never show.
///
/// Colour is the session tone (live green, attention orange, idle grey);
/// the strip never introduces a colour of its own.
struct ActivityStrip: View {
    @ObservedObject var store: ActivityPulseStore
    let tone: Color
    /// Drift only while the session is executing; an idle strip is a still
    /// image of whatever last happened.
    let isLive: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let size = CGSize(width: 28, height: 14)

    var body: some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !isLive || reduceMotion)) { context in
            Canvas(rendersAsynchronously: false) { ctx, size in
                // With drift paused the schedule date is stale; bars still need
                // to sit where they belong the moment a new frame lands.
                draw(in: &ctx, size: size, now: (isLive && !reduceMotion) ? context.date : Date())
            }
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .accessibilityHidden(true)
    }

    private func draw(in ctx: inout GraphicsContext, size: CGSize, now: Date) {
        let window = ActivityPulseStore.window
        let baseY = size.height - 1
        let barWidth: CGFloat = 1.5
        let usable = size.height - 2

        ctx.fill(
            Path(CGRect(x: 0, y: baseY - 0.5, width: size.width, height: 1)),
            with: .color(tone.opacity(0.28))
        )

        for pulse in store.pulses.reversed() {
            let age = now.timeIntervalSince(pulse.at)
            if age < 0 { continue }
            if age > window { break }
            let progress = age / window
            let x = size.width - CGFloat(progress) * size.width
            let height = max(2, usable * CGFloat(pulse.kind.height))
            let alpha = 0.3 + 0.7 * (1 - progress)
            let rect = CGRect(x: x - barWidth, y: baseY - height, width: barWidth, height: height)
            ctx.fill(
                Path(roundedRect: rect, cornerRadius: barWidth / 2),
                with: .color(tone.opacity(alpha))
            )
        }
    }
}
