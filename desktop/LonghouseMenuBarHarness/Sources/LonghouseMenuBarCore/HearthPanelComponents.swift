import SwiftUI

/// Where a managed session sits in the Hearth panel. Order is priority.
enum HearthSessionKind: Int, Comparable {
    /// Explicitly waiting for an answer or approval.
    case needsYou
    /// Blocked or stalled: waiting on something the user may need to clear.
    case blocked
    /// Thinking, executing, starting.
    case working
    /// Longhouse lost its control path (terminal detached, bridge degraded).
    case lostControl
    /// Idle, ended, unknown or without live status: folded into one line.
    case quiet

    static func < (lhs: Self, rhs: Self) -> Bool { lhs.rawValue < rhs.rawValue }

    /// Kinds that can take the focus card.
    var asksForUser: Bool { self == .needsYou || self == .blocked }
}

extension ManagedSessionSnapshot {
    var hearthKind: HearthSessionKind {
        if explicitlyNeedsUser { return .needsYou }
        if menuBarAttentionKind == .blocked { return .blocked }
        if needsManagedSessionAttention { return .lostControl }
        if menuBarAttentionKind == .working { return .working }
        return .quiet
    }

    /// Fire size, 0...1. Executing burns hottest; waiting keeps a low flame;
    /// anything below ~0.12 is an unlit coal bed.
    var hearthHeat: Double {
        switch hearthKind {
        case .needsYou: return 0.26
        case .blocked: return 0.16
        case .lostControl, .quiet: return 0.04
        case .working:
            switch presentation?.primary?.key {
            case "executing": return 0.95
            case "starting": return 0.45
            default: return 0.72
            }
        }
    }
}

struct HearthSessionEntry: Identifiable {
    let id: String
    let provider: String
    let title: String
    let kind: HearthSessionKind
    let heat: Double
    /// Workspace and live detail ("zerg / main · Bash"), or what it waits for.
    let subtitle: String
    let ageLabel: String
    let seed: Int
    let openAction: (() -> Void)?
    let stopAction: (() -> Void)?
}

// MARK: - Focus card

/// The one session that needs the user right now, given the stage.
struct HearthFocusCard: View {
    let entry: HearthSessionEntry
    @Environment(\.colorScheme) private var colorScheme
    @State private var hovered = false

    var body: some View {
        let amber = HearthPalette.needsYou(colorScheme)
        Button(action: { entry.openAction?() }) {
            HStack(alignment: .center, spacing: 12) {
                HearthFlame(heat: max(entry.heat, 0.34), seed: entry.seed)
                    .frame(width: 42, height: 58)

                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.subtitle)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(amber)
                        .lineLimit(1)
                    Text(entry.title)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Color.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 4) {
                        ProviderGlyph(provider: entry.provider, size: 11, variant: .bare)
                        Text(entry.ageLabel == "-" ? HealthSnapshot.providerDisplayName(entry.provider) : "\(HealthSnapshot.providerDisplayName(entry.provider)) · waiting \(entry.ageLabel)")
                            .lineLimit(1)
                    }
                    .font(.system(size: 11))
                    .foregroundStyle(Color.secondary)
                }

                Spacer(minLength: 4)

                if entry.openAction != nil {
                    Text("Open")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(colorScheme == .dark ? Color.black.opacity(0.85) : Color.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background(Capsule(style: .continuous).fill(amber.opacity(hovered ? 1 : 0.9)))
                }
            }
            .padding(.leading, 6)
            .padding(.trailing, 12)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(amber.opacity(colorScheme == .dark ? 0.09 : 0.12))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(amber.opacity(0.28), lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(entry.openAction == nil)
        .onHover { hovered = $0 }
        .contextMenu { HearthSessionMenu(entry: entry) }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(Text("\(entry.title), \(entry.subtitle)"))
        .accessibilityAddTraits(.isButton)
    }
}

// MARK: - Rows

/// A session with a fire: the flame is the row's left edge.
struct HearthSessionRow: View {
    let entry: HearthSessionEntry
    @Environment(\.colorScheme) private var colorScheme
    @State private var hovered = false

    private var subtitleColor: Color {
        switch entry.kind {
        case .needsYou, .blocked: return HearthPalette.needsYou(colorScheme)
        case .lostControl: return HearthPalette.warning
        default: return Color.secondary
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Button(action: { entry.openAction?() }) {
                HStack(spacing: 10) {
                    HearthFlame(heat: entry.heat, seed: entry.seed)
                        .frame(width: 26, height: 36)

                    VStack(alignment: .leading, spacing: 1) {
                        Text(entry.title)
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(Color.primary)
                            .lineLimit(1)
                        HStack(spacing: 4) {
                            ProviderGlyph(provider: entry.provider, size: 11, variant: .bare)
                            Text(entry.subtitle)
                                .lineLimit(1)
                        }
                        .font(.system(size: 11))
                        .foregroundStyle(subtitleColor)
                    }

                    Spacer(minLength: 6)

                    if entry.kind != .working, entry.ageLabel != "-" {
                        Text(entry.ageLabel)
                            .font(.system(size: 11).monospacedDigit())
                            .foregroundStyle(entry.kind.asksForUser ? subtitleColor : Color.secondary)
                    }
                }
                .padding(.leading, 2)
                .padding(.trailing, 6)
                .padding(.vertical, 1)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(entry.openAction == nil)
            .accessibilityLabel(Text("Open \(entry.title) in Longhouse"))

            HearthRowTrailing(entry: entry, hovered: hovered)
        }
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(hovered ? 0.06 : 0))
        )
        .onHover { hovered = $0 }
        .contextMenu { HearthSessionMenu(entry: entry) }
    }
}

