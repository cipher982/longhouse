import Foundation
import Testing
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
}
