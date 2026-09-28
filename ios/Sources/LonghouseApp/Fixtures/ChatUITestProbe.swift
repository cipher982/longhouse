#if DEBUG
import SwiftUI
import UIKit

@MainActor
final class ChatUITestProbe {
    private(set) var statusLine = ""
    private(set) var latestItemID = "none"
    private(set) var lastStage = "none"

    private let path: String?
    private var renderCount = 0
    private var duplicateCount = 0
    private var repeatRenderCount = 0
    private var lastRenderedKey: String?
    private var maxRenderDurationMs = 0
    private var renderDurationsMs: [Int] = []
    private var traceRenderDurationsMs: [Int] = []
    private var tracePrepareDurationsMs: [Int] = []
    private var traceJSDecodeDurationsMs: [Int] = []
    private var traceJSHTMLDurationsMs: [Int] = []
    private var traceJSDOMDurationsMs: [Int] = []
    private var traceJSRAFDurationsMs: [Int] = []
    private var traceJSTotalDurationsMs: [Int] = []
    private var traceSourceRevisions: Set<Int> = []
    private var traceUnattributedRenderCount = 0
    private var traceRenderBaseline = 0
    private var traceDuplicateBaseline = 0
    private var traceRepeatBaseline = 0
    private var coldRenderMaxMs = 0
    private var traceMeasurementStarted = false
    private var tick = 0
    private var rowCount = 0
    private var payloadBytes = 0
    private var payloadFingerprint = "none"
    private var shouldStickToBottom = false
    private var renderDurationMs = 0
    private var benchmarkPhase = "idle"
    private var benchmarkUpdateCount = 0
    private var benchmarkRenderer = "none"
    private var benchmarkSemanticTier = "none"
    private var mainThreadStallCount = 0
    private var mainThreadStallMaxMs = 0
    private var coldMainThreadStallCount = 0
    private var coldMainThreadStallMaxMs = 0
    private let buildCommit: String
    private let buildDirty: Bool
    private let deviceName: String
    private let deviceModel: String
    private let osVersion: String
    private let benchmarkBuildConfiguration: String
    private let benchmarkDebugger: String
    private let benchmarkTemperature: String
    private let thermalState: String
    private let lowPowerMode: Bool
    private let batteryState: String
    private let batteryLevelPercent: Int
    private weak var statusLabel: UILabel?

    init(path: String?) {
        self.path = path
        switch BuildIdentityLoader.loadFromMainBundle() {
        case .success(let identity):
            buildCommit = identity.commit
            buildDirty = identity.dirty
        case .failure:
            buildCommit = "unknown"
            buildDirty = true
        }
        let environment = ProcessInfo.processInfo.environment
        deviceName = environment["SIMULATOR_DEVICE_NAME"] ?? UIDevice.current.model
        deviceModel = environment["SIMULATOR_MODEL_IDENTIFIER"] ?? Self.hardwareModelIdentifier()
        osVersion = UIDevice.current.systemVersion
        benchmarkBuildConfiguration = UITestHooks.transcriptBenchmarkBuildConfiguration ?? "unknown"
        benchmarkDebugger = UITestHooks.transcriptBenchmarkDebugger ?? "unknown"
        benchmarkTemperature = UITestHooks.transcriptBenchmarkTemperature ?? "uncontrolled"
        thermalState = Self.thermalStateName(ProcessInfo.processInfo.thermalState)
        lowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
        UIDevice.current.isBatteryMonitoringEnabled = true
        batteryState = Self.batteryStateName(UIDevice.current.batteryState)
        let level = UIDevice.current.batteryLevel
        batteryLevelPercent = level >= 0 ? Int((level * 100).rounded()) : -1
        rebuildAndPersist()
    }

