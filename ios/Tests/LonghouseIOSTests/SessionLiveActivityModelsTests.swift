import Foundation
import Testing
@testable import Longhouse

struct SessionLiveActivityModelsTests {
    @Test
    func decodesServerContentStatePayloadKeys() throws {
        let payload = """
        {
          "presenceState": "running",
          "displayPhase": "Running bash",
          "activeTool": "bash",
          "updatedAt": 1777140000,
          "isAttention": false
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "running")
        #expect(state.displayPhase == "Running bash")
        #expect(state.activeTool == "bash")
        #expect(state.updatedAt == 1_777_140_000)
        #expect(state.isAttention == false)
    }

    @Test
    func decodesNullActiveToolFromServerPayload() throws {
        let payload = """
        {
          "presenceState": "needs_user",
          "displayPhase": "Waiting on you",
          "activeTool": null,
          "updatedAt": 1777140001,
          "isAttention": true
        }
        """
        let data = try #require(payload.data(using: .utf8))
        let state = try JSONDecoder().decode(SessionWatchAttributes.ContentState.self, from: data)

        #expect(state.presenceState == "needs_user")
        #expect(state.activeTool == nil)
        #expect(state.isAttention == true)
    }
}
