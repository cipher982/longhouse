import Foundation
import OSLog

/// Client-side realtime latency beacon.
///
/// Measures provider-emitted → iOS-rendered latency. Posts fire-and-forget
/// to /api/telemetry/client-render. Idempotent per event_id so rapid
/// re-renders do not double-count.
actor RenderBeaconReporter {
    static let shared = RenderBeaconReporter()

    struct WebKitDiagnostics: Encodable, Equatable, Sendable {
        let stage: String
        let payload_byte_size: Int
        let row_count: Int
        let latest_item_id: String?
        let payload_fingerprint: String?
        let render_sequence: Int
        let js_failure_count: Int
        let should_stick_to_bottom: Bool
        let web_view_loaded: Bool
        let source_revision: Int?
        let source_operation: String?
        let swift_prepare_duration_ms: Int
        let render_duration_ms: Int?
        let js_decode_duration_ms: Int?
        let js_html_duration_ms: Int?
        let js_dom_duration_ms: Int?
        let js_raf_duration_ms: Int?
        let js_total_duration_ms: Int?
        let error_description: String?

        init(
            stage: String,
            payload_byte_size: Int,
            row_count: Int,
            latest_item_id: String?,
            payload_fingerprint: String? = nil,
            render_sequence: Int,
            js_failure_count: Int,
            should_stick_to_bottom: Bool,
            web_view_loaded: Bool,
            source_revision: Int? = nil,
            source_operation: String? = nil,
            swift_prepare_duration_ms: Int = 0,
            render_duration_ms: Int? = nil,
            js_decode_duration_ms: Int? = nil,
            js_html_duration_ms: Int? = nil,
            js_dom_duration_ms: Int? = nil,
            js_raf_duration_ms: Int? = nil,
            js_total_duration_ms: Int? = nil,
            error_description: String?
        ) {
            self.stage = stage
            self.payload_byte_size = payload_byte_size
            self.row_count = row_count
            self.latest_item_id = latest_item_id
            self.payload_fingerprint = payload_fingerprint
            self.render_sequence = render_sequence
            self.js_failure_count = js_failure_count
            self.should_stick_to_bottom = should_stick_to_bottom
            self.web_view_loaded = web_view_loaded
            self.source_revision = source_revision
            self.source_operation = source_operation
            self.swift_prepare_duration_ms = swift_prepare_duration_ms
            self.render_duration_ms = render_duration_ms
            self.js_decode_duration_ms = js_decode_duration_ms
            self.js_html_duration_ms = js_html_duration_ms
            self.js_dom_duration_ms = js_dom_duration_ms
            self.js_raf_duration_ms = js_raf_duration_ms
            self.js_total_duration_ms = js_total_duration_ms
            self.error_description = error_description
        }
    }

    struct Payload: Encodable, Sendable {
        let event_id: String
        let session_id: String?
        let surface: String
        let render_kind: String
        let managed: Bool
        let emitted_at_ms: Int64
        let rendered_at_ms: Int64
        let clock_skew_ms: Int
        let server_fanout_at_ms: Int64?
        let client_received_at_ms: Int64?
        let pubsub_seq: Int?
        let state_commit_seq: Int64?
        let state_phase: String?
        let state_observed_at_ms: Int64?
        let webkit: WebKitDiagnostics?
    }

    private var lastBeaconKey: String?

    func payload(
        sessionId: String,
        latestEventId: String,
        emittedAt: Date,
        managed: Bool,
        clockSkewMs: Int = 0,
        serverFanoutAtMs: Int64? = nil,
        clientReceivedAtMs: Int64? = nil,
        pubsubSeq: Int? = nil,
        renderKind: String = "event",
        stateCommitSeq: Int64? = nil,
        statePhase: String? = nil,
        stateObservedAtMs: Int64? = nil,
        webkit: WebKitDiagnostics? = nil
    ) -> Payload? {
        let stage = webkit?.stage ?? "rendered"
        let beaconKey = "\(sessionId):\(renderKind):\(latestEventId):\(stage)"
        if lastBeaconKey == beaconKey { return nil }
        lastBeaconKey = beaconKey
        return Payload(
            event_id: latestEventId,
            session_id: sessionId,
            surface: "ios",
            render_kind: renderKind,
            managed: managed,
            emitted_at_ms: Int64(emittedAt.timeIntervalSince1970 * 1000),
            rendered_at_ms: Int64(Date().timeIntervalSince1970 * 1000),
            clock_skew_ms: clockSkewMs,
            server_fanout_at_ms: serverFanoutAtMs,
            client_received_at_ms: clientReceivedAtMs,
            pubsub_seq: pubsubSeq,
            state_commit_seq: stateCommitSeq,
            state_phase: statePhase,
            state_observed_at_ms: stateObservedAtMs,
            webkit: webkit
        )
    }
}
