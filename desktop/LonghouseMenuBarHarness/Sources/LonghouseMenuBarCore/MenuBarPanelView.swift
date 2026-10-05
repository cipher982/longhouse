import SwiftUI

public enum MenuBarPanelLayout {
    public static let panelWidth: CGFloat = 360
    public static let defaultWindowHeight: CGFloat = 480
    public static let maximumWindowHeight: CGFloat = 760
    public static let chromeCornerRadius: CGFloat = 18
    public static let chromePadding: CGFloat = 12
    public static let rootSpacing: CGFloat = 10
    /// Session rows scroll inside this height so actions below stay on screen.
    public static let sessionAreaMaximumHeight: CGFloat = 300
    /// The whole body below the header scrolls past this, so the panel never
    /// exceeds `maximumWindowHeight` (760 = 12+12 padding, ~56 header, 10 gap,
    /// ~30 feedback headroom, 640 body).
    public static let bodyMaximumHeight: CGFloat = 640
}

/// One shape for every panel state that is not a snapshot: loading, booting,
/// catching up, failure. A glyph, a title, one sentence.
private struct PanelStatusView<Accessory: View>: View {
    let title: String
    let detail: String
    let tint: Color
    let systemImage: String?
    @ViewBuilder let accessory: () -> Accessory

    var body: some View {
        PanelChrome {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        Circle()
                            .fill(tint.opacity(0.14))
                            .frame(width: 32, height: 32)
                        if let systemImage {
                            Image(systemName: systemImage)
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(tint)
                        } else {
                            ProgressView()
                                .controlSize(.small)
                        }
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(Color.primary)
                        Text(detail)
                            .font(.system(size: 11))
                            .foregroundStyle(Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                accessory()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
        }
    }
}

public struct MenuBarLoadingView: View {
    public init() {}

    public var body: some View {
        PanelStatusView(
            title: "Refreshing Longhouse",
            detail: "Collecting the latest status for this Mac.",
            tint: .gray,
            systemImage: nil
        ) { EmptyView() }
    }
}

public struct MenuBarBootingView: View {
    public init() {}

    public var body: some View {
        PanelStatusView(
            title: "Starting Longhouse",
            detail: "Bringing up the local engine. This usually takes a few seconds on first launch.",
            tint: .blue,
            systemImage: nil
        ) { EmptyView() }
    }
}

public struct MenuBarSettlingView: View {
    public init() {}

    public var body: some View {
        PanelStatusView(
            title: "Catching up",
            detail: "The local engine is refreshing after an idle gap. Warnings appear if status keeps aging.",
            tint: .blue,
            systemImage: nil
        ) { EmptyView() }
    }
}

public struct MenuBarFailureView: View {
    private let message: String
    private let retry: () -> Void

    public init(message: String, retry: @escaping () -> Void) {
        self.message = message
        self.retry = retry
    }

    public var body: some View {
        PanelStatusView(
            title: "Longhouse status unavailable",
            detail: "Longhouse.app could not load its latest status.",
            tint: .red,
            systemImage: "xmark.circle.fill"
        ) {
            VStack(alignment: .leading, spacing: 10) {
                Text(message)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.message)

                Button(action: retry) {
                    Label("Retry", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.retryButton)
                .accessibilityLabel(Text("Retry"))
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Error.headline)
    }
}

/// The menu bar panel ("Hearth", `macos-menu-bar-state-model.md`). Order:
/// header, stale banner, a repair card, the focus card for the session that
/// needs the user, live sessions as fires, quiet sessions folded into one
/// line, then system health as one line (or a card when something is wrong).
public struct MenuBarPanelView: View {
    private let snapshot: HealthSnapshot
    private let history: [SnapshotHistorySample]
    private let presentationDate: Date
    private let feedback: HealthActionFeedback?
    private let setFeedback: (HealthActionFeedback?) -> Void
    private let actionSink: any HealthActionSink
    private let isManualRefreshing: Bool
    private let refresh: () -> Void
    private let dataTrust: DataTrust
    private let projectionTrust: DataTrust

    /// nil follows the default for the current contents.
    @State private var quietExpanded: Bool?
    @State private var unmanagedExpanded = false
    @State private var factsExpanded = false

    public init(
        snapshot: HealthSnapshot,
        history: [SnapshotHistorySample],
        presentationDate: Date,
        feedback: HealthActionFeedback?,
        setFeedback: @escaping (HealthActionFeedback?) -> Void,
        actionSink: any HealthActionSink,
        isManualRefreshing: Bool,
        dataTrust: DataTrust = .current,
        projectionTrust: DataTrust = .current,
        refresh: @escaping () -> Void
    ) {
        self.snapshot = snapshot
        self.history = history
        self.presentationDate = presentationDate
        self.feedback = feedback
        self.setFeedback = setFeedback
        self.actionSink = actionSink
        self.isManualRefreshing = isManualRefreshing
        self.dataTrust = dataTrust
        self.projectionTrust = projectionTrust
        self.refresh = refresh
    }

