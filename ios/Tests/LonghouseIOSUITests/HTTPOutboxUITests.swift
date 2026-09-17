import Foundation
import XCTest

/// Real-client proof against the disposable loopback Runtime Host-shaped fixture.
///
/// This test deliberately does not launch ChatUITestFixtureView, inject bytes into
/// production state, or call a mock API. The app receives only the normal
/// headless credentials/open-session environment and the seeded photo is chosen
/// through PhotosPicker. The fixture's first multipart request records the bytes
/// then drops the acknowledgement; after a process restart the driver enables a
/// served receipt and proves that the original operation identity and attachment
/// digest are still the only durable request evidence.
@MainActor
final class HTTPOutboxUITests: XCTestCase {
    private static let timeout: TimeInterval = 30

    private struct ProofFailure: Error, CustomStringConvertible {
        let description: String
    }

    private struct FixturePost: Decodable {
        let clientRequestId: String
        let text: String
        let intent: String
        let filename: String
        let mimeType: String
        let attachmentSha256: String
        let attachmentBytes: Int
    }

    private struct FixtureStream: Decodable {
        let number: Int
        let epoch: String
    }

    private struct FixtureReceipt: Decodable {
        let clientRequestId: String
        let intent: String
        let status: String
        let inputId: Int?
        let eventId: String
    }

    private struct FixtureState: Decodable {
        let sessionId: String
        let posts: [FixturePost]
        let receiptEnabled: Bool
        let receiptRequests: Int
        let servedReceipts: Int
        let lastServedReceipt: FixtureReceipt?
        let streams: [FixtureStream]
        let workspaceReads: [String]
    }

