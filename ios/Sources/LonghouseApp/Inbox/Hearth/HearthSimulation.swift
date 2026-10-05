import CoreGraphics
import Foundation
import Metal
import simd

/// Where a fire draws, relative to its row's lamp cell (points, top-left
/// origin). As on the web, the drawn tile is taller than the cell and
/// bottom-aligned on it, so a flame tip fades into the row instead of meeting
/// a ceiling; the light spill reaches wider still. `bounds` covers all of it
/// and is the frame of the row's Metal layer.
struct HearthGeometry: Equatable {
    static let tileScale: CGFloat = 1.2
    static let glowWidth: CGFloat = 2.4
    static let glowHeight: CGFloat = 0.95
    static let sparkMargin: CGFloat = 4

    let tile: CGRect
    let glow: CGRect
    let bounds: CGRect

    init(cell: CGSize) {
        let height = cell.height * Self.tileScale
        let width = height * CGFloat(HearthSimulation.tileWidth) / CGFloat(HearthSimulation.tileHeight)
        let centre = cell.width / 2
        tile = CGRect(x: centre - width / 2, y: cell.height - height, width: width, height: height)
        let glowWidth = width * Self.glowWidth
        let glowTop = cell.height - height * Self.glowHeight
        glow = CGRect(x: centre - glowWidth / 2, y: glowTop, width: glowWidth, height: cell.height * 1.12 - glowTop)
        let top = min(tile.minY, glow.minY) - Self.sparkMargin
        bounds = CGRect(x: glow.minX, y: top, width: glowWidth, height: glow.maxY - top)
    }
}

/// The shared Hearth solver: one Metal atlas holding a tile per visible fire,
/// the per-tile flame controller (height slew, flare roots, stokes, sparks)
/// and the draw call that composites one tile into a layer. Platform-neutral
/// so the macOS render harness (ios/scripts/hearth-render) runs this exact code.
/// Port of web/src/shared/instruments/hearth/renderer.ts.
final class HearthSimulation {
    static let capacity = 12
    static let tileWidth = 32
    static let tileHeight = 44
    static let sourcesPerTile = 6
    static let sparkCount = 1024
    static let rootX: [Float] = [0.5, 0.3, 0.7, 0.17, 0.83]
    /// Solver step. Low Power Mode halves the step rate (and the GPU work)
    /// rather than slowing the fire down.
    static let step: Double = 1.0 / 60
    static let reducedStep: Double = 1.0 / 30
    static let settleSteps = 90
    /// A fire that comes on screen is simulated this far ahead before it is
    /// first drawn, so a glance at the timeline finds it already burning.
    static let warmupSteps = 180
    private static let warmupStepsPerFrame = 24 // even: see encodeSimulation

    private static let jacobiIterations = 20
    private static let eps0: Float = 4.0
    private static let ki: Float = 0.4
    private static let glowMax: Float = 0.085
    private static let maxSpawnsPerFrame = 96

    private static let sparkStyles: [HearthToolKind: SparkStyle] = [
        .exec: SparkStyle(count: 9, temperature: 2350, radius: 0.75, speed: 15...24, spread: 0.55, life: 1.6),
        .edit: SparkStyle(count: 6, temperature: 1650, radius: 1.6, speed: 10...15, spread: 0.9, life: 2.6),
        .read: SparkStyle(count: 3, temperature: 1350, radius: 0.55, speed: 4...8, spread: 0.5, life: 1.4),
        .agent: SparkStyle(count: 14, temperature: 1950, radius: 1.0, speed: 13...21, spread: 1.0, life: 2.0),
        .other: SparkStyle(count: 2, temperature: 1400, radius: 0.7, speed: 5...9, spread: 0.5, life: 1.2),
    ]

    private struct SparkStyle {
        let count: Int
        let temperature: Float
        let radius: Float
        let speed: ClosedRange<Float>
        let spread: Float
        let life: Float
    }

    struct Tile {
        var heat: HearthHeat?
        var puff = [Float](repeating: 0, count: HearthSimulation.rootX.count)
        var gust: Float = 0
        var height: Float = 0
        var gain: Float = 0
        var measured: Float = 0
        var budget: Float = 3
        var coldTime: Double = 0
        var frozen = true
        var seed: Float = 0
        var nextRoot = 0
        var litAt: Double = 0
        var warmSteps = 0
        /// Off screen: the fluid keeps its last state and nothing advances it.
        var parked = false
    }

    private struct Pass {
        var dt: Float
        var open: Float
        var time: Float
        var lo: UInt32
    }

