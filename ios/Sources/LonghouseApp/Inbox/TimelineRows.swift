import OSLog
import SwiftUI
import WidgetKit

struct TimelineSessionCardRow: View {
    let session: SessionSummary
    let role: TimelineRowRole
    var connectivityBanner: TimelineConnectivityBanner = .none

    var body: some View {
        let signal = TimelineSignal.resolve(for: session, suppressed: connectivityBanner != .none)
        let isNewResult = role == .newResult
        let dotColor = isNewResult ? newResultStatusColor(for: session) : signal.dotColor
        // Only the rows that want you carry an edge; everything else is a
        // quiet char card and lets the dot and status line speak. A new result
        // is marked by its bold title and outcome dot, never by gold, which
        // stays the brand and the primary action.
        let edgeColor: Color? = role == .needsYou ? signal.dotColor : nil
        let dotPulses = !isNewResult && signal.pulses
        let titleWeight: Font.Weight = isNewResult ? .bold : .semibold
        let titleLineLimit = role == .needsYou || isNewResult ? 2 : 1

        // Three-line row built for glanceability:
        //  - kicker: project · machine · branch ............... when
        //  - headline: ● <frozen server-resolved title>
        //  - status: demoted runtime state, colored by signal
        // The frozen `title` (server timeline_title) is the muscle-memory anchor;
        // the leading dot + status carry "is it active / waiting on me / done".
        // No Managed badge, no turns/tools — that was the dead right half.
        HStack(alignment: .top, spacing: 11) {
            ProviderGlyph(provider: session.provider, size: 30)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if let project = session.projectLabel {
                        Text(project)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Ember.textSecondary)
                            .lineLimit(1)
                    }
                    if let machine = session.timelineMachineLabel {
                        Text("· \(machine)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    if let branch = session.timelineBranchBadgeLabel {
                        Text("· \(branch)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    Spacer(minLength: 6)
                    if !isNewResult, let duration = stateDurationLabel(for: session) {
                        Text(duration)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                            .monospacedDigit()
                    }
                }

                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    LivenessDot(color: dotColor, pulsing: dotPulses)
                        .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 4 }
                    Text(session.title)
                        .font(.subheadline.weight(titleWeight))
                        .foregroundStyle(Ember.text)
                        .lineLimit(titleLineLimit)
                }
                // The dot is color-only; fold its meaning into the headline so
                // VoiceOver announces "Waiting on you" / "Working" rather than
                // leaving amber as the sole, invisible-to-VoiceOver code.
                .accessibilityElement(children: .combine)
                .accessibilityLabel(rowAccessibilityLabel(session: session, role: role))

                if isNewResult {
                    NewResultLine(session: session)
                } else {
                    CompactRuntimeLine(session: session, signal: signal)
                }

                // B-lite drift line: the live, drifting summary title parked on a
                // demoted, low-contrast line where movement is legitimate. The
                // frozen headline above stays put (muscle memory); this is the
                // "what is it doing now" channel, shown only while actively
                // working so it never churns under a resting row.
                if role == .open, signal == .working, let drift = session.driftTitle {
                    Text("now: \(drift)")
                        .font(.caption2)
                        .italic()
                        .foregroundStyle(Ember.textMuted)
                        .lineLimit(1)
                }
            }
            TimelineHearthLamp(session: session, suppressed: connectivityBanner != .none)
                .frame(maxHeight: .infinity, alignment: .center)
        }
        .padding(.vertical, 11)
        .padding(.horizontal, 12)
        .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(alignment: .leading) {
            if let edgeColor {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(edgeColor.opacity(0.85))
                    .frame(width: 3)
                    .padding(.vertical, 12)
            }
        }
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(edgeColor?.opacity(0.28) ?? Ember.hairline, lineWidth: 0.8)
        }
    }
}

private struct NewResultLine: View {
    let session: SessionSummary

    var body: some View {
        Text(newResultStatusText(for: session))
            .font(.caption.weight(.semibold))
            .foregroundStyle(newResultStatusColor(for: session))
            .lineLimit(1)
            .accessibilityLabel(newResultStatusText(for: session))
    }
}

