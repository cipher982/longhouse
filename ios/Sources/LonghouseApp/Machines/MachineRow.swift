import SwiftUI
func machineProviderGlyphVariant(for provider: String?) -> ProviderGlyph.Variant {
    ProviderBrands.lookup(provider).glyphStyle == "template" ? .chip : .bare
}


struct MachineRow: View {
    let machine: MachineDirectoryEntry
    let activity: MachineActivity?
    let sync: MachineSync?
    var showsChevron = true
    var showsCheckmark = false

    private var status: MachineStatus {
        deriveMachineStatus(machine: machine, activity: activity, sync: sync)
    }

    private var providers: [String] {
        var values = machine.launch.providers.map(\.provider)
        values.append(contentsOf: machine.launch.unavailableProviders.map(\.provider))
        var seen = Set<String>()
        return values.filter { seen.insert($0).inserted }
    }

    var body: some View {
        rowContent
            .accessibilityIdentifier("machine-row-\(machine.deviceId)")
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(machine.machineName), \(status.text)")
    }

    private var rowContent: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(status.role.dotColor)
                .frame(width: 9, height: 9)
                .overlay {
                    if status.role == .off {
                        Circle().stroke(status.role.dotColor, lineWidth: 1.5)
                            .background(Circle().fill(Ember.card))
                    }
                }
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                Text(machine.machineName)
                    .font(.body)
                    .foregroundStyle(Ember.text)
                    .lineLimit(1)
                Text(status.text)
                    .font(.subheadline)
                    .foregroundStyle(status.role.textColor)
                    .lineLimit(1)
                if let detail = status.detail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(status.role.textColor.opacity(0.9))
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 8)
            if !providers.isEmpty {
                HStack(spacing: 3) {
                    ForEach(Array(providers.prefix(3)), id: \.self) { provider in
                        ProviderGlyph(
                            provider: provider,
                            size: 18,
                            variant: machineProviderGlyphVariant(for: provider)
                        )
                    }
                    if providers.count > 3 {
                        Text("+\(providers.count - 3)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Ember.textMuted)
                    }
                }
                .accessibilityHidden(true)
            }
            if showsCheckmark {
                Image(systemName: "checkmark")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Ember.signalLiveText)
                    .accessibilityHidden(true)
            }
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Ember.textMuted)
                    .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }
}