    private struct Configuration {
        let serverURL: URL
        let authToken: String
        let sessionID: String
        let stateURL: URL
        let enableReceiptURL: URL
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testRealHTTPOutboxPhotosPickerSurvivesTerminateAndReopen() async throws {
        let configuration = try configuration()
        let app = XCUIApplication()
        app.launchEnvironment = [
            // These are the app's actual headless authentication surface. No
            // fixture selector is provided, so production SessionView renders.
            "LONGHOUSE_HEADLESS_SERVER_URL": configuration.serverURL.absoluteString,
            "LONGHOUSE_HEADLESS_AUTH_TOKEN": configuration.authToken,
            "LONGHOUSE_HEADLESS_OPEN_SESSION": configuration.sessionID,
        ]
        defer { app.terminate() }

        app.launch()
        let transcript = app.descendants(matching: .any)["session-chat-transcript"]
        XCTAssertTrue(transcript.waitForExistence(timeout: Self.timeout), "headless app did not open the real session transcript")

        let composer = app.textFields["session-chat-composer"]
        let actions = app.buttons["session-chat-compose-actions"]
        XCTAssertTrue(composer.waitForExistence(timeout: Self.timeout), "real composer did not load")
        XCTAssertTrue(actions.waitForExistence(timeout: Self.timeout), "real composer action menu did not load")

        actions.tap()
        let attach = app.buttons["session-chat-attach"]
        XCTAssertTrue(attach.waitForExistence(timeout: 5), "real attachment action is not available")
        attach.tap()
        try chooseSeededPhoto(in: app)

        let tray = app.descendants(matching: .any)["session-chat-attachment-tray"]
        guard tray.waitForExistence(timeout: Self.timeout) else {
            throw ProofFailure(description: "PhotosPicker selection did not reach the production attachment tray")
        }

        let message = "HTTP outbox proof \(UUID().uuidString)"
        composer.tap()
        composer.typeText(message)
        let send = app.buttons["session-chat-send"]
        XCTAssertTrue(send.waitForExistence(timeout: 5), "real send control did not load")
        send.tap()

        // The fixture records the complete multipart body before dropping its
        // response. A single record plus no served receipt proves the client is
        // in the ambiguous, durable-outbox state rather than a known rejection.
        let firstState = try await waitForState(configuration, timeout: Self.timeout) {
            $0.posts.count == 1 && !$0.receiptEnabled && $0.servedReceipts == 0 && $0.receiptRequests >= 1
        }
        XCTAssertEqual(firstState.sessionId, configuration.sessionID)
        let firstPost = try XCTUnwrap(firstState.posts.first)
        XCTAssertEqual(firstPost.text, message)
        XCTAssertEqual(firstPost.intent, "auto")
        XCTAssertGreaterThan(firstPost.attachmentBytes, 0)
        XCTAssertEqual(firstPost.mimeType, "image/jpeg")
        XCTAssertFalse(firstPost.clientRequestId.isEmpty)

        let pendingWebView = app.webViews.firstMatch
        XCTAssertTrue(pendingWebView.waitForExistence(timeout: 5), "pending outbox state did not expose its WebView transcript")
        XCTAssertTrue(
            waitForWebViewText(pendingWebView, containing: "Not confirmed", timeout: Self.timeout),
            "ambiguous transport outcome was not rendered as Not confirmed before termination"
        )
        // The fixture has recorded the complete multipart body, but has not
        // enabled the server-owned receipt. Keep this genuinely unknown frame
        // as evidence before crossing the process boundary.
        let pendingFrame = XCTAttachment(screenshot: app.screenshot())
        pendingFrame.name = "http-outbox-unknown-before-termination"
        pendingFrame.lifetime = .keepAlways
        add(pendingFrame)

        app.terminate()
        // Do not fabricate a response in the app. This is the fixture's
        // server-owned receipt becoming available after the process boundary.
        try await enableReceipt(configuration)

        app.launch()
        let reopenedTranscript = app.descendants(matching: .any)["session-chat-transcript"]
        XCTAssertTrue(reopenedTranscript.waitForExistence(timeout: Self.timeout), "reopened app did not reach the real transcript")
        let reopenedWebView = app.webViews.firstMatch
        XCTAssertTrue(reopenedWebView.waitForExistence(timeout: Self.timeout), "reopened normal app did not expose its WebView transcript")
        XCTAssertTrue(
            waitForWebViewText(reopenedWebView, containing: "HTTP fixture ready.", timeout: Self.timeout),
            "reopened normal app did not render the fixture transcript"
        )
        let finalState = try await waitForState(configuration, timeout: Self.timeout) {
            $0.receiptEnabled
                && $0.receiptRequests >= 2
                && $0.servedReceipts >= 1
                && $0.posts.count == 1
                && $0.streams.contains(where: { $0.epoch == "proof-epoch-2" })
                && $0.workspaceReads.contains("proof-epoch-2")
        }
        let finalPost = try XCTUnwrap(finalState.posts.first)
        // The absence of a second post proves relaunch reconciled authority
        // instead of allocating a fresh operation or replaying bytes blindly.
        XCTAssertEqual(finalPost.clientRequestId, firstPost.clientRequestId)
        XCTAssertEqual(finalPost.attachmentSha256, firstPost.attachmentSha256)
        XCTAssertEqual(finalPost.attachmentBytes, firstPost.attachmentBytes)
        let servedReceipt = try XCTUnwrap(finalState.lastServedReceipt)
        XCTAssertEqual(servedReceipt.clientRequestId, firstPost.clientRequestId)
        XCTAssertEqual(servedReceipt.intent, "auto")
        XCTAssertEqual(servedReceipt.status, "accepted")
        XCTAssertEqual(servedReceipt.inputId, 7)
        XCTAssertEqual(servedReceipt.eventId, "proof-event-1")

        // A durable user event, not the optimistic outbox row, is the
        // confirmation. Its Longhouse origin and prompt text prove that the
        // epoch-2 workspace reconciliation rendered the accepted event; the
        // absence of the old status proves the unknown row was replaced.
        XCTAssertTrue(
            waitForWebViewText(reopenedWebView, containing: message, timeout: Self.timeout),
            "accepted prompt did not survive relaunch in the rendered transcript"
        )
        XCTAssertTrue(
            waitForWebViewText(reopenedWebView, containing: "Sent via Longhouse", timeout: Self.timeout),
            "accepted prompt was not rendered as an authoritative Longhouse event"
        )
        let finalSnapshot = try XCTUnwrap(reopenedWebView.snapshot())
        XCTAssertFalse(
            snapshotContains(finalSnapshot, value: "Not confirmed"),
            "reopened transcript still rendered the unknown outbox status after authoritative reconciliation"
        )

        let evidence: [String: Any] = [
            "proof": "real-app-http-fixture",
            "session_id": configuration.sessionID,
            "client_request_id": firstPost.clientRequestId,
            "attachment_sha256": firstPost.attachmentSha256,
            "attachment_bytes": firstPost.attachmentBytes,
            "stream_epochs": finalState.streams.map(\.epoch),
            "workspace_reads": finalState.workspaceReads,
            "receipt_requests": finalState.receiptRequests,
            "served_receipts": finalState.servedReceipts,
            "posts_recorded": finalState.posts.count,
            "receipt_enabled_after_relaunch": finalState.receiptEnabled,
            "receipt_status": servedReceipt.status,
            "receipt_event_id": servedReceipt.eventId,
        ]
        let evidenceData = try JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
        let attachment = XCTAttachment(data: evidenceData, uniformTypeIdentifier: "public.json")
        attachment.name = "http-outbox-proof-evidence.json"
        attachment.lifetime = .keepAlways
        add(attachment)
        let reconciledFrame = XCTAttachment(screenshot: app.screenshot())
        reconciledFrame.name = "http-outbox-reconciled-after-relaunch"
        reconciledFrame.lifetime = .keepAlways
        add(reconciledFrame)
    }

