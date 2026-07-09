import SwiftUI

// MARK: - Preview helpers

private extension SessionDetail {
    static func mock(
        provider: String = "claude",
        headline: String = "Idle",
        runtimeDetail: String? = "Waiting for next prompt",
        tone: String = "idle",
        capabilityLabel: String = "Live on cinder",
        composerDisabledReason: String? = nil,
        live: Bool = true,
        canSteer: Bool = false,
        canQueue: Bool = false,
        loopMode: SessionLoopMode = .assist,
        executing: Bool = false
    ) -> SessionDetail {
        let json = """
        {
          "id": "preview-1",
          "provider": "\(provider)",
          "project": "my-project",
          "cwd": "/Users/example/code",
          "gitBranch": "main",
          "summary": "Working on iOS session view redesign",
          "summaryTitle": "iOS session view redesign",
          "presenceState": "\(executing ? "running" : "idle")",
          "userState": "active",
          "capabilities": {
            "liveControlAvailable": \(live),
            "hostReattachAvailable": false,
            "replyToLiveSessionAvailable": \(live),
            "canQueueNextInput": \(canQueue),
            "canSteerActiveTurn": \(canSteer),
            "displayLabel": "\(capabilityLabel)",
            "displayTone": "\(live ? "success" : "warning")",
            "composerDisabledReason": \(composerDisabledReason.map { "\"\($0)\"" } ?? "null")
          },
          "runtimeDisplay": {
            "truthTier": "managed-local",
            "signalTier": "phase_signal",
            "state": "\(executing ? "running" : "idle")",
            "tone": "\(tone)",
            "headline": "\(headline)",
            "detail": \(runtimeDetail.map { "\"\($0)\"" } ?? "null"),
            "phaseLabel": "\(headline)",
            "compactToolLabel": null,
            "isLive": \(live),
            "isExecuting": \(executing),
            "needsAttention": false,
            "isIdle": \(!executing),
            "isStalled": false,
            "isManagedLocalTruth": true,
            "hasSignal": true,
            "controlPath": "managed",
            "activityRecency": "live",
            "lifecycle": "open",
            "hostState": "online",
            "terminalReason": null
          },
          "loopMode": "\(loopMode.rawValue)"
        }
        """
        do {
            return try JSONDecoder().decode(SessionDetail.self, from: Data(json.utf8))
        } catch {
            print("--- [SessionDetail.mock decoding failure] ---")
            print(error)
            print("JSON:")
            print(json)
            print("---------------------------------------------")
            fatalError("Failed to decode SessionDetail mock: \(error)")
        }
    }
}

// MARK: - Preview chrome — dock + composer only

private struct ComposerPreviewChrome: View {
    let detail: SessionDetail
    @State private var text = ""

    var body: some View {
        VStack(spacing: 0) {
            navBar

            // Fake chat content above
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    assistantBubble("Hey, I just finished updating the repo structure. The results digest is now the canonical current-state readout.")
                    userBubble("Thanks, can you also update the experiment journal?")
                    assistantBubble("Sure! I've updated `experiment_journal.md` with the latest documentation policy and statistical reset. The current policy prioritizes bucketed results with confidence intervals.")
                }
                .padding()
            }

            // The actual chrome we're designing
            SessionRuntimeDock(detail: detail)
            composerRow
        }
    }

    private var navBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "chevron.left")
                .font(.body.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 28, height: 28)
            Text("Session")
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
            Spacer(minLength: 0)
            LoopModeButtons(
                currentMode: detail.effectiveLoopMode,
                disabled: false,
                onChange: { _ in }
            )
            Image(systemName: "bell.fill")
                .font(.body)
                .foregroundStyle(Color.white.opacity(0.72))
                .frame(width: 28, height: 28)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private var composerRow: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Menu {
                Button {} label: {
                    Label("Draft reply", systemImage: "sparkles")
                }
                Button {} label: {
                    Label("Attach images", systemImage: "paperclip")
                }
            } label: {
                Image(systemName: "plus")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 32, height: 32)
            }
            .accessibilityLabel("Message actions")

            TextField("Reply", text: $text, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...6)

            Image(systemName: "arrow.up.circle.fill")
                .font(.title2)
                .foregroundStyle(text.isEmpty ? Color.secondary.opacity(0.3) : Color.accentColor)
        }
        .padding(12)
        .background(.bar)
    }

    private func userBubble(_ msg: String) -> some View {
        HStack {
            Spacer(minLength: 48)
            Text(msg)
                .font(.callout)
                .padding(10)
                .background(Color.blue.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private func assistantBubble(_ msg: String) -> some View {
        Text(msg)
            .font(.callout)
            .padding(10)
            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Previews

#Preview("Idle · Assist · Dark") {
    ComposerPreviewChrome(detail: .mock())
        .preferredColorScheme(.dark)
}

#Preview("Running · can steer + queue · Dark") {
    ComposerPreviewChrome(detail: .mock(
        headline: "Working",
        runtimeDetail: "Using Shell",
        tone: "running",
        canSteer: true,
        canQueue: true,
        executing: true
    ))
    .preferredColorScheme(.dark)
}

#Preview("Launch setup · Dark") {
    ComposerPreviewChrome(detail: .mock(
        provider: "codex",
        headline: "Launch dispatch",
        runtimeDetail: "Waiting for the host",
        tone: "running",
        capabilityLabel: "Launching",
        composerDisabledReason: "Setting up Codex.",
        live: false,
        canSteer: false,
        canQueue: false,
        executing: true
    ))
    .preferredColorScheme(.dark)
}

#Preview("Idle · Autopilot · Dark") {
    ComposerPreviewChrome(detail: .mock(loopMode: .autopilot))
        .preferredColorScheme(.dark)
}

#Preview("Idle · Off · Light") {
    ComposerPreviewChrome(detail: .mock(loopMode: .manual))
        .preferredColorScheme(.light)
}

// MARK: - Transcript load-state previews (M3: one shared overlay component)

#Preview("Transcript · hard error · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .hardError("Couldn't load session: The Internet connection appears to be offline."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · refresh banner · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(
            state: .contentWithRefreshError("Couldn't refresh. Showing saved messages."),
            onRetry: {}
        )
    }
    .preferredColorScheme(.dark)
}

#Preview("Transcript · loading · Dark") {
    ZStack {
        Color(.systemBackground).ignoresSafeArea()
        TranscriptStateOverlay(state: .loading, onRetry: {})
    }
    .preferredColorScheme(.dark)
}