/// Compact row for a quiet session: no fire, just identity and age.
struct HearthQuietRow: View {
    let entry: HearthSessionEntry
    @State private var hovered = false

    var body: some View {
        HStack(spacing: 6) {
            Button(action: { entry.openAction?() }) {
                HStack(spacing: 8) {
                    ProviderGlyph(provider: entry.provider, size: 13, variant: .bare)
                        .frame(width: 26)
                    Text(entry.title)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.primary.opacity(0.85))
                        .lineLimit(1)
                    if !entry.subtitle.isEmpty {
                        Text(entry.subtitle)
                            .font(.system(size: 11))
                            .foregroundStyle(Color.secondary)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    Spacer(minLength: 6)
                    if entry.ageLabel != "-" {
                        Text(entry.ageLabel)
                            .font(.system(size: 11).monospacedDigit())
                            .foregroundStyle(Color.secondary)
                    }
                }
                .padding(.leading, 2)
                .padding(.trailing, 6)
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(entry.openAction == nil)
            .accessibilityLabel(Text("Open \(entry.title) in Longhouse"))

            HearthRowTrailing(entry: entry, hovered: hovered)
        }
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(hovered ? 0.06 : 0))
        )
        .onHover { hovered = $0 }
        .contextMenu { HearthSessionMenu(entry: entry) }
    }
}

/// Stop appears on hover only; the chevron says the row opens.
private struct HearthRowTrailing: View {
    let entry: HearthSessionEntry
    let hovered: Bool

    var body: some View {
        HStack(spacing: 2) {
            if let stopAction = entry.stopAction {
                Button(action: stopAction) {
                    Image(systemName: "stop.circle")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.secondary)
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Stop this session")
                .accessibilityLabel(Text("Stop managed session"))
                .opacity(hovered ? 1 : 0)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(Color.secondary.opacity(hovered ? 0.9 : 0.35))
                .frame(width: 10)
                .accessibilityHidden(true)
                .opacity(entry.openAction == nil ? 0 : 1)
        }
        .padding(.trailing, 6)
    }
}

private struct HearthSessionMenu: View {
    let entry: HearthSessionEntry

    var body: some View {
        if let openAction = entry.openAction {
            Button("Open in Longhouse", action: openAction)
        }
        if let stopAction = entry.stopAction {
            Divider()
            Button("Stop Session", role: .destructive, action: stopAction)
        }
    }
}

// MARK: - Folded lines

/// One line that stands for several things and expands into them.
struct HearthFoldLine<Expanded: View>: View {
    let providers: [String]
    let title: String
    let detail: String
    @Binding var expanded: Bool
    let identifier: String
    @ViewBuilder let content: () -> Expanded

    private var uniqueProviders: [String] {
        var seen = Set<String>()
        return providers.filter { seen.insert($0).inserted }.prefix(2).map { $0 }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Button {
                withAnimation(.snappy(duration: 0.22)) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    HStack(spacing: 2) {
                        ForEach(uniqueProviders, id: \.self) { provider in
                            ProviderGlyph(provider: provider, size: 11, variant: .bare)
                        }
                    }
                    .frame(width: 26, alignment: .center)
                    .opacity(0.75)
                    Text(title)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.secondary)
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(Color.secondary.opacity(0.7))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(Color.secondary.opacity(0.6))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .padding(.trailing, 12)
                }
                .padding(.leading, 2)
                .padding(.vertical, 5)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier(identifier)
            .accessibilityLabel(Text("\(title). \(expanded ? "Collapse" : "Expand")"))

            if expanded {
                content()
                    .transition(.opacity)
            }
        }
    }
}

// MARK: - Health

struct HearthFactsList: View {
    let facts: [MenuBarSystemFact]

    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 5) {
            ForEach(facts) { fact in
                GridRow {
                    Text(fact.label)
                        .font(.system(size: 11))
                        .foregroundStyle(Color.secondary)
                        .gridColumnAlignment(.leading)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(fact.value)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(fact.promotion.factColor)
                        if let detail = fact.detail, !detail.isEmpty {
                            Text(detail)
                                .font(.system(size: 10))
                                .foregroundStyle(Color.secondary.opacity(0.8))
                                .lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension MenuBarPromotion {
    /// Normal facts stay quiet; only a fact that is wrong gets colour.
    var factColor: Color {
        switch self {
        case .normal, .needsUser: return Color.primary
        case .inspect: return HearthPalette.warning
        case .unavailable: return Color.secondary
        case .repair: return HearthPalette.fault
        }
    }

    var troubleSymbol: String {
        switch self {
        case .repair: return "xmark.octagon.fill"
        case .unavailable: return "questionmark.circle.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }
}
