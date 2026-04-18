import SwiftUI

@MainActor
struct InboxView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = InboxViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isInitialLoading {
                    ProgressView().controlSize(.large)
                } else if let error = viewModel.errorMessage, viewModel.isEmpty {
                    errorView(error)
                } else if viewModel.isEmpty {
                    emptyView
                } else {
                    listBody
                }
            }
            .navigationTitle("Inbox")
            .refreshable { await viewModel.refresh(using: appState) }
            .task { await viewModel.load(using: appState) }
        }
    }

    private var listBody: some View {
        List {
            if !viewModel.attention.isEmpty {
                Section("Needs you") {
                    ForEach(viewModel.attention) { session in
                        NavigationLink(value: session) {
                            AttentionRow(session: session)
                        }
                    }
                }
            }
            if !viewModel.recent.isEmpty {
                Section("Recent") {
                    ForEach(viewModel.recent) { session in
                        NavigationLink(value: session) {
                            SessionRow(session: session)
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationDestination(for: SessionSummary.self) { session in
            SessionView(sessionId: session.id, fallbackTitle: session.title)
        }
    }

    private var emptyView: some View {
        ContentUnavailableView(
            "Caught up",
            systemImage: "tray",
            description: Text("Nothing needs your attention right now.")
        )
    }

    private func errorView(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(.orange)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Try again") {
                Task { await viewModel.refresh(using: appState) }
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
    }
}

private struct AttentionRow: View {
    let session: SessionSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Circle()
                    .fill(session.isBlocked ? Color.orange : Color.yellow)
                    .frame(width: 8, height: 8)
                Text(session.attentionLabel)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            Text(session.title)
                .font(.body.weight(.medium))
                .lineLimit(2)
            HStack(spacing: 6) {
                if let project = session.project {
                    Text(project).font(.caption).foregroundStyle(.secondary)
                }
                if let provider = session.provider {
                    Text("·").foregroundStyle(.secondary)
                    Text(provider).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

private struct SessionRow: View {
    let session: SessionSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(session.title)
                .font(.body)
                .lineLimit(2)
            HStack(spacing: 6) {
                if let project = session.project {
                    Text(project).font(.caption).foregroundStyle(.secondary)
                }
                if let provider = session.provider {
                    Text("·").foregroundStyle(.secondary)
                    Text(provider).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

@MainActor
final class InboxViewModel: ObservableObject {
    @Published var attention: [SessionSummary] = []
    @Published var recent: [SessionSummary] = []
    @Published var errorMessage: String?
    @Published var isInitialLoading = true

    var isEmpty: Bool { attention.isEmpty && recent.isEmpty }

    func load(using appState: AppState) async {
        if !isInitialLoading { return }
        await refresh(using: appState)
    }

    func refresh(using appState: AppState) async {
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Invalid server URL"
            isInitialLoading = false
            return
        }
        do {
            async let attentionTask = api.sessionsNeedingAttention()
            async let recentTask = api.recentSessions(limit: 30)
            let (attention, recent) = try await (attentionTask, recentTask)
            let attentionIds = Set(attention.map(\.id))
            self.attention = attention
            self.recent = recent.filter { !attentionIds.contains($0.id) }
            self.errorMessage = nil
        } catch LonghouseAPIError.notAuthenticated {
            errorMessage = "Session expired. Sign in again."
        } catch {
            errorMessage = "Couldn't load sessions: \(error.localizedDescription)"
        }
        isInitialLoading = false
    }
}
