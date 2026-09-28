import Foundation
import SwiftUI

/// The one continuous Balanced surface shared by status and composer. Meaning
/// comes from served facts immediately; only these background layers settle.
enum SessionSignalMaterialKind: Equatable {
    case working
    case exception
    case settled
}

struct SessionSignalField<Content: View>: View {
    let detail: SessionDetail
    @ObservedObject var activity: ActivityPulseStore
    let content: Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.scenePhase) private var scenePhase
    @State private var fieldNow = Date()
    @State private var receiptTask: Task<Void, Never>?
    @State private var receiptActive = false
    @State private var receiptOpacity = 0.0
    @State private var lastObservedPulseAt: Date?

    init(
        detail: SessionDetail,
        activity: ActivityPulseStore,
        @ViewBuilder content: () -> Content
    ) {
        self.detail = detail
        self.activity = activity
        self.content = content()
    }

    private var materialKind: SessionSignalMaterialKind {
        switch detail.ledgerEvidence(asOf: fieldNow) {
        case .working: return .working
        // Exception material is reserved for a state the user owns, and the only
        // one the ledger raises that way is an unresolved interaction: a
        // question or an approval. A stalled turn, an auth requirement and a
        // control fault keep the served tone on the headline and dot instead, and
        // "no current observation" is information rather than an alarm.
        case .attention: return .exception
        case .uncertain, .quiet: return .settled
        }
    }

    private var holdMotion: Bool { reduceMotion || UITestHooks.holdsAmbientMotion }

    private var activityDeadlineKey: String {
        "\(detail.id):\(detail.stateFacts.activityValidUntil ?? "")"
    }

    var body: some View {
        let kind = materialKind
        content
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                ZStack(alignment: .top) {
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(workMaterial)
                        .opacity(kind == .working ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(exceptionMaterial)
                        .opacity(kind == .exception ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(settledMaterial)
                        .opacity(kind == .settled ? 1 : 0)
                        .animation(
                            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                            value: kind
                        )
                    workSheen(kind: kind)
                    ActivityReceiptTrail(
                        store: activity,
                        // Flame is far more saturated than the old pale green;
                        // at half strength the bars stay texture under the text.
                        tone: signalTone.opacity(0.5),
                        evidenceLive: kind == .working
                    )
                    .opacity(kind == .working ? 1 : 0)
                    .animation(
                        reduceMotion ? nil : .linear(duration: 0.12),
                        value: kind
                    )
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .fill(
                            RadialGradient(
                                colors: [signalTone.opacity(0.16), .clear],
                                center: UnitPoint(x: 0.72, y: 0.54),
                                startRadius: 2,
                                endRadius: 240
                            )
                        )
                        .opacity(receiptOpacity)
                }
                .id(reduceMotion)
                .clipped()
            }
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay { BrassCornerPoints(inset: 9) }
            .overlay(
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .strokeBorder(borderColor(for: kind), lineWidth: 0.75)
                    .animation(
                        reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.36),
                        value: kind
                    )
            )
            .shadow(color: .black.opacity(colorScheme == .dark ? 0.28 : 0.12), radius: 16, y: 5)
            .task(id: activityDeadlineKey) {
                fieldNow = Date()
                guard let deadline = detail.stateFacts.activityValidUntil.flatMap(LonghouseDateParser.parse) else {
                    return
                }
                let remaining = deadline.timeIntervalSinceNow
                if remaining > 0 {
                    try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
                }
                if !Task.isCancelled { fieldNow = Date() }
            }
            .onAppear {
                fieldNow = Date()
                lastObservedPulseAt = activity.latestPulseAt
            }
            .onChange(of: activity.latestPulseAt) { _, latest in
                guard latest != lastObservedPulseAt else { return }
                lastObservedPulseAt = latest
                startReceiptAccent()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { fieldNow = Date() }
            }
            .onChange(of: materialKind) { _, kind in
                fieldNow = Date()
                guard kind != .working, receiptActive else { return }
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                withAnimation(reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.12)) {
                    receiptOpacity = 0
                }
            }
            .onChange(of: detail.id) { _, _ in
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
                lastObservedPulseAt = activity.latestPulseAt
            }
            .onDisappear {
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
            }
            .onChange(of: reduceMotion) { _, reduced in
                guard reduced else { return }
                receiptTask?.cancel()
                receiptTask = nil
                receiptActive = false
                receiptOpacity = 0
            }
            .accessibilityElement(children: .contain)
    }

    private func workSheen(kind: SessionSignalMaterialKind) -> some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: kind != .working || holdMotion)) { context in
            let phase = context.date.timeIntervalSinceReferenceDate
                .truncatingRemainder(dividingBy: 2.8) / 2.8
            let intensity = holdMotion ? 0.08 : 0.09 + 0.07 * (0.5 + 0.5 * sin(phase * 2 * .pi))
            RoundedRectangle(cornerRadius: 24, style: .continuous)
                .fill(RadialGradient(
                    colors: [sheenColor.opacity(intensity), .clear],
                    center: UnitPoint(x: 0.65, y: 0),
                    startRadius: 0,
                    endRadius: 260
                ))
        }
        .opacity(kind == .working ? 1 : 0)
        .animation(
            reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.12),
            value: kind
        )
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
    private func startReceiptAccent() {
        guard materialKind == .working, !receiptActive else { return }
        receiptActive = true
        receiptTask?.cancel()
        withAnimation(reduceMotion ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.156)) {
            receiptOpacity = 1
        }
        receiptTask = Task { @MainActor in
            if reduceMotion {
                try? await Task.sleep(nanoseconds: 1_300_000_000)
            } else {
                try? await Task.sleep(nanoseconds: 156_000_000)
                guard !Task.isCancelled else { return }
                withAnimation(.timingCurve(0.2, 0.8, 0.2, 1, duration: 1.144)) {
                    receiptOpacity = 0
                }
                try? await Task.sleep(nanoseconds: 1_144_000_000)
            }
            guard !Task.isCancelled else { return }
            receiptOpacity = 0
            receiptActive = false
            receiptTask = nil
        }
    }

    /// Live work reads as flame, the web composer's running glow.
    private var signalTone: Color { Ember.flame }

    private var sheenColor: Color {
        colorScheme == .dark ? Ember.uiHex(0xFFB25A) : Ember.uiHex(0xFFE2B8)
    }

    private var workMaterial: LinearGradient {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Ember.uiHex(0x2A1B12), Ember.uiHex(0x1A120E)]
                : [Ember.uiHex(0xFFF6E8), Ember.uiHex(0xFBEBD4)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var exceptionMaterial: LinearGradient {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Ember.uiHex(0x2C1510), Ember.uiHex(0x1B0F0C)]
                : [Ember.uiHex(0xFCEDE3), Ember.uiHex(0xF6DECF)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var settledMaterial: Color { Ember.card }

    private func borderColor(for kind: SessionSignalMaterialKind) -> Color {
        switch kind {
        case .working: return signalTone.opacity(0.28)
        case .exception: return TranscriptPalette.attention.opacity(0.48)
        case .settled: return Ember.border.opacity(0.9)
        }
    }
}
