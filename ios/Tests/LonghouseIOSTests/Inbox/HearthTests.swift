import Foundation
import Metal
import Testing
@testable import Longhouse

struct HearthHeatTests {
    private let wall = Date(timeIntervalSince1970: 1_790_000_000)

    private func events(_ heat: HearthHeat, from start: Double, seconds: Double) -> [HearthEvent] {
        var events: [HearthEvent] = []
        var time = start
        while time < start + seconds {
            time += 1.0 / 30
            heat.step(time: time, delta: 1.0 / 30, wall: wall.addingTimeInterval(time)) { events.append($0) }
        }
        return events
    }

    @Test
    func counterGrowthFiresEventsAndRaisesTheFlame() {
        let heat = HearthHeat()
        heat.update(snapshot: HearthSnapshot(mode: .working, toolCalls: 10, assistantMessages: 4), time: 0, wall: wall)
        let pilot = heat.target(time: 0)
        #expect(pilot >= HearthHeat.heightFloor)
        heat.update(snapshot: HearthSnapshot(mode: .working, toolCalls: 13, assistantMessages: 5, tool: "Bash"),
                    time: 1.2, wall: wall)
        let fired = events(heat, from: 1.2, seconds: 3)
        #expect(fired.filter { $0 == .tool(.exec) }.count == 3)
        #expect(fired.contains(.message))
        #expect(heat.target(time: 4.2) > pilot)
    }

    @Test
    func aFallingCounterIsANewBaselineNotEvents() {
        let heat = HearthHeat()
        heat.update(snapshot: HearthSnapshot(mode: .idle, toolCalls: 40, userMessages: 6), time: 0, wall: wall)
        heat.update(snapshot: HearthSnapshot(mode: .idle, toolCalls: 2, userMessages: 1), time: 1, wall: wall)
        #expect(events(heat, from: 1, seconds: 3).isEmpty)
        #expect(!heat.isActive(time: 4))
    }

    @Test
    func aReconnectBurstIsCappedAndKeepsItsPrompts() {
        let heat = HearthHeat()
        heat.update(snapshot: HearthSnapshot(mode: .working, toolCalls: 0, userMessages: 0), time: 0, wall: wall)
        heat.update(snapshot: HearthSnapshot(mode: .working, toolCalls: 500, userMessages: 2, tool: "Edit"),
                    time: 2.5, wall: wall)
        let fired = events(heat, from: 2.5, seconds: 4)
        #expect(fired.count == 8)
        #expect(fired.prefix(2).allSatisfy { $0 == .prompt })
    }

    @Test
    func anIdleBedCoolsFromItsLastActivity() {
        let heat = HearthHeat()
        let snapshot = HearthSnapshot(mode: .idle, lastActivity: wall.addingTimeInterval(-HearthHeat.surfaceCoolingTime))
        heat.update(snapshot: snapshot, time: 0, wall: wall)
        let expected = HearthHeat.ambientTemperature
            + (HearthHeat.litTemperature - HearthHeat.ambientTemperature) * Float(exp(-1.0))
        #expect(abs(heat.surface - expected) < 1)
        #expect(heat.core > heat.surface)
        #expect(heat.target(time: 0) == 0)
    }

    @Test
    func waitingKeepsTheCoalsWarmAndEndedIsAsh() {
        let waiting = HearthHeat()
        waiting.update(snapshot: HearthSnapshot(mode: .waiting, lastActivity: wall.addingTimeInterval(-7200)),
                       time: 0, wall: wall)
        #expect(waiting.surface >= HearthHeat.waitingTemperature)

        let ended = HearthHeat()
        ended.update(snapshot: HearthSnapshot(mode: .ended, toolCalls: 3, lastActivity: wall), time: 0, wall: wall)
        ended.update(snapshot: HearthSnapshot(mode: .ended, toolCalls: 9, lastActivity: wall), time: 1, wall: wall)
        #expect(events(ended, from: 1, seconds: 2).isEmpty)
        #expect(ended.surface == HearthHeat.ambientTemperature)
        #expect(!ended.isActive(time: 3))
    }

    @Test
    func toolNamesMapToSparkKinds() {
        #expect(HearthToolKind(name: "Bash") == .exec)
        #expect(HearthToolKind(name: "mcp__fs__write_file") == .edit)
        #expect(HearthToolKind(name: "WebFetch") == .read)
        #expect(HearthToolKind(name: "Task") == .agent)
        #expect(HearthToolKind(name: nil) == .other)
    }

    @Test
    func theDrawnFireMatchesTheWebGeometry() {
        // Web: tile = 1.2 x the cell height at the 32:44 aspect, bottom on the
        // cell; glow 2.4 tile widths.
        let geometry = HearthGeometry(cell: CGSize(width: 26, height: 36))
        #expect(abs(geometry.tile.height - 43.2) < 0.01)
        #expect(abs(geometry.tile.width - 43.2 * 32 / 44) < 0.01)
        #expect(geometry.tile.maxY == 36)
        #expect(abs(geometry.glow.width - geometry.tile.width * 2.4) < 0.01)
        #expect(geometry.bounds.contains(geometry.tile))
        #expect(geometry.bounds.contains(geometry.glow))
    }
}

struct HearthSimulationTests {
    /// The runtime-compiled solver lights a fire: a busy session's tile
    /// reaches a measured flame height in about two seconds of simulation.
    @Test
    func theMetalSolverLightsAWorkingFire() throws {
        let device = try #require(MTLCreateSystemDefaultDevice())
        let simulation = try HearthSimulation(device: device, library: HearthSimulation.makeLibrary(device: device))
        let heat = HearthHeat()
        let wall = Date(timeIntervalSince1970: 1_790_000_000)
        heat.update(snapshot: HearthSnapshot(mode: .working, toolCalls: 300, lastActivity: wall,
                                             started: wall.addingTimeInterval(-3600)), time: 0, wall: wall)
        let tile = try #require(simulation.attach(heat, seed: 0.3, time: 0, reducedMotion: false))
        var time = 0.0
        for _ in 0..<90 {
            time += 1.0 / 30
            let burning = simulation.advance(time: time, delta: 1.0 / 30, wall: wall, reducedMotion: false)
            #expect(burning == 1)
            let command = try #require(simulation.queue.makeCommandBuffer())
            let slot = simulation.encodeSimulation(command, steps: 2, step: HearthSimulation.step, time: time)
            command.commit()
            command.waitUntilCompleted()
            #expect(command.status == .completed)
            if let slot { simulation.absorbMeasurement(slot: slot, time: time) }
        }
        #expect(simulation.tiles[tile].measured > 0.2)
    }
}
