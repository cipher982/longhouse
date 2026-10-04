import SwiftUI

/// Colours of the hearth. Amber is the flame's own colour and the only one in
/// the panel that asks for the user (`needsYou`).
enum HearthPalette {
    static let flameCore = Color(.sRGB, red: 1.0, green: 0.945, blue: 0.769)
    static let flame = Color(.sRGB, red: 1.0, green: 0.706, blue: 0.290)
    static let ember = Color(.sRGB, red: 0.941, green: 0.384, blue: 0.110)
    static let ash = Color(.sRGB, red: 0.659, green: 0.149, blue: 0.055)

    static func needsYou(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? flame : Color(.sRGB, red: 0.70, green: 0.40, blue: 0.0)
    }

    /// System-level warning (inspect). Yellow, so it never reads as a session
    /// waiting on the user; darkened on light glass to stay legible.
    static let warning = Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? .systemYellow
            : NSColor(srgbRed: 0.60, green: 0.45, blue: 0.0, alpha: 1)
    })
    static let fault = Color(nsColor: .systemRed)
    static let ok = Color(nsColor: .systemGreen)

    /// Port of the web hearth's coalColor(): ambient through dull red to orange.
    static func coal(_ kelvin: Double) -> Color {
        let stops: [(Double, (Double, Double, Double))] = [
            (300, (30, 24, 20)), (600, (38, 26, 20)), (720, (92, 26, 12)),
            (850, (158, 42, 14)), (1000, (218, 86, 26)), (1150, (245, 142, 44)),
        ]
        return ramp(kelvin, stops)
    }

    /// Blackbody approximation for embers: 0 cold ... 1 white hot.
    static func blackbody(_ value: Double) -> Color {
        let stops: [(Double, (Double, Double, Double))] = [
            (0, (60, 8, 2)), (0.3, (168, 38, 14)), (0.55, (240, 98, 28)), (0.8, (255, 180, 74)), (1, (255, 241, 196)),
        ]
        return ramp(value, stops)
    }

    private static func ramp(_ x: Double, _ stops: [(Double, (Double, Double, Double))]) -> Color {
        var lo = stops[0], hi = stops[stops.count - 1]
        if x <= lo.0 { hi = lo }
        else if x < hi.0 {
            for index in 1..<stops.count where x <= stops[index].0 {
                lo = stops[index - 1]
                hi = stops[index]
                break
            }
        } else { lo = hi }
        let f = hi.0 == lo.0 ? 0 : (x - lo.0) / (hi.0 - lo.0)
        return Color(
            .sRGB,
            red: (lo.1.0 + (hi.1.0 - lo.1.0) * f) / 255,
            green: (lo.1.1 + (hi.1.1 - lo.1.1) * f) / 255,
            blue: (lo.1.2 + (hi.1.2 - lo.1.2) * f) / 255
        )
    }
}

private struct HearthAnimatingKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// True only while the panel is on screen. Flames are a single still frame
    /// otherwise, so a hidden panel and fixture renders draw nothing per frame.
    var hearthAnimating: Bool {
        get { self[HearthAnimatingKey.self] }
        set { self[HearthAnimatingKey.self] = newValue }
    }
}

/// A small fire whose height and temperature follow `heat` (0...1), drawn in
/// the web timeline hearth's colours. Below ~0.12 it is an unlit coal bed and
/// never animates. Every frame is a pure function of time, so a paused flame
/// and a fixture render are the same deterministic picture.
struct HearthFlame: View {
    let heat: Double
    let seed: Int
    var embers = true

    @Environment(\.hearthAnimating) private var animating
    @Environment(\.colorScheme) private var colorScheme

    static let frameInterval: TimeInterval = 1.0 / 30.0

    private var lit: Bool { heat > 0.12 }

    var body: some View {
        TimelineView(.animation(minimumInterval: Self.frameInterval, paused: !(animating && lit))) { context in
            let time = animating
                ? context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 3600)
                : 1.3 + Double(seed % 17) * 0.37
            Canvas { graphics, size in
                HearthFlameRenderer(
                    heat: max(0, min(1, heat)),
                    seed: seed,
                    time: time,
                    embers: embers,
                    additive: colorScheme == .dark
                ).draw(in: &graphics, size: size)
            }
        }
        .accessibilityHidden(true)
    }
}

private struct HearthFlameRenderer {
    let heat: Double
    let seed: Int
    let time: Double
    let embers: Bool
    let additive: Bool

    /// Smooth value noise in time, so tongues flicker without strobing.
    private func flicker(_ k: Double, _ salt: Int) -> Double {
        let s = Double(salt)
        return sin(time * (5.1 + k) + s) * 0.5
            + sin(time * (8.7 + k * 1.7) + s * 2.3) * 0.3
            + sin(time * 13.3 + s * 0.7) * 0.2
    }

    private func hash01(_ a: Int, _ b: Int) -> Double {
        var x = UInt64(bitPattern: Int64(a &* 73_856_093 ^ b &* 19_349_663))
        x ^= x >> 33
        x = x &* 0xff51afd7ed558ccd
        x ^= x >> 33
        return Double(x % 10_000) / 10_000
    }

    /// Teardrop tongue: rounded base at (cx, base), tip bent sideways by `bend`.
    private func tongue(cx: Double, base: Double, width: Double, height: Double, bend: Double) -> Path {
        var path = Path()
        let tip = CGPoint(x: cx + bend, y: base - height)
        path.move(to: CGPoint(x: cx - width / 2, y: base))
        path.addCurve(
            to: tip,
            control1: CGPoint(x: cx - width * 0.62, y: base - height * 0.45),
            control2: CGPoint(x: cx - width * 0.08 + bend * 0.5, y: base - height * 0.78)
        )
        path.addCurve(
            to: CGPoint(x: cx + width / 2, y: base),
            control1: CGPoint(x: cx + width * 0.08 + bend * 0.5, y: base - height * 0.78),
            control2: CGPoint(x: cx + width * 0.62, y: base - height * 0.45)
        )
        path.addQuadCurve(to: CGPoint(x: cx - width / 2, y: base), control: CGPoint(x: cx, y: base + width * 0.42))
        path.closeSubpath()
        return path
    }

