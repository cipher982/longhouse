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
    func attentionNotificationCategoryOffersOpenSessionAction() throws {
        let categories = LonghouseNotificationCategory.allCategories()
        let attention = try #require(categories.first { $0.identifier == LonghouseNotificationCategory.sessionAttention })

        #expect(attention.actions.map(\.identifier) == [LonghouseNotificationCategory.openSessionAction])
    }
}