    private func configuration() throws -> Configuration {
        let environment = ProcessInfo.processInfo.environment
        let names = [
            "LONGHOUSE_HEADLESS_SERVER_URL",
            "LONGHOUSE_HEADLESS_AUTH_TOKEN",
            "LONGHOUSE_HEADLESS_OPEN_SESSION",
        ]
        if names.allSatisfy({ environment[$0] == nil }) {
            throw XCTSkip("HTTP outbox proof requires the isolated fixture runner")
        }
        func required(_ name: String) throws -> String {
            guard let value = environment[name]?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
                throw ProofFailure(description: "Missing \(name)")
            }
            return value
        }
        let rawServerURL = try required("LONGHOUSE_HEADLESS_SERVER_URL")
        guard let serverURL = URL(string: rawServerURL),
              ["http", "https"].contains(serverURL.scheme?.lowercased() ?? ""),
              serverURL.user == nil,
              serverURL.password == nil,
              serverURL.query == nil,
              serverURL.fragment == nil,
              ["127.0.0.1", "localhost", "::1"].contains(serverURL.host?.lowercased() ?? "") else {
            throw ProofFailure(description: "HTTP outbox proof only accepts a loopback fixture URL")
        }
        let authToken = try required("LONGHOUSE_HEADLESS_AUTH_TOKEN")
        let sessionID = try required("LONGHOUSE_HEADLESS_OPEN_SESSION")
        guard UUID(uuidString: sessionID) != nil else {
            throw ProofFailure(description: "LONGHOUSE_HEADLESS_OPEN_SESSION must be a UUID")
        }
        let rawStateURL = try required("LONGHOUSE_HTTP_PROOF_STATE_URL")
        let rawEnableReceiptURL = try required("LONGHOUSE_HTTP_PROOF_ENABLE_RECEIPT_URL")
        guard let stateURL = URL(string: rawStateURL),
              let enableReceiptURL = URL(string: rawEnableReceiptURL) else {
            throw ProofFailure(description: "HTTP proof control URLs are invalid")
        }
        guard stateURL.scheme == serverURL.scheme,
              enableReceiptURL.scheme == serverURL.scheme,
              stateURL.host == serverURL.host,
              enableReceiptURL.host == serverURL.host,
              ["/__proof/state", "/__proof/state/"].contains(stateURL.path),
              ["/__proof/enable-receipt", "/__proof/enable-receipt/"].contains(enableReceiptURL.path) else {
            throw ProofFailure(description: "HTTP proof control URLs must share the loopback fixture host")
        }
        return Configuration(
            serverURL: serverURL,
            authToken: authToken,
            sessionID: sessionID,
            stateURL: stateURL,
            enableReceiptURL: enableReceiptURL
        )
    }

