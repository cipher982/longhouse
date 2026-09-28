import Foundation
import Testing

@testable import Longhouse

/// The diagnostics funnel is the app's hottest main-actor path when anything
/// loops: a re-arming wake loop was measured at ~14k marks/second, with 1333 of
/// 1559 main-thread samples inside `_os_log_impl`, and it shipped a telemetry
/// POST every 40 marks for as long as it ran. These pin the budget that keeps a
/// future loop from spending the log it would be diagnosed from.
@MainActor
struct SessionOpenWaterfallTests {
    @Test
    func markStormIsCappedAndCounted() {
        let waterfall = SessionOpenWaterfall(sessionId: "session-1")
        let before = waterfall.suppressedMarkCountForTesting

        for _ in 0..<100 {
            waterfall.mark("storm", "detail")
        }

        // One stage gets a burst of 8 per second; a loop that emits 100 marks
        // in a single tick must spend the burst and drop the rest.
        let dropped = waterfall.suppressedMarkCountForTesting - before
        #expect(dropped >= 90, "expected the storm to be capped, dropped \(dropped)")
    }

    @Test
    func eachStageKeepsItsOwnBudget() {
        let waterfall = SessionOpenWaterfall(sessionId: "session-1")
        let before = waterfall.suppressedMarkCountForTesting

        // Two stages spending their full burst must both get through: the
        // budget is per stage, so one chatty stage cannot silence the marks
        // that explain what a different part of the open was doing.
        for _ in 0..<8 {
            waterfall.mark("history_fill", "loaded=1")
            waterfall.mark("older_applied", "page_items=1")
        }

        #expect(waterfall.suppressedMarkCountForTesting == before)
    }

    @Test
    func spreadingAStormAcrossStagesStillHitsTheTotalBudget() {
        let waterfall = SessionOpenWaterfall(sessionId: "session-1")
        let before = waterfall.suppressedMarkCountForTesting

        // Per-stage limits alone let a storm buy volume with names: 20 stages
        // at 8 marks each would be 160 marks in one tick. The total budget is
        // what actually bounds the funnel.
        for round in 0..<10 {
            for stage in 0..<20 {
                waterfall.mark("storm_\(stage)", "round=\(round)")
            }
        }

        let dropped = waterfall.suppressedMarkCountForTesting - before
        #expect(dropped >= 100, "expected the total budget to bite, dropped only \(dropped)")
    }
}