    public var body: some View {
        let presentation = self.presentation
        let entries = hearthEntries
        let focus = entries.first { $0.kind.asksForUser }
        let rows = entries.filter { $0.kind != .quiet && $0.id != focus?.id }
        let quiet = entries.filter { $0.kind == .quiet }
        let repairFirst = showsTroubleCard && (presentation.promotion == .repair || snapshot.isSetupRequired)

        PanelChrome {
            VStack(alignment: .leading, spacing: MenuBarPanelLayout.rootSpacing) {
                header(presentation)

                // Everything between the header and the feedback banner scrolls
                // as one body once it outgrows the window, so a long trouble
                // card or expanded facts can never push actions off screen.
                ScrollView(.vertical) {
                    VStack(alignment: .leading, spacing: MenuBarPanelLayout.rootSpacing) {
                        // Above everything it qualifies: below it is last-known.
                        if !dataTrust.isCurrent {
                            staleBanner
                        }

                        if repairFirst {
                            troubleCard(presentation)
                        }

                        if let focus {
                            HearthFocusCard(entry: focus)
                        }

                        if snapshot.sessionDiscoveryAttention && !snapshot.isSetupRequired {
                            notice(
                                snapshot.sessionDiscoveryWarningDetail
                                    ?? "Session discovery is incomplete; active sessions may be missing from this list.",
                                identifier: "longhouse.session-discovery-warning"
                            )
                        }

                        // A never-connected Mac has no agent to describe. Any
                        // session evidence it does have still shows.
                        if showsRuntimeSurface {
                            sessionArea(rows: rows, quiet: quiet, hasFocus: focus != nil)
                        }

                        if showsTroubleCard && !repairFirst {
                            troubleCard(presentation)
                        } else if !showsTroubleCard && !snapshot.isSetupRequired {
                            healthLine(presentation)
                        }

                        if let backgroundActivity = presentation.backgroundActivity {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Image(systemName: "clock.arrow.circlepath")
                                Text("\(backgroundActivity) · current sessions have priority")
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .font(.system(size: 11))
                            .foregroundStyle(Color.secondary)
                            .padding(.horizontal, 4)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
                .frame(maxHeight: MenuBarPanelLayout.bodyMaximumHeight)

                if let feedback {
                    feedbackBanner(feedback)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.panel)
    }

    private var presentation: MenuBarPresentation {
        snapshot.menuBarPresentation(
            relativeTo: presentationDate,
            localEvidenceTrust: dataTrust,
            projectionTrust: projectionTrust
        )
    }

    // MARK: Header

    private func header(_ presentation: MenuBarPresentation) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 10) {
                longhouseBrandEmblem(severity: staleSeverity ?? presentation.promotion.iconSeverity)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Header.statusGlyph)

                // Headline only: the rows below are the breakdown, and a count
                // line repeated them in a second, wrapping voice.
                Text(presentation.headline)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .harnessAccessibility(
                        identifier: LonghouseMenuBarAccessibilityID.Header.headline,
                        label: presentation.headline
                    )

                Spacer(minLength: 4)

                HStack(spacing: 2) {
                    headerButton(
                        systemImage: "arrow.up.forward.app",
                        identifier: LonghouseMenuBarAccessibilityID.Button.openLonghouse,
                        label: "Open Longhouse"
                    ) { perform(.openLonghouse) }
                    refreshControl
                    toolsMenu
                }
            }

            let chips = [snapshot.updateAvailableChipLabel, snapshot.restartPendingChipLabel].compactMap { $0 }
            if !chips.isEmpty {
                HStack(spacing: 6) {
                    ForEach(chips, id: \.self) { chip in
                        Text(chip)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(Color.secondary)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Capsule(style: .continuous).fill(Color.primary.opacity(0.07)))
                    }
                }
                .padding(.leading, 40)
            }
        }
        .padding(.horizontal, 2)
    }

    private var staleSeverity: HarnessSeverity? {
        switch dataTrust {
        case .current: return nil
        case .lastKnown: return .yellow
        case .neverLoaded: return .red
        }
    }

    private func headerButton(
        systemImage: String,
        identifier: String,
        label: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            headerGlyph(systemImage)
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(Text(label))
    }

    private func headerGlyph(_ systemImage: String) -> some View {
        Image(systemName: systemImage)
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(Color.secondary)
            .frame(width: 24, height: 24)
            .contentShape(Rectangle())
    }

    private var refreshControl: some View {
        Button {
            perform(.refresh)
        } label: {
            if isManualRefreshing {
                ProgressView()
                    .controlSize(.mini)
                    .frame(width: 24, height: 24)
            } else {
                headerGlyph("arrow.clockwise")
            }
        }
        .buttonStyle(.plain)
        .help("Refresh")
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.refresh)
        .accessibilityLabel(Text(isManualRefreshing ? "Refreshing" : "Refresh"))
    }