struct TimelineSearchResultRow: View {
    let session: SessionSummary
    let query: String

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ProviderGlyph(provider: session.provider, size: 30)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if let project = session.projectLabel {
                        Text(project)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if let machine = session.timelineMachineLabel {
                        Text("· \(machine)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 6)
                    Text(relativeTime(session.timelineAnchor))
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(.tertiary)
                }

                Text(session.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)

                if let snippet = nonEmpty(session.matchSnippet) {
                    Text(highlightedSnippet(snippet, query: query))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Text("\(session.providerLabel) · \(session.turnCount) turns")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 11)
        .padding(.horizontal, 12)
        .background(Ember.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Ember.hairline, lineWidth: 0.8)
        }
    }
}

/// Demoted runtime status line under the headline: the state label, colored by
/// the row signal, with an inline "stale" flag. The dot moved up to the
/// headline, so this line is text-only and subordinate.
private struct CompactRuntimeLine: View {
    let session: SessionSummary
    let signal: TimelineSignal

    var body: some View {
        let sessionStale = signal == .quiet && session.shouldAnnotateTimelineStatusAsStale

        HStack(spacing: 5) {
            Text(session.spokenStatusLabel())
                .font(.caption.weight(.medium))
                .foregroundStyle(signal.statusColor)
                .lineLimit(1)
            if sessionStale {
                Text("· stale")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Ember.flame)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(runtimeBadgeAccessibilityLabel(for: session, stale: sessionStale))
    }
}

/// Slim status strip for sustained host updates and connection faults.
/// Updating claims are calm and passive; pull to refresh remains the existing
/// retry path for faults.
struct ConnectionStatusStrip: View {
    let banner: TimelineConnectivityBanner