    func draw(in graphics: inout GraphicsContext, size: CGSize) {
        let w = size.width, h = size.height
        let baseY = h * 0.84
        let scale = max(1, w / 30)

        if heat > 0.05 {
            // Warm light on the hearth floor: a gradient, not a blur pass.
            let glow = CGRect(x: 0, y: baseY - h * 0.5 * heat, width: w, height: h * 0.6 * heat + 8)
            graphics.fill(
                Path(ellipseIn: glow),
                with: .radialGradient(
                    Gradient(colors: [HearthPalette.ember.opacity(0.34 * heat), .clear]),
                    center: CGPoint(x: glow.midX, y: glow.midY),
                    startRadius: 0,
                    endRadius: max(glow.width, glow.height) / 2
                )
            )
        }

        if heat > 0.12 {
            let tongues = heat > 0.75 ? 3 : (heat > 0.4 ? 2 : 1)
            // Two blur passes per flame: soft outer layers, crisp inner ones.
            for (layers, blur) in [([0, 1], 1.9 * scale), ([2, 3], 0.9 * scale)] {
                graphics.drawLayer { layer in
                    layer.addFilter(.blur(radius: blur))
                    if additive { layer.blendMode = .plusLighter }
                    for k in 0..<tongues {
                        let side = k == 0 ? 0.0 : (k == 1 ? -0.17 : 0.17)
                        let size = k == 0 ? 1.0 : 0.62
                        let height = h * (0.22 + 0.58 * heat) * size * (0.86 + 0.14 * flicker(Double(k), seed &+ k))
                        let width = w * (0.34 + 0.16 * heat) * size
                        let bend = w * 0.09 * flicker(Double(k) + 3, seed &+ 40 &+ k)
                        for index in layers {
                            let (color, inset): (Color, Double) = [
                                (HearthPalette.ash, 1.0), (HearthPalette.ember, 0.78),
                                (HearthPalette.flame, 0.55), (HearthPalette.flameCore, 0.3),
                            ][index]
                            layer.fill(
                                tongue(
                                    cx: w * (0.5 + side),
                                    base: baseY - Double(index) * 0.5,
                                    width: width * inset,
                                    height: height * (0.45 + 0.55 * inset),
                                    bend: bend * inset
                                ),
                                with: .color(color.opacity(index == 0 ? 0.75 : 0.85))
                            )
                        }
                    }
                }
            }

            if embers {
                var sparks = graphics
                if additive { sparks.blendMode = .plusLighter }
                let count = Int(18 * heat * 0.5)
                for index in 0..<count {
                    let life = 0.7 + hash01(seed, index) * 0.8
                    let age = ((time + hash01(seed, index + 100) * life).truncatingRemainder(dividingBy: life)) / life
                    let x = w * (0.5 + (hash01(seed, index + 200) - 0.5) * 0.4 * (1 - age * 0.5))
                        + sin(time * 4 + Double(index)) * w * 0.05 * age
                    let y = baseY - h * 0.15 - age * h * (0.4 + 0.5 * heat)
                    let r = max(0.6, w * 0.022 * (1 - age))
                    sparks.fill(
                        Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)),
                        with: .color(HearthPalette.blackbody(0.9 - age * 0.6).opacity((1 - age) * 0.9))
                    )
                }
            }
        }

        // Coal bed: dark lumps with glowing seams, hotter as the fire works.
        // Unlit, it is grey ash: visible on glass, plainly not burning.
        let cold = heat <= 0.12
        let ash = Color(white: additive ? 0.36 : 0.62)
        let surface = 420 + heat * 760 + (cold ? 0 : flicker(9, seed) * 30 * heat)
        let lumps: [(Double, Double, Double, Double)] = [
            (-0.2, 0.17, 0.075, 0.012), (0.21, 0.16, 0.07, 0.016), (0, 0.19, 0.085, -0.006),
        ]
        for (index, lump) in lumps.enumerated() {
            let (dx, rx, ry, dy) = lump
            let cx = w * (0.5 + dx), cy = baseY + h * dy
            let rect = CGRect(x: cx - w * rx, y: cy - h * ry, width: w * rx * 2, height: h * ry * 2)
            if cold {
                graphics.fill(Path(ellipseIn: rect), with: .color(ash.opacity(0.9 - Double(index) * 0.15)))
                continue
            }
            graphics.fill(Path(ellipseIn: rect), with: .color(HearthPalette.coal(surface - 420)))
            var seam = Path()
            seam.move(to: CGPoint(x: rect.minX + rect.width * 0.18, y: rect.midY + rect.height * 0.1))
            seam.addLine(to: CGPoint(x: rect.midX - rect.width * 0.05, y: rect.midY - rect.height * (0.15 + 0.1 * Double(index % 2))))
            seam.addLine(to: CGPoint(x: rect.midX + rect.width * 0.12, y: rect.midY + rect.height * 0.12))
            seam.addLine(to: CGPoint(x: rect.maxX - rect.width * 0.2, y: rect.midY - rect.height * 0.08))
            graphics.stroke(
                seam,
                with: .color(HearthPalette.coal(surface + 40)),
                style: StrokeStyle(lineWidth: max(0.7, w * 0.03), lineCap: .round, lineJoin: .round)
            )
        }
    }
}