    private struct Spark {
        var a: SIMD4<Float>
        var b: SIMD4<Float>
    }

    private struct Spawn {
        var spark: Spark
        var slot: SIMD4<UInt32>
    }

    private struct Draw {
        var rect: SIMD4<Float>
        var glowRect: SIMD4<Float>
        var clip: SIMD4<Float>
        var bed: SIMD4<Float>
        var glow: SIMD4<Float>
        var viewport: SIMD2<Float>
        var time: Float
        var dpr: Float
        var tile: Int32
        var light: Int32
    }

    let device: MTLDevice
    let queue: MTLCommandQueue
    private let compute: [String: MTLComputePipelineState]
    private let tilePipeline: MTLRenderPipelineState
    private let glowPipeline: MTLRenderPipelineState
    private let sparkPointPipeline: MTLRenderPipelineState
    private let sparkLinePipeline: MTLRenderPipelineState
    private let sparkPointOverPipeline: MTLRenderPipelineState
    private let sparkLineOverPipeline: MTLRenderPipelineState
    private var velocity: [MTLTexture]
    private var scalar: [MTLTexture]
    private var pressure: [MTLTexture]
    private let forward: MTLTexture
    private let backward: MTLTexture
    private let curl: MTLTexture
    private let divergence: MTLTexture
    private let blackbody: MTLTexture
    private let sparks: MTLBuffer
    private let measurements: [MTLBuffer]
    private var measureIndex = 0
    private var lastMeasureTime: Double = 0

    private(set) var tiles = [Tile](repeating: Tile(), count: HearthSimulation.capacity)
    private var clearing = Set<Int>(0..<HearthSimulation.capacity)
    private var spawns: [Spawn] = []
    private var ring = 0
    private var accumulator: Double = 0
    private(set) var simulationTime: Float = 0
    private(set) var sparkUntil: Double = 0
    /// GPU time of the last completed simulation + draw command buffer.
    private(set) var lastGPUTime: Double = 0
    private var uniforms: [SIMD4<Float>]
    private var random = SystemRandomNumberGenerator()

    static func makeLibrary(device: MTLDevice) throws -> MTLLibrary {
        // Fast math is the default, as for offline-compiled shaders.
        return try device.makeLibrary(source: HearthShaderSource.metal, options: MTLCompileOptions())
    }

    init(device: MTLDevice, library: MTLLibrary, pixelFormat: MTLPixelFormat = .bgra8Unorm) throws {
        guard let queue = device.makeCommandQueue() else { throw HearthError.unavailable }
        queue.label = "Hearth"
        self.device = device
        self.queue = queue
        var compute: [String: MTLComputePipelineState] = [:]
        for name in ["hearthClear", "hearthAdvect", "hearthMacCormack", "hearthCurl", "hearthForce", "hearthReact",
                     "hearthDivergence", "hearthPressure", "hearthProject", "hearthSparkSpawn", "hearthSparkKill",
                     "hearthSparkUpdate", "hearthMeasure"] {
            guard let function = library.makeFunction(name: name) else { throw HearthError.unavailable }
            compute[name] = try device.makeComputePipelineState(function: function)
        }
        self.compute = compute

        func pipeline(_ vertex: String, _ fragment: String, additive: Bool) throws -> MTLRenderPipelineState {
            let descriptor = MTLRenderPipelineDescriptor()
            descriptor.vertexFunction = library.makeFunction(name: vertex)
            descriptor.fragmentFunction = library.makeFunction(name: fragment)
            let attachment = descriptor.colorAttachments[0]!
            attachment.pixelFormat = pixelFormat
            attachment.isBlendingEnabled = true
            attachment.rgbBlendOperation = .add
            attachment.alphaBlendOperation = .add
            attachment.sourceRGBBlendFactor = .one
            attachment.sourceAlphaBlendFactor = .one
            attachment.destinationRGBBlendFactor = additive ? .one : .oneMinusSourceAlpha
            attachment.destinationAlphaBlendFactor = additive ? .one : .oneMinusSourceAlpha
            return try device.makeRenderPipelineState(descriptor: descriptor)
        }
        tilePipeline = try pipeline("hearthTileVertex", "hearthTileFragment", additive: false)
        glowPipeline = try pipeline("hearthGlowVertex", "hearthGlowFragment", additive: false)
        sparkPointPipeline = try pipeline("hearthSparkPointVertex", "hearthSparkPointFragment", additive: true)
        sparkLinePipeline = try pipeline("hearthSparkLineVertex", "hearthSparkLineFragment", additive: true)
        sparkPointOverPipeline = try pipeline("hearthSparkPointVertex", "hearthSparkPointFragment", additive: false)
        sparkLineOverPipeline = try pipeline("hearthSparkLineVertex", "hearthSparkLineFragment", additive: false)

        func field(_ format: MTLPixelFormat) throws -> MTLTexture {
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: format, width: Self.tileWidth * Self.capacity, height: Self.tileHeight, mipmapped: false
            )
            descriptor.storageMode = .private
            descriptor.usage = [.shaderRead, .shaderWrite]
            guard let texture = device.makeTexture(descriptor: descriptor) else { throw HearthError.unavailable }
            return texture
        }
        velocity = [try field(.rg16Float), try field(.rg16Float)]
        scalar = [try field(.rgba16Float), try field(.rgba16Float)]
        pressure = [try field(.r16Float), try field(.r16Float)]
        forward = try field(.rgba16Float)
        backward = try field(.rgba16Float)
        curl = try field(.r16Float)
        divergence = try field(.r16Float)

