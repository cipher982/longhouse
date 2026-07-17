#if DEBUG
import Foundation
import OSLog

/// Debug-build diagnostic for proving whether an apparent UI freeze is a
/// blocked main run loop. Set `LONGHOUSE_MAIN_THREAD_STALL_DIAGNOSTICS=0` to
/// disable it while investigating probe-sensitive behavior.
final class MainThreadStallMonitor: @unchecked Sendable {
    static let shared = MainThreadStallMonitor()

    private let logger = Logger(subsystem: "ai.longhouse.ios", category: "MainThreadStall")
    private let monitorQueue = DispatchQueue(label: "ai.longhouse.ios.main-thread-stall")
    private var timer: DispatchSourceTimer?
    private var probeInFlight = false

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
                    self.logger.error("main thread stall duration_ms=\(delayMs, privacy: .public) uptime_ms=\(Int(ProcessInfo.processInfo.systemUptime * 1000), privacy: .public)")
                    print("MAIN_THREAD_STALL duration_ms=\(delayMs) uptime_ms=\(Int(ProcessInfo.processInfo.systemUptime * 1000))")
                }
            }
        }
    }
}
#endif
