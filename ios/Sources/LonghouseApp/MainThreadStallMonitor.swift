#if DEBUG
import Foundation
import OSLog

/// Debug-build diagnostic for proving whether an apparent UI freeze is a
/// blocked main run loop. Set `LONGHOUSE_MAIN_THREAD_STALL_DIAGNOSTICS=0` to
/// disable it while investigating probe-sensitive behavior.
final class MainThreadStallMonitor: @unchecked Sendable {
    struct Snapshot: Sendable {
        let count: Int
        let maximumDurationMs: Int
    }

    static let shared = MainThreadStallMonitor()

    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "MainThreadStall")
    private let monitorQueue = DispatchQueue(label: "ai.longhouse.ios.main-thread-stall")
    private var timer: DispatchSourceTimer?
    private var probeInFlight = false
    private var stallCount = 0
    private var maximumStallDurationMs = 0

    private init() {}

    func startIfEnabled() {
        guard ProcessInfo.processInfo.environment["LONGHOUSE_MAIN_THREAD_STALL_DIAGNOSTICS"] != "0" else {
            return
        }
        monitorQueue.async { [weak self] in
            guard let self, self.timer == nil else { return }
            let timer = DispatchSource.makeTimerSource(queue: self.monitorQueue)
            timer.schedule(deadline: .now(), repeating: .milliseconds(100), leeway: .milliseconds(20))
            timer.setEventHandler { [weak self] in self?.probeMainRunLoop() }
            self.timer = timer
            timer.resume()
            self.logger.info("main thread stall monitor started threshold_ms=250")
        }
    }

    func snapshot() async -> Snapshot {
        await snapshot(resetAfterRead: false)
    }

    func snapshotAndReset() async -> Snapshot {
        await snapshot(resetAfterRead: true)
    }

    private func snapshot(resetAfterRead: Bool) async -> Snapshot {
        await withCheckedContinuation { continuation in
            monitorQueue.async {
                let snapshot = Snapshot(
                    count: self.stallCount,
                    maximumDurationMs: self.maximumStallDurationMs
                )
                if resetAfterRead {
                    self.stallCount = 0
                    self.maximumStallDurationMs = 0
                }
                continuation.resume(returning: snapshot)
            }
        }
    }

    private func probeMainRunLoop() {
        guard !probeInFlight else { return }
        probeInFlight = true
        let sentAt = DispatchTime.now().uptimeNanoseconds
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let receivedAt = DispatchTime.now().uptimeNanoseconds
            let delayMs = Int((receivedAt - sentAt) / 1_000_000)
            self.monitorQueue.async {
                self.probeInFlight = false
                if delayMs >= 250 {
                    self.stallCount += 1
                    self.maximumStallDurationMs = max(self.maximumStallDurationMs, delayMs)
                    self.logger.error("main thread stall duration_ms=\(delayMs, privacy: .public) uptime_ms=\(Int(ProcessInfo.processInfo.systemUptime * 1000), privacy: .public)")
                    print("MAIN_THREAD_STALL duration_ms=\(delayMs) uptime_ms=\(Int(ProcessInfo.processInfo.systemUptime * 1000))")
                }
            }
        }
    }
}
#endif
