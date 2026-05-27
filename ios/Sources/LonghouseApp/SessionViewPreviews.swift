import SwiftUI

// MARK: - Preview helpers

private extension SessionDetail {
    static func mock(
        headline: String = "Idle",
        runtimeDetail: String? = "Waiting for next prompt",
        tone: String = "idle",
        capabilityLabel: String = "Live on cinder",
        live: Bool = true,
        canSteer: Bool = false,
        canQueue: Bool = false,
        loopMode: SessionLoopMode = .assist,
        executing: Bool = false
    ) -> SessionDetail {
        let json = """
        {
          "id": "preview-1",
          "provider": "claude",
          "project": "my-project",
          "cwd": "/Users/david/code",
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
            "displayTone": "\(live ? "success" : "warning")"
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
            SessionRuntimeDock(
                detail: detail,
                loopMode: detail.effectiveLoopMode,
                isUpdatingLoopMode: false,
                onLoopModeChange: { _ in }
            )
            composerRow
        }
    }

    private var composerRow: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Image(systemName: "sparkles")
                .font(.title3)
                .foregroundStyle(text.isEmpty ? Color.secondary : Color.secondary.opacity(0.3))
                .frame(width: 32, height: 32)

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

#Preview("Idle · Autopilot · Dark") {
    ComposerPreviewChrome(detail: .mock(loopMode: .autopilot))
        .preferredColorScheme(.dark)
}

#Preview("Idle · Off · Light") {
    ComposerPreviewChrome(detail: .mock(loopMode: .manual))
        .preferredColorScheme(.light)
}