    private func chooseSeededPhoto(in app: XCUIApplication) throws {
        // The exported accessibility hierarchy shows the seeded PhotosPicker
        // tile as an Image with this semantic identifier. Query that observed
        // tile directly rather than the composer's unrelated plus Image.
        let photo = app.images.matching(identifier: "PXGGridLayout-Info").firstMatch
        guard photo.waitForExistence(timeout: 5) else {
            throw ProofFailure(description: "seeded simulator Photos asset was not visible in the PhotosPicker grid")
        }
        guard waitUntilEnabled(photo, timeout: 5) else {
            throw ProofFailure(description: "PhotosPicker seeded photo was not accessibility-enabled")
        }
        let frame = photo.frame
        guard frame.width > 0, frame.height > 0 else {
            throw ProofFailure(description: "PhotosPicker seeded photo had no surfaced geometry")
        }

        // PhotosPicker exposes the tile as an enabled Image, but XCUITest may
        // not mark that noninteractive accessibility element hittable. Tap the
        // center of its surfaced geometry, not a hardcoded screen coordinate.
        photo.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()

        let done = app.buttons["Done"]
        guard done.waitForExistence(timeout: 5),
              waitUntilEnabled(done, timeout: 5),
              waitUntilHittable(done, timeout: 5),
              done.isEnabled else {
            throw ProofFailure(description: "PhotosPicker did not expose an enabled Done action after selecting the seeded photo")
        }
        done.tap()
    }

    private func waitUntilEnabled(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let expectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "enabled == true"),
            object: element
        )
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func waitUntilHittable(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let expectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "hittable == true"),
            object: element
        )
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func waitForWebViewText(
        _ webView: XCUIElement,
        containing value: String,
        timeout: TimeInterval
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let snapshot = try? webView.snapshot(),
               snapshotContains(snapshot, value: value) {
                return true
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        return false
    }

    private func snapshotContains(_ node: XCUIElementSnapshot, value: String) -> Bool {
        if node.label.contains(value) { return true }
        return node.children.contains { snapshotContains($0, value: value) }
    }


    private func waitForState(
        _ configuration: Configuration,
        timeout: TimeInterval,
        where predicate: (FixtureState) -> Bool
    ) async throws -> FixtureState {
        let deadline = Date().addingTimeInterval(timeout)
        var lastError: Error?
        while Date() < deadline {
            do {
                let state = try await fetchState(configuration)
                if predicate(state) { return state }
            } catch {
                lastError = error
            }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        throw ProofFailure(description: "Timed out waiting for fixture state: \(lastError.map { String(describing: $0) } ?? "predicate unmet")")
    }

    private func fetchState(_ configuration: Configuration) async throws -> FixtureState {
        var request = URLRequest(url: configuration.stateURL, cachePolicy: .reloadIgnoringLocalCacheData)
        request.setValue("Bearer \(configuration.authToken)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ProofFailure(description: "fixture state endpoint did not return HTTP 200")
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(FixtureState.self, from: data)
    }

    private func enableReceipt(_ configuration: Configuration) async throws {
        var request = URLRequest(url: configuration.enableReceiptURL)
        request.httpMethod = "POST"
        request.setValue("Bearer \(configuration.authToken)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ProofFailure(description: "fixture receipt enable endpoint did not return HTTP 200")
        }
    }
}
