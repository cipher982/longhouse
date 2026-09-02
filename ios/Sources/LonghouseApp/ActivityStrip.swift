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

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// True from the newest frame until it leaves the window. The 30 fps
    /// schedule runs only inside that span: a quiet or wedged session costs
    /// nothing, and the strip cannot outlive its own evidence.
    @State private var drifting = false

    private var holdStill: Bool { reduceMotion || UITestHooks.holdsAmbientMotion }

    static let size = CGSize(width: 32, height: 14)

    var body: some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !drifting || holdStill)) { context in
            Canvas(rendersAsynchronously: false) { ctx, size in
                // With drift paused the schedule date is stale; bars still need
                // to sit where they belong the moment a new frame lands or the
                // last one expires.
                draw(in: &ctx, size: size, now: (drifting && !holdStill) ? context.date : Date())
            }
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .accessibilityHidden(true)
        .task(id: store.pulses.last?.at) {
            guard let last = store.pulses.last else {
                drifting = false
                return
            }
            drifting = true
            let remaining = last.at.addingTimeInterval(ActivityPulseStore.window).timeIntervalSinceNow
            if remaining > 0 {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            }
            // Flipping the flag re-evaluates the body once more, which is the
            // one-shot redraw that clears the expired bars under Reduce Motion.
            if !Task.isCancelled { drifting = false }
        }
    }

    private func draw(in ctx: inout GraphicsContext, size: CGSize, now: Date) {
        let window = ActivityPulseStore.window
        let baseY = size.height - 1
        let barWidth: CGFloat = 2
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
