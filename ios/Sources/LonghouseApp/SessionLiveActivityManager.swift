@preconcurrency import ActivityKit
import Foundation

@MainActor
final class SessionLiveActivityManager: ObservableObject {
    @Published private(set) var watchedSessionId: String?
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?

    var isWatching: Bool { watchedSessionId != nil }

    init() {
        refreshWatchedSession()
    }

    func refreshWatchedSession() {
        watchedSessionId = Activity<SessionWatchAttributes>.activities.first?.attributes.sessionId
    }

    func isWatching(sessionId: String) -> Bool {
        watchedSessionId == sessionId
    }

    func toggle(detail: SessionDetail, appState: AppState) async {
        if isWatching(sessionId: detail.id) {
            await stopWatching(sessionId: detail.id, appState: appState)
        } else {
            await startWatching(detail: detail, appState: appState)
        }
    }

    func update(detail: SessionDetail) async {
        let content = ActivityContent(
            state: detail.liveActivityContentState(),
            staleDate: Calendar.current.date(byAdding: .minute, value: 5, to: Date())
        )
        for activity in Activity<SessionWatchAttributes>.activities where activity.attributes.sessionId == detail.id {
            await activity.update(content)
        }
        refreshWatchedSession()
    }

    private func startWatching(detail: SessionDetail, appState: AppState) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            errorMessage = "Live Activities are disabled for Longhouse."
            return
        }
        isBusy = true
        defer { isBusy = false }

        do {
            for activity in Activity<SessionWatchAttributes>.activities where activity.attributes.sessionId != detail.id {
                await activity.end(nil, dismissalPolicy: .immediate)
            }
            let content = ActivityContent(
                state: detail.liveActivityContentState(),
                staleDate: Calendar.current.date(byAdding: .minute, value: 5, to: Date())
            )
            let activity = try Activity<SessionWatchAttributes>.request(
                attributes: detail.liveActivityAttributes,
                content: content,
                pushType: .token
            )
            watchedSessionId = detail.id
            errorMessage = nil
            observePushTokenUpdates(for: activity, appState: appState)
        } catch {
            errorMessage = "Couldn't start Live Activity: \(error.localizedDescription)"
            refreshWatchedSession()
        }
    }

    private func stopWatching(sessionId: String, appState: AppState) async {
        isBusy = true
        defer { isBusy = false }

        let activities = Activity<SessionWatchAttributes>.activities.filter { $0.attributes.sessionId == sessionId }
        for activity in activities {
            try? await markEnded(activityId: activity.id, appState: appState)
            await activity.end(nil, dismissalPolicy: .immediate)
        }
        errorMessage = nil
        refreshWatchedSession()
    }

    private func observePushTokenUpdates(for activity: Activity<SessionWatchAttributes>, appState: AppState) {
        Task {
            for await tokenData in activity.pushTokenUpdates {
                let token = tokenData.map { String(format: "%02x", $0) }.joined()
                try? await register(activity: activity, pushToken: token, appState: appState)
            }
        }
    }

    private func register(
        activity: Activity<SessionWatchAttributes>,
        pushToken: String,
        appState: AppState
    ) async throws {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return }
        try await api.registerAPNSLiveActivity(
            sessionId: activity.attributes.sessionId,
            activityId: activity.id,
            pushToken: pushToken,
            pushEnvironment: PushNotificationStore.pushEnvironment,
            appBuildId: PushNotificationStore.currentAppBuildID
        )
    }

    private func markEnded(activityId: String, appState: AppState) async throws {
        guard let api = LonghouseAPI(host: appState.serverURL) else { return }
        try await api.endAPNSLiveActivity(activityId: activityId)
    }
}