    func record(_ diagnostics: RenderBeaconReporter.WebKitDiagnostics) {
        let latest = diagnostics.latest_item_id ?? "none"
        let fingerprint = diagnostics.payload_fingerprint ?? "\(diagnostics.row_count)|\(diagnostics.payload_byte_size)|\(latest)"
        let key = fingerprint
        if diagnostics.stage == "rendered" {
            if key == lastRenderedKey {
                repeatRenderCount += 1
            }
            lastRenderedKey = key
            renderCount += 1
            let durationMs = diagnostics.render_duration_ms ?? 0
            renderDurationsMs.append(durationMs)
            if benchmarkPhase == "running" {
                traceRenderDurationsMs.append(durationMs)
                tracePrepareDurationsMs.append(diagnostics.swift_prepare_duration_ms)
                if let value = diagnostics.js_decode_duration_ms { traceJSDecodeDurationsMs.append(value) }
                if let value = diagnostics.js_html_duration_ms { traceJSHTMLDurationsMs.append(value) }
                if let value = diagnostics.js_dom_duration_ms { traceJSDOMDurationsMs.append(value) }
                if let value = diagnostics.js_raf_duration_ms { traceJSRAFDurationsMs.append(value) }
                if let value = diagnostics.js_total_duration_ms { traceJSTotalDurationsMs.append(value) }
                if let revision = diagnostics.source_revision {
                    traceSourceRevisions.insert(revision)
                } else {
                    traceUnattributedRenderCount += 1
                }
            }
            maxRenderDurationMs = max(maxRenderDurationMs, durationMs)
        } else if diagnostics.stage == "duplicate" {
            duplicateCount += 1
        }

        rowCount = diagnostics.row_count
        payloadBytes = diagnostics.payload_byte_size
        latestItemID = latest
        payloadFingerprint = fingerprint
        lastStage = diagnostics.stage
        shouldStickToBottom = diagnostics.should_stick_to_bottom
        renderDurationMs = diagnostics.render_duration_ms ?? 0
        // Avoid benchmark telemetry becoming part of the workload. The trace
        // runner observes these in-memory fields and publishes one final sample.
        if benchmarkPhase != "running" {
            rebuildAndPersist()
        }
    }

    func recordTick(_ tick: Int) {
        self.tick = tick
        rebuildAndPersist()
    }

    func recordBenchmark(phase: String, updateCount: Int) {
        if phase == "running", !traceMeasurementStarted {
            traceMeasurementStarted = true
            traceRenderBaseline = renderCount
            traceDuplicateBaseline = duplicateCount
            traceRepeatBaseline = repeatRenderCount
            traceRenderDurationsMs = []
            tracePrepareDurationsMs = []
            traceJSDecodeDurationsMs = []
            traceJSHTMLDurationsMs = []
            traceJSDOMDurationsMs = []
            traceJSRAFDurationsMs = []
            traceJSTotalDurationsMs = []
            traceSourceRevisions = []
            traceUnattributedRenderCount = 0
            coldRenderMaxMs = maxRenderDurationMs
        }
        benchmarkPhase = phase
        benchmarkUpdateCount = updateCount
        rebuildAndPersist()
    }

    func recordBenchmarkRenderer(_ renderer: TranscriptBenchmarkRendererKind) {
        benchmarkRenderer = renderer.rawValue
        benchmarkSemanticTier = renderer.semanticTier
        rebuildAndPersist()
    }

    func recordMainThreadStalls(_ snapshot: MainThreadStallMonitor.Snapshot) {
        mainThreadStallCount = snapshot.count
        mainThreadStallMaxMs = snapshot.maximumDurationMs
        rebuildAndPersist()
    }

    func recordColdMainThreadStalls(_ snapshot: MainThreadStallMonitor.Snapshot) {
        coldMainThreadStallCount = snapshot.count
        coldMainThreadStallMaxMs = snapshot.maximumDurationMs
        rebuildAndPersist()
    }

    func attachStatusLabel(_ label: UILabel) {
        statusLabel = label
        updateStatusLabel()
    }