        let lut = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba16Float, width: 256, height: 1, mipmapped: false)
        lut.usage = .shaderRead
        guard let blackbody = device.makeTexture(descriptor: lut) else { throw HearthError.unavailable }
        let colors = HearthBlackbody.table(count: 256).map { SIMD4<Float16>($0) }
        colors.withUnsafeBytes { bytes in
            blackbody.replace(region: MTLRegionMake2D(0, 0, 256, 1), mipmapLevel: 0,
                              withBytes: bytes.baseAddress!, bytesPerRow: MemoryLayout<SIMD4<Float16>>.stride * 256)
        }
        self.blackbody = blackbody

        let zeros = [Spark](repeating: Spark(a: .zero, b: .zero), count: Self.sparkCount)
        guard let sparks = device.makeBuffer(bytes: zeros, length: MemoryLayout<Spark>.stride * Self.sparkCount,
                                             options: .storageModeShared) else { throw HearthError.unavailable }
        self.sparks = sparks
        measurements = try (0..<3).map { _ in
            guard let buffer = device.makeBuffer(length: MemoryLayout<SIMD4<Float>>.stride * Self.capacity,
                                                 options: .storageModeShared) else { throw HearthError.unavailable }
            return buffer
        }
        uniforms = [SIMD4<Float>](repeating: .zero, count: Self.capacity * (2 + Self.sourcesPerTile))
    }

    // MARK: - Tiles

    func tileIndex(of heat: HearthHeat) -> Int? {
        tiles.firstIndex { $0.heat === heat }
    }

    var freeTile: Int? { tiles.firstIndex { $0.heat == nil } }

    /// Give a free tile to a session. A row arriving on screen already
    /// burning starts part-grown, not from a spark.
    @discardableResult
    func attach(_ heat: HearthHeat, seed: Float, time: Double, reducedMotion: Bool) -> Int? {
        guard let index = freeTile else { return nil }
        var tile = Tile()
        tile.heat = heat
        tile.seed = seed
        let target = heat.target(time: time)
        tile.height = reducedMotion ? 0 : target
        tile.frozen = false
        tile.litAt = time
        if !reducedMotion && (target > 0 || heat.snapshot?.mode == .waiting) {
            tile.warmSteps = Self.warmupSteps
        }
        tiles[index] = tile
        clearing.insert(index)
        return index
    }

    func detach(_ index: Int) {
        tiles[index] = Tile()
        clearing.insert(index)
    }

    /// Park a tile whose row left the screen: its fluid state is kept as is
    /// and it stops counting as burning, so coming back shows the fire as it
    /// was instead of relighting it from bare coals.
    func setParked(_ index: Int, _ parked: Bool) {
        tiles[index].parked = parked
    }

    /// Still being simulated ahead before its first draw.
    func isWarming(_ index: Int) -> Bool {
        tiles[index].warmSteps > 0
    }

    var anyWarming: Bool {
        tiles.contains { $0.heat != nil && !$0.parked && $0.warmSteps > 0 }
    }

    // MARK: - Frame (CPU)

    /// Advance the flame controller of every attached tile. Heats are stepped
    /// here so their events land on the tile showing them; returns how many
    /// tiles are burning (anything moving that needs the loop to keep going).
    func advance(time: Double, delta: Double, wall: Date, reducedMotion: Bool) -> Int {
        var burning = 0
        let flameDecay = Float(exp(-delta / 0.35))
        let gustDecay = Float(exp(-delta / 0.6))
        let slew = Float(delta * 1.2)
        for index in tiles.indices {
            guard let heat = tiles[index].heat else { continue }
            if tiles[index].parked {
                // Advance the session (beds cool, events retire), not the fire.
                heat.step(time: time, delta: delta, wall: wall) { _ in }
                continue
            }
            if reducedMotion {
                heat.step(time: time + 3, delta: 3, wall: wall) { _ in }
            } else {
                heat.step(time: time, delta: delta, wall: wall) { [self] event in
                    emit(event, tile: index, time: time)
                }
            }
            guard let snapshot = heat.snapshot else { continue }
            var tile = tiles[index]
            var target = heat.target(time: time)
            if snapshot.mode == .waiting {
                // Guttering: a low flame that dips and recovers, calmer than work but never still.
                let t = Float(time)
                let g = 0.5 + 0.5 * sin(t * 1.3 + tile.seed * 11) * sin(t * 0.47 + tile.seed * 5)
                target = 0.2 + 0.13 * g
            }
            // Subagents are extra flame roots while the session works.
            let roots = heat.isActive(time: time) && snapshot.mode == .working
                ? min(Self.rootX.count - 1, snapshot.subagents) : 0
            if roots > 0 {
                for root in 1...roots { tile.puff[root] = max(tile.puff[root], 0.55) }
            }
            if !reducedMotion {
                for root in tile.puff.indices { tile.puff[root] *= flameDecay }
            }
            tile.gust *= gustDecay
            tile.budget = min(4, tile.budget + Float(delta) * 4)
            tile.height = reducedMotion ? target : tile.height + max(-slew, min(slew, target - tile.height))
            let busy = tile.height > 0.01 || tile.gust > 1 || tile.puff.contains { $0 > 0.05 }
            if reducedMotion {
                tile.frozen = !busy
                if busy { burning += 1 }
            } else if busy {
                burning += 1
                tile.coldTime = 0
                tile.frozen = false
            } else if !tile.frozen {
                tile.coldTime += delta
                if tile.coldTime > 4 {
                    tile.frozen = true
                    clearing.insert(index)
                }
            }
            tiles[index] = tile
        }
        return burning
    }

    private func emit(_ event: HearthEvent, tile index: Int, time: Double) {
        switch event {
        case .prompt:
            tiles[index].gust += 70
        case .message:
            break
        case .tool(let kind):
            let slot = tiles[index].nextRoot
            tiles[index].nextRoot = (slot + 1) % Self.rootX.count
            if tiles[index].budget >= 1 {
                tiles[index].puff[slot] = min(4, tiles[index].puff[slot] + (kind == .read ? 1.4 : 2.4))
                tiles[index].budget -= 1
                spawnSparks(kind: kind, tile: index, slot: slot, time: time)
            }
        }
    }

    private func spawnSparks(kind: HearthToolKind, tile index: Int, slot: Int, time: Double) {
        guard let style = Self.sparkStyles[kind] else { return }
        let x = Float(index * Self.tileWidth) + Self.rootX[slot] * Float(Self.tileWidth)
        for _ in 0..<style.count where spawns.count < Self.maxSpawnsPerFrame {
            let angle = Float.random(in: -1...1, using: &random) * style.spread
            let speed = Float.random(in: style.speed, using: &random)
            let radius = style.radius * Float.random(in: 0.75...1.25, using: &random)
            let spark = Spark(
                a: SIMD4(x + Float.random(in: -1.5...1.5, using: &random), 5.5 + Float.random(in: 0...2, using: &random),
                         sin(angle) * speed, cos(angle) * speed),
                b: SIMD4(style.temperature * Float.random(in: 0.92...1.04, using: &random),
                         style.life * Float.random(in: 0.7...1.3, using: &random), radius, Float(index))
            )
            spawns.append(Spawn(spark: spark, slot: SIMD4(UInt32(ring), 0, 0, 0)))
            ring = (ring + 1) % Self.sparkCount
        }
        sparkUntil = max(sparkUntil, time + Double(style.life) * 1.35)
    }

    /// Solver steps owed for `delta` seconds of wall time at the given step,
    /// in pairs (see `encodeSimulation`); an odd step waits for the next frame.
    func steps(for delta: Double, step: Double, maximum: Int) -> Int {
        accumulator += delta
        var count = 0
        while accumulator >= 2 * step && count + 2 <= maximum {
            accumulator -= 2 * step
            count += 2
        }
        if count + 2 > maximum { accumulator = min(accumulator, 2 * step) }
        return count
    }

    // MARK: - Frame (GPU)

    typealias Run = (lo: Int, count: Int)

    /// Consecutive tiles matching `include`, as dispatch ranges.
    private func runs(where include: (Tile) -> Bool) -> [Run] {
        var runs: [Run] = []
        for index in tiles.indices where tiles[index].heat != nil && include(tiles[index]) {
            if let last = runs.last, last.lo + last.count == index {
                runs[runs.count - 1].count += 1
            } else {
                runs.append((index, 1))
            }
        }
        return runs
    }

    /// Encode this frame's tile clears, spark spawns, `steps` solver steps and
    /// (when lit) the flame-height probe into one compute pass. Serial
    /// dispatches in one encoder are ordered and see each other's writes.
    /// Returns the probe slot written this frame; pass it to `absorbMeasurement`
    /// once the command buffer completes.
    ///
    /// Only awake tiles are stepped, so a parked or frozen tile's fluid stays
    /// exactly as it was. That holds because every frame steps an even number
    /// of times: a step swaps the velocity and pressure ping-pong textures an
    /// odd number of times, so after an even count they point where they did
    /// and the untouched tiles' state is the current state again.
    @discardableResult
    func encodeSimulation(_ command: MTLCommandBuffer, steps: Int, step: Double, time: Double) -> Int? {
        precondition(steps % 2 == 0, "Hearth steps come in pairs; see steps(for:step:maximum:)")
        packUniforms(time: time)
        let cleared = clearing
        let pendingSpawns = spawns
        spawns.removeAll()
        let awake = runs { !$0.frozen && !$0.parked && $0.warmSteps == 0 }
        let warming = runs { !$0.parked && $0.warmSteps > 0 }
        guard !cleared.isEmpty || !pendingSpawns.isEmpty || !warming.isEmpty || (!awake.isEmpty && steps > 0),
              let encoder = command.makeComputeCommandEncoder() else {
            clearing.removeAll()
            return nil
        }
        encoder.label = "Hearth simulation"
        var flags = uniforms
        for index in 0..<Self.capacity {
            flags[Self.flagsBase + index] = SIMD4(cleared.contains(index) ? 1 : 0, 0, 0, 0)
        }
        clearing.removeAll()
        flags.withUnsafeBytes { encoder.setBytes($0.baseAddress!, length: $0.count, index: 0) }
        if !cleared.isEmpty {
            encoder.setComputePipelineState(compute["hearthClear"]!)
            for (slot, texture) in (velocity + scalar + pressure + [forward, backward, curl, divergence]).enumerated() {
                encoder.setTexture(texture, index: slot)
            }
            dispatch(encoder, width: Self.tileWidth * Self.capacity, height: Self.tileHeight)
            encoder.setComputePipelineState(compute["hearthSparkKill"]!)
            encoder.setBuffer(sparks, offset: 0, index: 1)
            dispatch(encoder, width: Self.sparkCount, height: 1)
        }
        if !pendingSpawns.isEmpty {
            encoder.setComputePipelineState(compute["hearthSparkSpawn"]!)
            pendingSpawns.withUnsafeBytes { encoder.setBytes($0.baseAddress!, length: $0.count, index: 0) }
            encoder.setBuffer(sparks, offset: 0, index: 1)
            dispatch(encoder, width: pendingSpawns.count, height: 1)
            flags.withUnsafeBytes { encoder.setBytes($0.baseAddress!, length: $0.count, index: 0) }
        }
        // Warm new fires ahead on their own clock, a frame's share at a time,
        // without sparks: burning fires keep their state and their flicker.
        var warmTime = simulationTime
        var budget = 2 * Self.warmupStepsPerFrame
        for run in warming {
            let count = min(Self.warmupStepsPerFrame, tiles[run.lo].warmSteps) / 2 * 2
            guard max(2, count) <= budget else { break }
            budget -= max(2, count)
            for _ in 0..<count {
                warmTime += Float(Self.step)
                simulate(encoder, runs: [run], step: Float(Self.step), time: warmTime, withSparks: false)
            }
            for index in run.lo..<(run.lo + run.count) { tiles[index].warmSteps = max(0, tiles[index].warmSteps - max(2, count)) }
        }
        var slot: Int?
        if !awake.isEmpty, steps > 0 {
            for _ in 0..<steps {
                simulationTime += Float(step)
                simulate(encoder, runs: awake, step: Float(step), time: simulationTime)
            }
            slot = measure(encoder, runs: awake)
        }
        encoder.endEncoding()
        return slot
    }

    private func simulate(_ e: MTLComputeCommandEncoder, runs: [Run], step: Float, time: Float, withSparks: Bool = true) {
        func run(_ name: String, _ textures: [MTLTexture], dt: Float = step, open: Float = 0) {
            e.setComputePipelineState(compute[name]!)
            for (index, texture) in textures.enumerated() { e.setTexture(texture, index: index) }
            for range in runs {
                var pass = Pass(dt: dt, open: open, time: time, lo: UInt32(range.lo))
                e.setBytes(&pass, length: MemoryLayout<Pass>.stride, index: 1)
                dispatch(e, width: range.count * Self.tileWidth, height: Self.tileHeight)
            }
        }
        let v = velocity[0]
        run("hearthAdvect", [v, scalar[0], forward], open: 1)
        run("hearthAdvect", [v, forward, backward], dt: -step)
        run("hearthMacCormack", [v, scalar[0], forward, backward, scalar[1]])
        scalar.swapAt(0, 1)
        run("hearthAdvect", [v, v, velocity[1]])
        velocity.swapAt(0, 1)
        run("hearthReact", [scalar[0], scalar[1]])
        scalar.swapAt(0, 1)
        run("hearthCurl", [velocity[0], curl])
        run("hearthForce", [velocity[0], curl, scalar[0], velocity[1]])
        velocity.swapAt(0, 1)
        run("hearthDivergence", [velocity[0], divergence])
        e.setComputePipelineState(compute["hearthPressure"]!)
        e.setTexture(pressure[0], index: 0)
        e.setTexture(divergence, index: 1)
        e.setTexture(pressure[1], index: 2)
        for range in runs {
            var pass = Pass(dt: step, open: 0, time: time, lo: UInt32(range.lo))
            e.setBytes(&pass, length: MemoryLayout<Pass>.stride, index: 1)
            e.dispatchThreadgroups(MTLSize(width: range.count, height: 1, depth: 1),
                                   threadsPerThreadgroup: MTLSize(width: 256, height: 1, depth: 1))
        }
        pressure.swapAt(0, 1)
        run("hearthProject", [pressure[0], velocity[0], velocity[1]])
        velocity.swapAt(0, 1)
        guard withSparks else { return }
        var pass = Pass(dt: step, open: 0, time: time, lo: 0)
        e.setBytes(&pass, length: MemoryLayout<Pass>.stride, index: 1)
        e.setComputePipelineState(compute["hearthSparkUpdate"]!)
        e.setBuffer(sparks, offset: 0, index: 2)
        e.setTexture(velocity[0], index: 0)
        dispatch(e, width: Self.sparkCount, height: 1)
    }

    /// The probe runs once per frame on lit tiles; its result arrives a frame
    /// or two later and drives a slow integral controller on fuel, so the
    /// flame reaches the height its work asks for (renderer.ts measureAsync).
    private func measure(_ e: MTLComputeCommandEncoder, runs: [Run]) -> Int {
        let slot = measureIndex
        measureIndex = (measureIndex + 1) % measurements.count
        e.setComputePipelineState(compute["hearthMeasure"]!)
        e.setBuffer(measurements[slot], offset: 0, index: 2)
        e.setTexture(scalar[0], index: 0)
        e.setTexture(blackbody, index: 1)
        for range in runs {
            var pass = Pass(dt: 0, open: 0, time: simulationTime, lo: UInt32(range.lo))
            e.setBytes(&pass, length: MemoryLayout<Pass>.stride, index: 1)
            dispatch(e, width: range.count, height: 1)
        }
        return slot
    }

    /// Fold a completed probe into the controller (call after its command
    /// buffer completes, on the thread that owns the simulation). Results for
    /// tiles that changed hands since are harmless: the controller is slow
    /// and a fresh tile ignores the probe for its first 1.5 s.
    func absorbMeasurement(slot: Int, time: Double) {
        let dtm = Float(min(0.25, max(0, time - lastMeasureTime)))
        lastMeasureTime = time
        let values = measurements[slot].contents().bindMemory(to: SIMD4<Float>.self, capacity: Self.capacity)
        for tile in tiles.indices where tiles[tile].heat != nil && !tiles[tile].frozen && !tiles[tile].parked {
            guard time - tiles[tile].litAt > 1.5 else { continue }
            tiles[tile].measured += (values[tile].x - tiles[tile].measured) * (1 - exp(-dtm / 0.35))
            if tiles[tile].height > 0.05 {
                tiles[tile].gain = max(-1, min(1, tiles[tile].gain + Self.ki * (tiles[tile].height - tiles[tile].measured) * dtm))
            }
        }
    }

    private func dispatch(_ encoder: MTLComputeCommandEncoder, width: Int, height: Int) {
        let w = min(width, 32)
        let h = height == 1 ? 1 : 8
        encoder.dispatchThreads(MTLSize(width: width, height: height, depth: 1),
                                threadsPerThreadgroup: MTLSize(width: w, height: h, depth: 1))
    }

    private static let tilesBase = 0
    private static let sourcesBase = capacity
    private static let flagsBase = capacity + capacity * sourcesPerTile

    private func packUniforms(time: Double) {
        for index in 0..<Self.capacity {
            let tile = tiles[index]
            let surface = bedTemperatures(index, time: time).surface
            let turbulence = 0.3 + 0.7 * min(1, tile.height / 0.8)
            uniforms[Self.tilesBase + index] = SIMD4((surface - HearthHeat.ambientTemperature) / 1000, tile.gust,
                                                     Self.eps0 * turbulence, turbulence)
            let fuel: Float = tile.height > 0.02 ? min(2500, 2300 * pow(tile.height, 1.5) * exp(tile.gain)) : 0
            let base = Self.sourcesBase + index * Self.sourcesPerTile
            uniforms[base] = SIMD4(0.5, 0.08 + 0.06 * tile.height, fuel, 0.06 * fuel + 1.2 * tile.height)
            for root in Self.rootX.indices {
                uniforms[base + root + 1] = SIMD4(Self.rootX[root], 0.06, tile.puff[root] * 1.4, tile.puff[root] * 0.6)
            }
        }
    }

    private func bedTemperatures(_ index: Int, time: Double) -> (surface: Float, core: Float, ash: Float) {
        guard let heat = tiles[index].heat, let snapshot = heat.snapshot else {
            return (HearthHeat.ambientTemperature, HearthHeat.ambientTemperature, 0)
        }
        if snapshot.mode == .ended { return (HearthHeat.ambientTemperature, HearthHeat.ambientTemperature, 1) }
        return (heat.surface, heat.core, 0)
    }

    // MARK: - Compositing

    /// Draw one tile into the current render pass. `cell` is the lamp cell's
    /// rect in the target's pixels (top-left origin); the clip is the
    /// geometry's bounds, which the caller sized the layer to.
    func encodeDraw(_ encoder: MTLRenderCommandEncoder, tile index: Int, cell: CGRect, scale: CGFloat,
                    viewport: CGSize, sparks drawSparks: Bool, lightBackground: Bool = false) {
        let tile = tiles[index]
        let geometry = HearthGeometry(cell: CGSize(width: cell.width / scale, height: cell.height / scale))
        func pixels(_ rect: CGRect) -> CGRect {
            CGRect(x: cell.minX + rect.minX * scale, y: cell.minY + rect.minY * scale,
                   width: rect.width * scale, height: rect.height * scale)
        }
        func vector(_ rect: CGRect) -> SIMD4<Float> {
            SIMD4(Float(rect.minX), Float(rect.minY), Float(rect.width), Float(rect.height))
        }
        let bounds = pixels(geometry.bounds)
        let bed = bedTemperatures(index, time: 0)
        // Light spill follows the flame (its size, a flicker, a stoke), never idle coals.
        let lit = min(1, max(0, (tile.height - 0.04) / 0.7))
        let flicker = 0.85 + 0.15 * sin(simulationTime * 9.1 + tile.seed * 17) * sin(simulationTime * 5.3 + tile.seed * 3)
        let intensity = Self.glowMax * lit * flicker + min(0.03, tile.gust * 0.0006)
        var draw = Draw(
            rect: vector(pixels(geometry.tile)),
            glowRect: vector(pixels(geometry.glow)),
            clip: SIMD4(Float(bounds.minX), Float(bounds.minY), Float(bounds.maxX), Float(bounds.maxY)),
            bed: SIMD4(bed.surface, bed.core, tile.seed, bed.ash),
            glow: SIMD4(intensity, intensity * 0.5, intensity * 0.16, intensity > 0.002 ? 1 : 0),
            viewport: SIMD2(Float(viewport.width), Float(viewport.height)),
            time: simulationTime,
            dpr: Float(scale),
            tile: Int32(index),
            light: lightBackground ? 1 : 0
        )
        let size = MemoryLayout<Draw>.stride
        encoder.setVertexBytes(&draw, length: size, index: 0)
        encoder.setFragmentBytes(&draw, length: size, index: 0)
        if draw.glow.w > 0 {
            encoder.setRenderPipelineState(glowPipeline)
            encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        }
        encoder.setRenderPipelineState(tilePipeline)
        encoder.setFragmentTexture(scalar[0], index: 0)
        encoder.setFragmentTexture(velocity[0], index: 1)
        encoder.setFragmentTexture(blackbody, index: 2)
        encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        if drawSparks {
            encoder.setVertexBuffer(sparks, offset: 0, index: 1)
            encoder.setVertexTexture(blackbody, index: 0)
            encoder.setRenderPipelineState(lightBackground ? sparkLineOverPipeline : sparkLinePipeline)
            encoder.drawPrimitives(type: .line, vertexStart: 0, vertexCount: Self.sparkCount * 2)
            encoder.setRenderPipelineState(lightBackground ? sparkPointOverPipeline : sparkPointPipeline)
            encoder.drawPrimitives(type: .point, vertexStart: 0, vertexCount: Self.sparkCount)
        }
    }

    /// Test hook: a copy of one tile's combustion state (heat, fuel, soot),
    /// written by `command` into a shared buffer of half floats.
    func copyState(tile index: Int, into command: MTLCommandBuffer) -> MTLBuffer? {
        let bytesPerRow = Self.tileWidth * MemoryLayout<SIMD4<Float16>>.stride
        guard let buffer = device.makeBuffer(length: bytesPerRow * Self.tileHeight, options: .storageModeShared),
              let blit = command.makeBlitCommandEncoder() else { return nil }
        blit.copy(from: scalar[0], sourceSlice: 0, sourceLevel: 0,
                  sourceOrigin: MTLOrigin(x: index * Self.tileWidth, y: 0, z: 0),
                  sourceSize: MTLSize(width: Self.tileWidth, height: Self.tileHeight, depth: 1),
                  to: buffer, destinationOffset: 0, destinationBytesPerRow: bytesPerRow,
                  destinationBytesPerImage: bytesPerRow * Self.tileHeight)
        blit.endEncoding()
        return buffer
    }

    func recordGPUTime(_ buffer: MTLCommandBuffer) {
        let elapsed = buffer.gpuEndTime - buffer.gpuStartTime
        if elapsed > 0 { lastGPUTime = elapsed }
    }
}

