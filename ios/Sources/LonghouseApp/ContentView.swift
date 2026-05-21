import SwiftUI

struct ContentView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        Group {
#if DEBUG
            if UITestHooks.shouldUseTimelineOpenFixture {
                TimelineOpenUITestFixtureView()
            } else if let fixtureName = UITestHooks.chatFixtureName {
                ChatUITestFixtureView(fixtureName: fixtureName)
            } else {
                normalContent
            }
#else
            normalContent
#endif
        }
    }

    @ViewBuilder
    private var normalContent: some View {
        if appState.isValidating {
            LoadingScreen()
        } else if appState.isAuthenticated {
            AuthenticatedPager()
        } else {
            LoginView()
                .overlay(alignment: .topTrailing) {
                    ServerConfigButton()
                }
        }
    }
}

private struct LoadingScreen: View {
    var body: some View {
        ZStack {
            Color(red: 0.04, green: 0.04, blue: 0.06).ignoresSafeArea()
            VStack(spacing: 16) {
                Image(systemName: "house.lodge.fill")
                    .font(.system(size: 36))
                    .foregroundStyle(.white.opacity(0.6))
                ProgressView()
                    .tint(.white.opacity(0.6))
            }
        }
    }
}

private struct AuthenticatedPager: View {
    @EnvironmentObject var appState: AppState
    @State private var selectedTab: PagerTab = .timeline

    var body: some View {
        TabView(selection: $selectedTab) {
            TimelineView()
                .tag(PagerTab.timeline)
                .tabItem {
                    Label("Timeline", systemImage: "rectangle.stack")
                }
            SettingsView()
                .tag(PagerTab.settings)
                .tabItem {
                    Label("Settings", systemImage: "gearshape")
                }
        }
        .onReceive(NotificationCenter.default.publisher(for: .longhouseOpenSessionFromPush)) { _ in
            selectedTab = .timeline
        }
    }
}

private enum PagerTab {
    case timeline
    case settings
}

private struct ServerConfigButton: View {
    @State private var showingSheet = false

    var body: some View {
        Button {
            showingSheet = true
        } label: {
            Image(systemName: "server.rack")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.white.opacity(0.4))
                .padding(10)
                .background(.white.opacity(0.06), in: Circle())
        }
        .accessibilityIdentifier("login.serverConfig")
        .padding(.trailing, 8)
        .padding(.top, 4)
        .sheet(isPresented: $showingSheet) {
            ServerConfigSheet()
        }
    }
}

struct ServerConfigSheet: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var urlText = ""
    @State private var widgetProbeResult: WidgetLoadResult?
    @State private var isRunningWidgetProbe = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Longhouse Server") {
                    TextField("https://your-instance.longhouse.ai", text: $urlText)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                }
                Section {
                    Text("Enter the URL of your Longhouse instance.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
#if DEBUG
                widgetDebugSection
#endif
            }
            .navigationTitle("Server")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let trimmed = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !trimmed.isEmpty {
                            appState.setServer(trimmed)
                        }
                        dismiss()
                    }
                }
            }
            .onAppear {
                urlText = appState.serverURL
            }
        }
        .presentationDetents([.medium])
    }

#if DEBUG
    @ViewBuilder
    private var widgetDebugSection: some View {
        let debugState = SharedAuthStore.debugState(for: appState.serverURL)

        Section("Widget Debug") {
            LabeledContent("App Group", value: debugState.appGroupAvailable ? "available" : "missing")
            if let containerPath = debugState.containerPath {
                Text(containerPath)
                    .font(.caption2)
                    .textSelection(.enabled)
            }
            LabeledContent("Shared Server", value: debugState.serverURL ?? "none")
            LabeledContent("Shared Cookies", value: "\(debugState.cookieCount)")
            if !debugState.cookieNames.isEmpty {
                Text(debugState.cookieNames.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Button {
                Task {
                    isRunningWidgetProbe = true
                    widgetProbeResult = await WidgetSessionLoader.load()
                    isRunningWidgetProbe = false
                }
            } label: {
                if isRunningWidgetProbe {
                    ProgressView()
                } else {
                    Text("Run Widget Probe")
                }
            }

            if let widgetProbeResult {
                Text(widgetProbeResult.statusTitle ?? (widgetProbeResult.isSignedIn ? "Signed in" : "Unavailable"))
                    .font(.subheadline.weight(.medium))
                if let message = widgetProbeResult.statusMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                LabeledContent("Probe Cookies", value: "\(widgetProbeResult.debugState.cookieCount)")
            }
        }
    }
#endif
}