    private var toolsMenu: some View {
        Menu {
            Button("Doctor") {
                setFeedback(actionSink.handle(.runDoctor, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.doctor)

            Button("Logs") {
                setFeedback(actionSink.handle(.openLogs, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)

            Button("Copy JSON") {
                setFeedback(actionSink.handle(.copyDiagnostics, snapshot: snapshot))
            }
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.copyDiagnostics)

            Divider()

            if snapshot.hasResolvedPythonPackageVersion {
                Text("Python package \(snapshot.pythonPackageVersionLabel)")
            }
            Text("Native pair \(snapshot.nativePairVersionLabel)")
            Text("Running engine \(snapshot.runningEngineVersionLabel)")

            Divider()

            Button("Quit Longhouse") {
                _ = actionSink.handle(.quitApp, snapshot: snapshot)
            }
        } label: {
            headerGlyph("ellipsis")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("More")
    }

    // MARK: Stale banner

    private var staleBannerHeadline: String {
        switch dataTrust {
        case .current:
            return ""
        case let .lastKnown(context):
            guard let age = context.age(relativeTo: presentationDate) else {
                return "Showing last known status"
            }
            return "Showing status from \(SnapshotAgeFormatter.compact(age)) ago"
        case .neverLoaded:
            return "Longhouse cannot read this Mac's status"
        }
    }

    private var staleBannerDetail: String {
        if let failure = dataTrust.failure {
            return failure.message
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: "\n", with: " ")
        }
        return "The status command has not completed recently."
    }

    private var staleTint: Color {
        if case .neverLoaded = dataTrust { return HearthPalette.fault }
        return HearthPalette.warning
    }

    private var staleBanner: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(staleTint)
                Text(staleBannerHeadline)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.StaleBanner.headline)
            }

            Text(staleBannerDetail)
                .font(.system(size: 11))
                .foregroundStyle(Color.secondary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.StaleBanner.detail)

            if let command = dataTrust.failure?.command {
                Text(command)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(Color.secondary.opacity(0.85))
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.StaleBanner.command)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(staleTint.opacity(0.12))
        )
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.StaleBanner.container)
    }

    private func notice(_ text: String, identifier: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(HearthPalette.warning)
            Text(text)
                .foregroundStyle(Color.primary.opacity(0.85))
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: 11))
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(identifier)
    }

    // MARK: Sessions

    private var showsRuntimeSurface: Bool {
        !snapshot.isSetupRequired
            || !(snapshot.managedSessions ?? []).isEmpty
            || !unmanagedActivityEntries.isEmpty
    }

    /// Why the list is empty, when it is. Absent evidence is never shown as
    /// an observed absence.
    private var emptySessionsText: String? {
        guard snapshot.currentManagedSessions.isEmpty else { return nil }
        if !dataTrust.isCurrent {
            return "Current session evidence is unavailable on this Mac."
        }
        if snapshot.managedSessions == nil {
            return "Session evidence is unavailable on this Mac."
        }
        if snapshot.sessionDiscoveryAttention {
            return nil
        }
        return "No managed sessions are running on this Mac."
    }

    private func sessionArea(rows: [HearthSessionEntry], quiet: [HearthSessionEntry], hasFocus: Bool) -> some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(rows) { HearthSessionRow(entry: $0) }

                if !quiet.isEmpty {
                    HearthFoldLine(
                        providers: quiet.map(\.provider),
                        title: quietTitle(quiet),
                        detail: quiet.map(\.title).joined(separator: ", "),
                        expanded: Binding(
                            get: { quietExpanded ?? (rows.isEmpty && !hasFocus && quiet.count <= 4) },
                            set: { quietExpanded = $0 }
                        ),
                        identifier: LonghouseMenuBarAccessibilityID.Hearth.quietSessions
                    ) {
                        VStack(spacing: 0) {
                            ForEach(quiet) { HearthQuietRow(entry: $0) }
                        }
                    }
                }

                if let emptySessionsText, rows.isEmpty, !hasFocus {
                    Text(emptySessionsText)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                }

                let unmanaged = unmanagedActivityEntries
                if !unmanaged.isEmpty {
                    HearthFoldLine(
                        providers: unmanaged.map(\.provider),
                        title: "\(unmanaged.count) not managed",
                        detail: unmanaged.map(\.title).joined(separator: ", "),
                        expanded: $unmanagedExpanded,
                        identifier: LonghouseMenuBarAccessibilityID.Hearth.unmanagedAgents
                    ) {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(unmanaged) { unmanagedRow($0) }
                            Text("Observed provider CLIs Longhouse did not launch. Start them from Longhouse to steer them here.")
                                .font(.system(size: 10))
                                .foregroundStyle(Color.secondary.opacity(0.8))
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.leading, 36)
                                .padding(.top, 2)
                        }
                    }
                }

                if !backgroundBridgeEntries.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Background processes to clean up")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Color.secondary)
                            .padding(.top, 6)
                        BackgroundBridgeList(
                            entries: backgroundBridgeEntries,
                            bulkStopAction: backgroundBridgeStopAllAction(),
                            bulkStopTargetCount: backgroundBridgeBulkStopTargets.count
                        )
                    }
                    .padding(.horizontal, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(maxHeight: MenuBarPanelLayout.sessionAreaMaximumHeight)
    }

    private func quietTitle(_ quiet: [HearthSessionEntry]) -> String {
        let quietIDs = Set(quiet.map(\.id))
        let noStatus = snapshot.currentManagedSessions
            .filter { quietIDs.contains($0.id) && $0.menuBarAttentionKind == .phaseUnavailable }
            .count
        return noStatus == quiet.count ? "\(quiet.count) without live status" : "\(quiet.count) quiet"
    }

    private func unmanagedRow(_ entry: UnmanagedActivityEntry) -> some View {
        HStack(spacing: 8) {
            ProviderGlyph(provider: entry.provider, size: 13, variant: .bare)
                .frame(width: 26)
            Text(entry.branch.map { "\(entry.title) / \($0)" } ?? entry.title)
                .font(.system(size: 12))
                .foregroundStyle(Color.primary.opacity(0.85))
                .lineLimit(1)
            Spacer(minLength: 6)
            Text(entry.age)
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(Color.secondary)
                .padding(.trailing, 12)
        }
        .padding(.leading, 2)
        .padding(.vertical, 4)
    }

    private var hearthEntries: [HearthSessionEntry] {
        let ranked: [(offset: Int, session: ManagedSessionSnapshot, kind: HearthSessionKind)] =
            snapshot.currentManagedSessions.enumerated().map { ($0.offset, $0.element, $0.element.hearthKind) }
        let sorted = ranked.sorted { lhs, rhs in
            lhs.kind == rhs.kind ? lhs.offset < rhs.offset : lhs.kind < rhs.kind
        }
        return sorted.map { hearthEntry(for: $0.session, kind: $0.kind) }
    }

    private func hearthEntry(for session: ManagedSessionSnapshot, kind: HearthSessionKind) -> HearthSessionEntry {
        let provider = (session.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return HearthSessionEntry(
            id: session.id,
            provider: provider.isEmpty ? "unknown" : provider,
            title: managedSessionTitle(session),
            kind: kind,
            heat: session.hearthHeat,
            subtitle: hearthSubtitle(session, kind: kind),
            ageLabel: snapshot.compactTimestampLabel(session.lastActivityAt, relativeTo: presentationDate),
            seed: Self.stableSeed(session.id),
            openAction: managedOpenAction(for: session),
            stopAction: managedStopAction(for: session)
        )
    }

    /// FNV-1a: flames keep their shape across launches and fixture renders.
    private static func stableSeed(_ value: String) -> Int {
        var hash: UInt32 = 2_166_136_261
        for byte in value.utf8 {
            hash = (hash ^ UInt32(byte)) &* 16_777_619
        }
        return Int(hash % 997)
    }

    private func hearthSubtitle(_ session: ManagedSessionSnapshot, kind: HearthSessionKind) -> String {
        let workspace = managedSessionWorkspaceContext(session)
        let label = session.presentation?.primary?.label.trimmingCharacters(in: .whitespacesAndNewlines)
        switch kind {
        case .needsYou:
            switch session.presentation?.primary?.key {
            case "needs_answer": return "Waiting for your answer"
            case "needs_approval": return "Waiting for your approval"
            default:
                let phase = session.phase?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                return phase == "needs permission" ? "Waiting for your approval" : "Waiting for you"
            }
        case .blocked:
            return compactDetailParts([label?.isEmpty == false ? label! : "Blocked", workspace])
        case .working:
            let tool = session.activity?.tool?.trimmingCharacters(in: .whitespacesAndNewlines)
            return compactDetailParts([workspace, tool?.isEmpty == false ? tool! : (label ?? "Working")])
        case .lostControl:
            return managedSessionDetail(session)
        case .quiet:
            if case .unknown = session.menuBarAttentionKind {
                return managedSessionDetail(session)
            }
            return workspace
        }
    }

    /// Live provider CLIs Longhouse does not own on this Mac right now.
    /// This is explicit process truth, not recent transcript activity.
    private var unmanagedActivityEntries: [UnmanagedActivityEntry] {
        snapshot.currentUnmanagedProcesses.map { process in
            let workspace = (process.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let provider = (process.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return UnmanagedActivityEntry(
                id: process.id,
                provider: provider.isEmpty ? "unknown" : provider,
                title: workspace.isEmpty ? HealthSnapshot.providerDisplayName(provider.isEmpty ? "unknown" : provider) : workspace,
                branch: (process.branch ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : process.branch,
                age: snapshot.compactTimestampLabel(process.startedAt, relativeTo: presentationDate)
            )
        }
    }

    private var backgroundBridgeEntries: [BackgroundBridgeEntry] {
        snapshot.currentOrphanBridges.map { bridge in
            let workspace = (bridge.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let provider = (bridge.provider ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let status = (bridge.status ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

            return BackgroundBridgeEntry(
                id: bridge.id,
                sessionID: bridge.sessionId,
                provider: provider.isEmpty ? "unknown" : provider,
                workspace: workspace.isEmpty ? "Detached workspace" : workspace,
                statusLabel: status.isEmpty ? "orphan" : status,
                ageLabel: snapshot.compactTimestampLabel(bridge.heartbeatAt ?? bridge.startedAt, relativeTo: presentationDate),
                detail: orphanBridgeDetail(bridge),
                stopAction: orphanBridgeStopAction(for: bridge)
            )
        }
    }

    private var backgroundBridgeBulkStopTargets: [ManagedStopTarget] {
        backgroundBridgeEntries.compactMap { entry -> ManagedStopTarget? in
            guard entry.stopAction != nil,
                  let sessionID = entry.sessionID?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !sessionID.isEmpty
            else {
                return nil
            }
            return ManagedStopTarget(sessionID: sessionID, provider: entry.provider)
        }
    }

    private func managedStopAction(for session: ManagedSessionSnapshot) -> (() -> Void)? {
        guard session.canStopFromMenuBar,
              let sessionID = session.sessionId,
              !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }

        let workspace = session.workspaceLabel
        let provider = session.provider
        return {
            setFeedback(
                actionSink.handleStopManagedBridge(
                    sessionID: sessionID,
                    provider: provider,
                    workspaceLabel: workspace,
                    snapshot: snapshot
                )
            )
        }
    }

    private func managedOpenAction(for session: ManagedSessionSnapshot) -> (() -> Void)? {
        guard let sessionID = session.sessionId?.trimmingCharacters(in: .whitespacesAndNewlines),
              !sessionID.isEmpty
        else {
            return nil
        }

        let title = managedSessionTitle(session)
        return {
            setFeedback(
                actionSink.handleOpenManagedSession(
                    sessionID: sessionID,
                    title: title,
                    snapshot: snapshot
                )
            )
        }
    }

    private func orphanBridgeStopAction(for bridge: OrphanBridgeSnapshot) -> (() -> Void)? {
        guard let sessionID = bridge.sessionId,
              !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }

        let workspace = bridge.workspaceLabel
        let provider = bridge.provider
        return {
            setFeedback(
                actionSink.handleStopManagedBridge(
                    sessionID: sessionID,
                    provider: provider,
                    workspaceLabel: workspace,
                    snapshot: snapshot
                )
            )
        }
    }

    private func backgroundBridgeStopAllAction() -> (() -> Void)? {
        let targets = backgroundBridgeBulkStopTargets
        guard !targets.isEmpty else {
            return nil
        }

        return {
            setFeedback(
                actionSink.handleStopManagedBridges(
                    targets: targets,
                    label: "detached bridges",
                    snapshot: snapshot
                )
            )
        }
    }

    /// Workspace/branch context plus control-path detail when it is useful.
    private func managedSessionDetail(_ session: ManagedSessionSnapshot) -> String {
        let workspaceContext = managedSessionWorkspaceContext(session)
        if session.normalizedState == "attached",
           case .unknown = session.menuBarAttentionKind {
            if let rawPhase = session.rawPhase?.trimmingCharacters(in: .whitespacesAndNewlines),
               !rawPhase.isEmpty {
                return compactDetailParts([workspaceContext, "Unexpected local phase: \(rawPhase)"])
            }
            if let phase = session.phase?.trimmingCharacters(in: .whitespacesAndNewlines),
               !phase.isEmpty {
                return compactDetailParts([workspaceContext, "Unexpected local phase label: \(phase)"])
            }
            return compactDetailParts([workspaceContext, "Longhouse cannot classify this managed phase yet"])
        }

        switch session.normalizedState {
        case "detached":
            return compactDetailParts([workspaceContext, "Terminal control detached"])
        case "degraded":
            let reasons = (session.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
            if reasons.isEmpty {
                return compactDetailParts([workspaceContext, "Control path degraded"])
            }
            return compactDetailParts([workspaceContext] + reasons)
        case "attached":
            return workspaceContext
        case "unknown":
            return compactDetailParts([workspaceContext, "Longhouse cannot classify this managed session yet"])
        default:
            let reasons = (session.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
            if !reasons.isEmpty {
                return compactDetailParts([workspaceContext] + reasons)
            }
            let normalized = session.normalizedState.trimmingCharacters(in: .whitespacesAndNewlines)
            if normalized.isEmpty {
                return workspaceContext
            }
            return compactDetailParts([workspaceContext, normalized.replacingOccurrences(of: "_", with: " ").capitalized])
        }
    }

    private func managedSessionTitle(_ session: ManagedSessionSnapshot) -> String {
        if let title = compactSessionText(session.resolvedTitleText, maxCharacters: 72) {
            return title
        }
        let provider = HealthSnapshot.providerDisplayName(session.provider ?? "Agent")
        let workspace = (session.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return workspace.isEmpty ? "\(provider) session" : "\(provider) session in \(workspace)"
    }

    private func managedSessionWorkspaceContext(_ session: ManagedSessionSnapshot) -> String {
        let workspace = (session.workspaceLabel ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let branch = (session.branch ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if workspace.isEmpty {
            return ""
        }
        if branch.isEmpty {
            return workspace
        }
        return "\(workspace) / \(branch)"
    }

    private func compactDetailParts(_ parts: [String]) -> String {
        parts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    private func compactSessionText(_ value: String?, maxCharacters: Int) -> String? {
        guard let value else {
            return nil
        }
        let compact = value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        guard !compact.isEmpty else {
            return nil
        }
        if compact.count <= maxCharacters {
            return compact
        }
        return String(compact.prefix(max(1, maxCharacters - 1))).trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }

    private func orphanBridgeDetail(_ bridge: OrphanBridgeSnapshot) -> String {
        var parts = (bridge.reasonCodes ?? []).prefix(2).map { HealthSnapshot.humanizeManagedReason($0) }
        if parts.isEmpty {
            parts.append("No managed session bound")
        }

        let heartbeat = snapshot.compactTimestampLabel(bridge.heartbeatAt, relativeTo: presentationDate)
        if heartbeat != "-" {
            parts.append("heartbeat \(heartbeat)")
        }

        return parts.joined(separator: " · ")
    }

    // MARK: Health

    /// The freshness fact is derived from evidence freshness rather than the
    /// engine liveness pulse, so an unavailable producer cannot render
    /// "Fresh · 21s" under a banner saying the status cannot be read. A health
    /// source that does not scan for orphaned bridges says so here: an empty
    /// cleanup list is not evidence of a clean machine.
    var displayedFacts: [MenuBarSystemFact] {
        var facts = presentation.facts
        if !dataTrust.isCurrent {
            facts = facts.map { fact in
                guard fact.id == "freshness" else { return fact }
                return MenuBarSystemFact(
                    id: fact.id,
                    label: fact.label,
                    value: "Unknown",
                    detail: "status command is not running",
                    promotion: .unavailable
                )
            }
        }
        if backgroundBridgeEntries.isEmpty && (snapshot.orphanBridgeEvidenceMissing || !dataTrust.isCurrent) {
            facts.append(MenuBarSystemFact(
                id: "cleanup-scan",
                label: "Cleanup scan",
                value: "Not reported",
                detail: "this health source does not scan for orphaned bridges",
                promotion: .unavailable
            ))
        }
        return facts
    }

    private var shouldOfferNativeRepair: Bool {
        guard !snapshot.isSetupRequired, !snapshot.isInstallLocationBlocked else {
            return false
        }
        // A failed or timed-out status command proves only that the producer
        // could not refresh, not that the installed agent needs repair; stale
        // snapshots can still contain an old repair suggestion.
        guard dataTrust.isCurrent else {
            return false
        }
        return snapshot.suggestedActionIds?.contains("repair_machine") == true
            && presentation.promotion == .repair
    }

    /// Current local evidence says the engine service is not running. That is
    /// its own repair, whatever the Runtime Host view or engine staleness say.
    private var serviceStopped: Bool {
        !snapshot.isSetupRequired
            && dataTrust.isCurrent
            && snapshot.service != nil
            && snapshot.serviceStatusLabel != "running"
    }

    private var shouldRetryLocalStatus: Bool {
        guard !snapshot.isSetupRequired else { return false }
        if !dataTrust.isCurrent { return true }
        // A stopped service explains its own stale engine evidence: retrying
        // the status read cannot help, repairing the service can.
        if serviceStopped { return false }
        return snapshot.engineStatus?.fresh == false
            || snapshot.reasons.contains("engine_status_stale")
            || snapshot.reasons.contains("engine_projection_stale")
    }

    /// Whether the machine needs a card with an action rather than one line.
    var showsTroubleCard: Bool {
        presentation.promotion == .repair
            || shouldOfferNativeRepair
            || !dataTrust.isCurrent
            || !projectionTrust.isCurrent
            || shouldRetryLocalStatus
            || snapshot.storageBlockProofUnknown
            || snapshot.suggestedActionIds?.contains("inspect_storage_source") == true
            || snapshot.suggestedActionIds?.contains("inspect_transport") == true
            || snapshot.suggestedActionIds?.contains("inspect_shipping") == true
    }

    private var troubleSeverity: MenuBarPromotion {
        if snapshot.isSetupRequired { return .needsUser }
        if case .neverLoaded = dataTrust { return .repair }
        if !dataTrust.isCurrent { return .inspect }
        switch presentation.promotion {
        case .repair, .unavailable, .inspect: return presentation.promotion
        case .normal, .needsUser: return .inspect
        }
    }

    private var troubleTint: Color {
        switch troubleSeverity {
        case .repair: return HearthPalette.fault
        case .unavailable: return Color.secondary
        case .needsUser: return .accentColor
        case .inspect, .normal: return HearthPalette.warning
        }
    }

    /// The health line's sentence and how wrong it is (nil = all normal).
    /// Read from the facts, never from session attention: a session waiting
    /// on the user must not make an offline transport read connected, and a
    /// configured address is not connection evidence. A system warning with
    /// no matching fact (provider release blocked, launch recovery, archive
    /// review) still surfaces as the reducer's headline, never as green.
    var healthSummary: (text: String, promotion: MenuBarPromotion?) {
        let considered = displayedFacts.filter { $0.id != "cleanup-scan" }
        let presentation = self.presentation
        let system = presentation.systemPromotion
        // Order: a fact that is wrong, then the machine's own warning (never
        // the session-aware promotion), then a fact that is merely unknown.
        if let wrong = considered.first(where: { $0.promotion == .repair || $0.promotion == .inspect }) {
            return ("\(wrong.label): \(wrong.value)", wrong.promotion)
        }
        if system == .repair || system == .inspect {
            return (presentation.systemHeadline, system)
        }
        if let unknown = considered.first(where: { $0.promotion == .unavailable }) {
            return ("\(unknown.label): \(unknown.value)", .unavailable)
        }
        if system == .unavailable {
            return (presentation.systemHeadline, .unavailable)
        }
        if snapshot.hostValueLabel != "-" {
            return ("Connected to \(snapshot.hostValueLabel)", nil)
        }
        return ("Local agent running", nil)
    }

    /// One line while nothing needs a repair; tap for the per-plane facts.
    private func healthLine(_ presentation: MenuBarPresentation) -> some View {
        let facts = displayedFacts
        let (summary, promotion) = healthSummary
        let dot = promotion?.factColor ?? HearthPalette.ok

        return VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.6)
            Button {
                withAnimation(.snappy(duration: 0.22)) { factsExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Circle().fill(dot).frame(width: 6, height: 6)
                    Text(summary)
                        .foregroundStyle(Color.secondary)
                        .lineLimit(1)
                    if dataTrust.isCurrent {
                        Text("· updated \(snapshot.snapshotAgeCompactLabel(relativeTo: presentationDate)) ago")
                            .foregroundStyle(Color.secondary.opacity(0.7))
                            .lineLimit(1)
                            .layoutPriority(-1)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(Color.secondary.opacity(0.6))
                        .rotationEffect(.degrees(factsExpanded ? 90 : 0))
                }
                .font(.system(size: 11))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Hearth.healthLine)

            if factsExpanded {
                HearthFactsList(facts: facts)
                    .padding(.leading, 12)
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, 4)
    }

    /// Something is wrong with the machine: what, what to do, and the facts.
    private func troubleCard(_ presentation: MenuBarPresentation) -> some View {
        let tint = troubleTint
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: snapshot.isSetupRequired ? "person.crop.circle.badge.checkmark" : troubleSeverity.troubleSymbol)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(tint)
                VStack(alignment: .leading, spacing: 3) {
                    Text(snapshot.isSetupRequired ? "Connect this Mac" : "Action required")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.primary)
                    Text(repairGuidance)
                        .font(.system(size: 11))
                        .foregroundStyle(Color.primary.opacity(0.8))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            HStack(spacing: 12) {
                primaryRepairButton
                    .buttonStyle(.borderedProminent)
                    .tint(snapshot.isSetupRequired ? .accentColor : tint)
                    .controlSize(.regular)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.repair)

                Button("Open Logs") {
                    perform(.openLogs)
                }
                .buttonStyle(.plain)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Color.secondary)
                .fixedSize()
                .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Button.openLogs)
            }

            if !snapshot.isSetupRequired {
                Divider().opacity(0.5)
                HearthFactsList(facts: displayedFacts)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(tint.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(tint.opacity(0.22), lineWidth: 1)
        )
    }

    private var repairGuidance: String {
        if snapshot.isSetupRequired {
            return "Sign in with your Longhouse address (for example https://yourname.longhouse.ai). Terminal opens, your browser asks you to approve this Mac, then Longhouse starts syncing."
        }
        if !dataTrust.isCurrent {
            return "The local status check is unavailable. Refresh to retry; stale evidence does not indicate a repair."
        }
        if serviceStopped {
            return "The local Longhouse engine is \(snapshot.serviceStatusLabel), so this Mac is not shipping. Repair restarts it."
        }
        if !projectionTrust.isCurrent {
            return "The Runtime Host session view is unavailable. The local agent and durable upload facts remain separate; refresh to retry the remote view."
        }
        if shouldRetryLocalStatus {
            return "The local agent is running, but its status evidence is stale. Refresh to retry; repair is not indicated."
        }
        if snapshot.storageBlockRequiresRepair {
            return "Local source evidence is retained. Inspect the exact block proof before retrying or discarding it."
        }
        if snapshot.isInstallLocationBlocked {
            return "Move Longhouse.app to /Applications, then reopen it."
        }
        if snapshot.suggestedActionIds?.contains("free_disk_space") == true {
            return "Free local disk space before continuing to rely on durable shipping."
        }
        if snapshot.suggestedActionIds?.contains("repair_machine") == true {
            return "Repair the configured Longhouse machine without opening Terminal."
        }
        if snapshot.suggestedActionIds?.contains("inspect_shipping") == true {
            return "Local dead letters are retained. Inspect shipping evidence before retrying; no destructive repair is required."
        }
        if snapshot.suggestedActionIds?.contains("inspect_transport") == true {
            return "Local upload progress needs inspection. Open Logs to review the transport evidence; local source data remains retained."
        }
        return "Current local evidence shows a broken product promise. Open Logs for the exact failing fact."
    }

    /// The one contextual action for the trouble card, in priority order.
    private var primaryRepairButton: Button<Label<Text, Image>> {
        let suggested = snapshot.suggestedActionIds ?? []
        let (title, systemImage, action): (String, String, () -> Void)
        if snapshot.isSetupRequired {
            (title, systemImage, action) = ("Sign in to connect this Mac", "person.crop.circle.badge.checkmark", { perform(.repairInstall) })
        } else if !dataTrust.isCurrent || shouldRetryLocalStatus {
            (title, systemImage, action) = ("Retry local status", "arrow.clockwise", { perform(.refresh) })
        } else if serviceStopped {
            (title, systemImage, action) = ("Repair local agent", "wrench.and.screwdriver", { perform(.repairInstall) })
        } else if !projectionTrust.isCurrent {
            (title, systemImage, action) = ("Retry session view", "arrow.clockwise", { perform(.refresh) })
        } else if snapshot.storageBlockRequiresRepair
                    || snapshot.storageBlockProofUnknown
                    || suggested.contains("inspect_storage_source") {
            (title, systemImage, action) = ("Inspect source evidence", "doc.text.magnifyingglass", { perform(.inspectStorageSource) })
        } else if suggested.contains("stop_managed_bridge"), let stopAll = backgroundBridgeStopAllAction() {
            (title, systemImage, action) = ("Stop orphaned processes", "stop.circle", stopAll)
        } else if suggested.contains("inspect_managed_session") {
            (title, systemImage, action) = ("Inspect managed session", "arrow.up.forward.square", { perform(.openLonghouse) })
        } else if suggested.contains("inspect_storage_outbox") {
            (title, systemImage, action) = ("Inspect storage outbox", "externaldrive.badge.questionmark", { perform(.runDoctor) })
        } else if suggested.contains("inspect_local_health") {
            (title, systemImage, action) = ("Inspect local health", "stethoscope", { perform(.runDoctor) })
        } else if suggested.contains("inspect_shipping") {
            (title, systemImage, action) = ("Inspect shipping", "shippingbox", { perform(.openLogs) })
        } else if suggested.contains("inspect_transport"),
                  !suggested.contains("free_disk_space"),
                  !suggested.contains("repair_machine") {
            (title, systemImage, action) = ("Inspect transport", "arrow.triangle.2.circlepath", { perform(.openLogs) })
        } else if suggested.contains("free_disk_space") {
            (title, systemImage, action) = ("Free disk space", "internaldrive", { perform(.freeDiskSpace) })
        } else if shouldOfferNativeRepair {
            (title, systemImage, action) = ("Repair local agent", "wrench.and.screwdriver", { perform(.repairInstall) })
        } else if suggested.contains("repair_machine") {
            (title, systemImage, action) = ("Repair machine", "wrench.and.screwdriver", { perform(.repairInstall) })
        } else if !suggested.isEmpty {
            (title, systemImage, action) = ("Inspect logs", "doc.text.magnifyingglass", { perform(.openLogs) })
        } else {
            (title, systemImage, action) = ("Repair", "wrench.and.screwdriver", { perform(.repairInstall) })
        }
        return Button(action: action) {
            Label(title, systemImage: systemImage)
        }
    }

    // MARK: Actions and feedback

    private func perform(_ action: HarnessAction) {
        let immediateFeedback = actionSink.handle(
            action,
            snapshot: snapshot,
            onFeedback: { terminalFeedback in
                setFeedback(terminalFeedback)
            }
        )
        setFeedback(immediateFeedback)
        if action == .refresh {
            refresh()
        }
    }

    private func feedbackBanner(_ feedback: HealthActionFeedback) -> some View {
        let tint = feedbackColor(for: feedback.style)

        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: feedbackIcon(for: feedback.style))
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(tint)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                Text(feedback.title)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.title)

                Text(feedback.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.detail)
            }

            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(tint.opacity(0.12))
        )
        .accessibilityIdentifier(LonghouseMenuBarAccessibilityID.Feedback.container)
    }

    private func feedbackColor(for style: HealthActionFeedbackStyle) -> Color {
        switch style {
        case .info: return .blue
        case .success: return HearthPalette.ok
        case .warning: return HearthPalette.warning
        case .failure: return HearthPalette.fault
        }
    }

    private func feedbackIcon(for style: HealthActionFeedbackStyle) -> String {
        switch style {
        case .info: return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .failure: return "xmark.circle.fill"
        }
    }
}