    private func rebuildAndPersist() {
        statusLine = [
            "renders=\(renderCount)",
            "duplicates=\(duplicateCount)",
            "repeats=\(repeatRenderCount)",
            "rows=\(rowCount)",
            "bytes=\(payloadBytes)",
            "latest=\(latestItemID)",
            "fingerprint=\(payloadFingerprint)",
            "stage=\(lastStage)",
            "stick=\(shouldStickToBottom ? 1 : 0)",
            "render_ms=\(renderDurationMs)",
            "max_render_ms=\(maxRenderDurationMs)",
            "render_p50_ms=\(percentile(0.50))",
            "render_p95_ms=\(percentile(0.95))",
            "cold_render_max_ms=\(coldRenderMaxMs)",
            "trace_renders=\(max(0, renderCount - traceRenderBaseline))",
            "trace_duplicates=\(max(0, duplicateCount - traceDuplicateBaseline))",
            "trace_repeats=\(max(0, repeatRenderCount - traceRepeatBaseline))",
            "trace_render_p50_ms=\(percentile(0.50, samples: traceRenderDurationsMs))",
            "trace_render_p95_ms=\(percentile(0.95, samples: traceRenderDurationsMs))",
            "trace_render_max_ms=\(traceRenderDurationsMs.max() ?? 0)",
            "trace_prepare_p95_ms=\(percentile(0.95, samples: tracePrepareDurationsMs))",
            "trace_js_decode_p95_ms=\(percentile(0.95, samples: traceJSDecodeDurationsMs))",
            "trace_js_html_p95_ms=\(percentile(0.95, samples: traceJSHTMLDurationsMs))",
            "trace_js_dom_p95_ms=\(percentile(0.95, samples: traceJSDOMDurationsMs))",
            "trace_js_raf_p95_ms=\(percentile(0.95, samples: traceJSRAFDurationsMs))",
            "trace_js_total_p95_ms=\(percentile(0.95, samples: traceJSTotalDurationsMs))",
            "trace_source_revisions=\(traceSourceRevisions.count)",
            "trace_unattributed_renders=\(traceUnattributedRenderCount)",
            "tick=\(tick)",
            "benchmark_phase=\(benchmarkPhase)",
            "benchmark_updates=\(benchmarkUpdateCount)",
            "benchmark_renderer=\(benchmarkRenderer)",
            "semantic_tier=\(benchmarkSemanticTier)",
            "build_commit=\(buildCommit)",
            "build_dirty=\(buildDirty ? 1 : 0)",
            "device_name=\(token(deviceName))",
            "device_model=\(token(deviceModel))",
            "os_version=\(token(osVersion))",
            "benchmark_build=\(token(benchmarkBuildConfiguration))",
            "benchmark_debugger=\(token(benchmarkDebugger))",
            "benchmark_temperature=\(token(benchmarkTemperature))",
            "thermal_state=\(thermalState)",
            "low_power=\(lowPowerMode ? 1 : 0)",
            "battery_state=\(batteryState)",
            "battery_level_percent=\(batteryLevelPercent)",
            "main_stalls=\(mainThreadStallCount)",
            "main_stall_max_ms=\(mainThreadStallMaxMs)",
            "cold_main_stalls=\(coldMainThreadStallCount)",
            "cold_main_stall_max_ms=\(coldMainThreadStallMaxMs)",
        ].joined(separator: " ")
        updateStatusLabel()
        persist()
    }

    private func updateStatusLabel() {
        statusLabel?.text = statusLine
        statusLabel?.accessibilityLabel = statusLine
    }

    private func token(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "unknown"
    }

    private static func hardwareModelIdentifier() -> String {
        var systemInfo = utsname()
        uname(&systemInfo)
        return withUnsafePointer(to: &systemInfo.machine) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: 1) {
                String(cString: $0)
            }
        }
    }

    private static func thermalStateName(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }

    private static func batteryStateName(_ state: UIDevice.BatteryState) -> String {
        switch state {
        case .unknown: return "unknown"
        case .unplugged: return "unplugged"
        case .charging: return "charging"
        case .full: return "full"
        @unknown default: return "unknown"
        }
    }

    private func percentile(_ quantile: Double) -> Int {
        percentile(quantile, samples: renderDurationsMs)
    }

    private func percentile(_ quantile: Double, samples: [Int]) -> Int {
        guard !samples.isEmpty else { return 0 }
        let sorted = samples.sorted()
        let index = min(sorted.count - 1, Int(ceil(Double(sorted.count) * quantile)) - 1)
        return sorted[max(0, index)]
    }

    private func persist() {
        guard let path else { return }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? statusLine.write(to: url, atomically: true, encoding: .utf8)
    }
}
#endif
