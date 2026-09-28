import SwiftUI

/// Real workspace arrivals form a twelve-second history in the material.
/// The trace stops and clears when provider evidence is no longer valid.
struct ActivityReceiptTrail: View {
    @ObservedObject var store: ActivityPulseStore
    let tone: Color
    var evidenceLive: Bool = true

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drifting = false

    private var holdStill: Bool { reduceMotion || UITestHooks.holdsAmbientMotion }

    var body: some View {
        Group {
            if evidenceLive {
                SwiftUI.TimelineView(
                    .animation(minimumInterval: 1.0 / 30.0, paused: !drifting || holdStill)
                ) { context in
                    Canvas(rendersAsynchronously: false) { ctx, size in
                        draw(in: &ctx, size: size, now: (drifting && !holdStill) ? context.date : Date())
                    }
                }
            } else {
                Canvas(rendersAsynchronously: false) { ctx, size in
                    draw(in: &ctx, size: size, now: Date(), includePulses: false)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .frame(height: 76)
        .mask(
            LinearGradient(
                colors: [.black, .black.opacity(0.65), .clear],
                startPoint: .bottom,
                endPoint: .top
            )
        )
        .accessibilityHidden(true)
        .task(id: "\(store.latestPulseAt?.timeIntervalSince1970 ?? 0)-\(evidenceLive)") {
            guard evidenceLive, store.latestPulseAt != nil else {
                drifting = false
                return
            }
            drifting = true
            guard let latest = store.latestPulseAt else { return }
            let remaining = latest.addingTimeInterval(ActivityPulseStore.window).timeIntervalSinceNow
            if remaining > 0 {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
            }
            if !Task.isCancelled { drifting = false }
        }
    }

    private func draw(
        in ctx: inout GraphicsContext,
        size: CGSize,
        now: Date,
        includePulses: Bool = true
    ) {
        let window = ActivityPulseStore.window
        let baseY = size.height - 1
        let barWidth: CGFloat = 2
        let usable = size.height - 2
        guard includePulses else { return }

        for pulse in store.pulses.reversed() {
            let age = now.timeIntervalSince(pulse.at)
            if age < 0 { continue }
            if age > window { break }
            let progress = age / window
            let x = size.width - CGFloat(progress) * size.width
            let height = max(3, usable * CGFloat(pulse.kind.height))
            let alpha = 0.06 + 0.28 * (1 - progress)
            let rect = CGRect(x: x - barWidth, y: baseY - height, width: barWidth, height: height)
            ctx.fill(
                Path(roundedRect: rect, cornerRadius: barWidth / 2),
                with: .color(tone.opacity(alpha))
            )
        }
    }
}
