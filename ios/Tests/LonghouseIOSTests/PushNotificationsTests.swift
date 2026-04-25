import Foundation
import Testing
import UserNotifications
@testable import Longhouse

struct PushNotificationsTests {
    @Test
    func deviceTokenStorageNormalizesHexLowercase() throws {
        let token = Data([0xAB, 0xCD, 0x01, 0xEF])

        let stored = PushNotificationStore.saveDeviceToken(token)

        #expect(stored == "abcd01ef")
        #expect(PushNotificationStore.storedDeviceToken() == "abcd01ef")
    }

    @Test
    func pendingSessionConsumptionIsOneShot() throws {
        PushNotificationStore.storePendingSessionID("session-123")

        #expect(PushNotificationStore.consumePendingSessionID() == "session-123")
        #expect(PushNotificationStore.consumePendingSessionID() == nil)
    }

    @Test
    func pendingSessionCanBeClearedAfterLiveOpen() throws {
        PushNotificationStore.storePendingSessionID("session-123")
        PushNotificationStore.clearPendingSessionID("other-session")
        #expect(PushNotificationStore.consumePendingSessionID() == "session-123")

        PushNotificationStore.storePendingSessionID("session-456")
        PushNotificationStore.clearPendingSessionID("session-456")
        #expect(PushNotificationStore.consumePendingSessionID() == nil)
    }

    @Test
    func attentionNotificationCategoryOffersOpenSessionAction() throws {
        let categories = LonghouseNotificationCategory.allCategories()
        let attention = try #require(categories.first { $0.identifier == LonghouseNotificationCategory.sessionAttention })

        #expect(attention.actions.map(\.identifier) == [LonghouseNotificationCategory.openSessionAction])
    }

    @Test
    func widgetOrderingPutsAttentionBeforeRecentActive() throws {
        let sessions = [
            makeSession(id: "idle", presenceState: "idle"),
            makeSession(id: "needs", presenceState: "needs_user"),
            makeSession(id: "archived-blocked", presenceState: "blocked", userState: "archived"),
            makeSession(id: "running", presenceState: "running"),
        ]

        let ordered = SessionSummary.attentionWidgetOrder(sessions, limit: 3)

        #expect(ordered.map(\.id) == ["needs", "idle", "running"])
    }

    @Test
    func widgetSnapshotPersistsActiveSessionsOnly() throws {
        let suiteName = "LonghouseWidgetSnapshotTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let sessions = [
            makeSession(id: "needs", presenceState: "needs_user"),
            makeSession(id: "archived", presenceState: "running", userState: "archived"),
            makeSession(id: "running", presenceState: "running"),
        ]

        WidgetSessionSnapshotStore.save(sessions: sessions, defaults: defaults)
        let snapshot = try #require(WidgetSessionSnapshotStore.load(defaults: defaults))

        #expect(snapshot.totalActive == 2)
        #expect(snapshot.sessions.map(\.id) == ["needs", "running"])
    }

    private func makeSession(
        id: String,
        presenceState: String,
        userState: String? = "active"
    ) -> SessionSummary {
        SessionSummary(
            id: id,
            title: id,
            presenceState: presenceState,
            provider: "codex",
            project: "zerg",
            lastActivityAt: nil,
            userState: userState,
            status: nil
        )
    }
}