    var body: some View {
        if let style = style(for: banner) {
            HStack(spacing: 8) {
                if let symbol = style.symbol {
                    Image(systemName: symbol)
                        .font(.caption2.weight(.semibold))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(style.label)
                        .font(.caption.weight(.semibold))
                    if let detail = style.detail {
                        Text(detail)
                            .font(.caption2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .foregroundStyle(style.foreground)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(style.background, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(style.accessibilityLabel)
        }
    }

    private struct Style {
        let label: String
        let symbol: String?
        let foreground: Color
        let background: Color
        var detail: String? = nil

        var accessibilityLabel: String {
            detail.map { "\(label), \($0)" } ?? label
        }
    }

    private func style(for banner: TimelineConnectivityBanner) -> Style? {
        switch banner {
        case .none:
            return nil
        case .updating:
            return Style(
                label: HostLinkCopy.updatingHeadline,
                symbol: "arrow.triangle.2.circlepath",
                foreground: Ember.textSecondary,
                background: Ember.raised.opacity(0.75),
                detail: HostLinkCopy.updatingDetail
            )
        case .slowUpdate(let elapsed):
            return Style(
                label: HostLinkCopy.slowUpdateHeadline,
                symbol: "hourglass",
                foreground: Ember.signalAttentionText,
                background: Ember.signalAttention.opacity(0.12),
                detail: "\(elapsed) elapsed"
            )
        case .degraded:
            return Style(label: "Connection degraded", symbol: "exclamationmark.triangle",
                         foreground: Ember.flame,
                         background: Ember.flame.opacity(0.12))
        case .offline:
            return Style(label: "Offline", symbol: "exclamationmark.triangle.fill",
                         foreground: Ember.ember,
                         background: Ember.ember.opacity(0.12))
        case .authRequired:
            return Style(label: "Sign in required", symbol: "person.crop.circle.badge.exclamationmark",
                         foreground: Ember.ember,
                         background: Ember.ember.opacity(0.12))
        }
    }
}


private struct LivenessDot: View {
    let color: Color
    let pulsing: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var animate = false

    var body: some View {
        let shouldPulse = pulsing && !reduceMotion

        ZStack {
            if shouldPulse {
                Circle()
                    .stroke(color, lineWidth: 1.4)
                    .scaleEffect(animate ? 2.0 : 1.0)
                    .opacity(animate ? 0.0 : 0.55)
                    .frame(width: 8, height: 8)
                    .animation(.easeOut(duration: 1.2).repeatForever(autoreverses: false), value: animate)
            }
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
        }
        .frame(width: 12, height: 12)
        // Drive `animate` from the `pulsing` prop directly so LazyVStack
        // recycling (which can swap pulsing on without firing onAppear)
        // still kicks the animation back on.
        .onAppear { animate = shouldPulse }
        .onChange(of: shouldPulse) { _, isPulsing in
            animate = isPulsing
        }
    }
}



private func nonEmpty(_ value: String?) -> String? {
    guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
        return nil
    }
    return trimmed
}

// TimelineSignal lives in shared session models so the app card and the
// home-screen widget share one definition. Use TimelineSignal.resolve.

private func relativeTime(_ value: String?) -> String {
    guard let date = parseLonghouseDate(value) else { return "Recent" }
    let formatter = RelativeDateTimeFormatter()
    formatter.unitsStyle = .abbreviated
    return formatter.localizedString(for: date, relativeTo: Date())
}

private func newResultOutcomeLabel(for session: SessionSummary) -> String {
    switch session.stateFacts.lastResultOutcome?.lowercased() {
    case "failed": return "Failed"
    case "cancelled": return "Cancelled"
    default: return "Finished"
    }
}

private func newResultStatusText(for session: SessionSummary) -> String {
    let outcome = newResultOutcomeLabel(for: session)
    guard let date = parseLonghouseDate(session.stateFacts.lastResultAt) else { return outcome }
    return "\(outcome) · \(compactDuration(since: date)) ago"
}

private func newResultStatusColor(for session: SessionSummary) -> Color {
    switch session.stateFacts.lastResultOutcome?.lowercased() {
    case "failed": return Ember.ember
    case "cancelled": return Ember.textSecondary
    default: return Ember.sage
    }
}

private func rowAccessibilityLabel(
    session: SessionSummary,
    role: TimelineRowRole
) -> String {
    if role == .newResult {
        return "\(session.title), new result, \(newResultStatusText(for: session))"
    }
    if role == .needsYou {
        return "\(session.title), needs you, \(session.spokenStatusLabel())"
    }
    return "\(session.title), \(session.spokenStatusLabel())"
}

private func highlightedSnippet(_ value: String, query: String) -> AttributedString {
    var attributed = AttributedString(value)
    let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !needle.isEmpty else { return attributed }

    var searchStart = value.startIndex
    while searchStart < value.endIndex,
          let match = value.range(
              of: needle,
              options: [.caseInsensitive, .diacriticInsensitive],
              range: searchStart..<value.endIndex
          ) {
        if let lower = AttributedString.Index(match.lowerBound, within: attributed),
           let upper = AttributedString.Index(match.upperBound, within: attributed) {
            attributed[lower..<upper].foregroundColor = .primary
            attributed[lower..<upper].backgroundColor = Color.accentColor.opacity(0.28)
        }
        searchStart = match.upperBound
    }
    return attributed
}

private func parseLonghouseDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    return LonghouseDateParser.parse(value)
}

// MARK: - Liveness + duration helpers (RuntimeBadge)

/// "How long in current state" — the headline number in the pill.
/// Uses `timelineAnchor`, which the backend re-anchors on phase changes
/// and progress signals (server/zerg/services/session_runtime.py).
/// Returns nil for closed sessions (we don't want to show a counter there).
func stateDurationLabel(for session: SessionSummary) -> String? {
    // Use the lifecycle flag, the same "closed" source the signal uses, so the
    // dot/accent and the duration never disagree about whether a row is closed.
    if session.isClosed { return nil }
    guard let date = parseLonghouseDate(session.timelineAnchor) else { return nil }
    return compactDuration(since: date)
}

private func runtimeBadgeAccessibilityLabel(for session: SessionSummary, stale: Bool) -> String {
    var parts = [session.spokenStatusLabel()]
    if let duration = stateDurationLabel(for: session) {
        parts.append(duration)
    }
    if stale {
        parts.append("stale")
    }
    return parts.joined(separator: ", ")
}

/// Compact, no-"ago" duration: "5s", "12s", "3m", "1h", "2d".
func compactDuration(since date: Date) -> String {
    let interval = max(0, Date().timeIntervalSince(date))
    let seconds = Int(interval)
    if seconds < 60 { return "\(seconds)s" }
    let minutes = seconds / 60
    if minutes < 60 { return "\(minutes)m" }
    let hours = minutes / 60
    if hours < 24 { return "\(hours)h" }
    let days = hours / 24
    return "\(days)d"
}