enum HearthError: Error {
    case unavailable
}

/// Planck x CIE 1931 (Wyman-Sloan-Shirley multi-lobe fit) -> XYZ -> linear
/// sRGB, 400 K to 3400 K: normalised chromaticity plus log-radiance brightness.
enum HearthBlackbody {
    static func table(count: Int) -> [SIMD4<Float>] {
        func lobe(_ x: Double, _ mean: Double, _ left: Double, _ right: Double) -> Double {
            let t = (x - mean) / (x < mean ? left : right)
            return exp(-0.5 * t * t)
        }
        func xyz(_ temperature: Double) -> SIMD3<Double> {
            var sum = SIMD3<Double>.zero
            for wavelength in stride(from: 380.0, through: 780.0, by: 5) {
                let meters = wavelength * 1e-9
                let radiance: Double = 1 / (pow(meters, 5) * (exp(1.4388e-2 / (meters * temperature)) - 1))
                // Typed one lobe at a time: as one expression this timed out
                // the type checker on CI's Xcode.
                var x: Double = 1.056 * lobe(wavelength, 599.8, 37.9, 31.0)
                x += 0.362 * lobe(wavelength, 442.0, 16.0, 26.7)
                x -= 0.065 * lobe(wavelength, 501.1, 20.4, 26.2)
                var y: Double = 0.821 * lobe(wavelength, 568.8, 46.9, 40.5)
                y += 0.286 * lobe(wavelength, 530.9, 16.3, 31.1)
                var z: Double = 1.217 * lobe(wavelength, 437.0, 11.8, 36.0)
                z += 0.681 * lobe(wavelength, 459.0, 26.0, 13.8)
                sum += SIMD3<Double>(x, y, z) * radiance
            }
            return sum
        }
        let lowest = log(xyz(620).y)
        let highest = log(xyz(2400).y)
        return (0..<count).map { index in
            let c = xyz(400 + 3000 * Double(index) / Double(count - 1))
            let r: Double = max(0, 3.2406 * c.x - 1.5372 * c.y - 0.4986 * c.z)
            let g: Double = max(0, -0.9689 * c.x + 1.8758 * c.y + 0.0415 * c.z)
            let b: Double = max(0, 0.0557 * c.x - 0.204 * c.y + 1.057 * c.z)
            let m = max(r, g, b) > 0 ? max(r, g, b) : 1
            let brightness = min(1, max(0, (log(c.y) - lowest) / (highest - lowest)))
            return SIMD4(Float(r / m), Float(g / m), Float(b / m), Float(brightness))
        }
    }
}
