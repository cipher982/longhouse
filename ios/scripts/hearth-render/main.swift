// Offscreen render harness for the iOS Hearth: runs the app's own
// HearthHeat + HearthSimulation + shader source on this Mac's GPU, on a
// deterministic clock, and writes an MP4, PNG stills and frame timings.
// Built and run by ios/scripts/hearth-render.sh; see --help there.

import CoreGraphics
import Foundation
import ImageIO
import Metal
import UniformTypeIdentifiers

struct Options {
    var out = "/tmp/agents/hearth-render"
    var seconds = 12.0
    var warmup = 4.0
    var fps = 30
    var scale: CGFloat = 3
    var light = false
    var lowPower = false
    var reducedMotion = false
    var benchTiles = 0
    var label = "native"
}

var options = Options()
var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--out": options.out = arguments.next()!
    case "--seconds": options.seconds = Double(arguments.next()!)!
    case "--warmup": options.warmup = Double(arguments.next()!)!
    case "--fps": options.fps = Int(arguments.next()!)!
    case "--scale": options.scale = CGFloat(Double(arguments.next()!)!)
    case "--light": options.light = true
    case "--low-power": options.lowPower = true
    case "--reduced-motion": options.reducedMotion = true
    case "--bench": options.benchTiles = Int(arguments.next()!)!
    case "--label": options.label = arguments.next()!
    default:
        FileHandle.standardError.write("unknown argument \(argument)\n".data(using: .utf8)!)
        exit(2)
    }
}

// MARK: - Scenarios

struct Scenario {
    let name: String
    var snapshot: HearthSnapshot
    /// Seconds between card updates that add work; nil for a still card.
    let tick: Double?
    let tools: ClosedRange<Int>
    let names: [String]
}

let wallBase = Date(timeIntervalSince1970: 1_790_000_000)
let clockBase = 1000.0
func wall(_ time: Double) -> Date { wallBase.addingTimeInterval(time - clockBase) }

func makeScenarios(at time: Double) -> [Scenario] {
    let now = wall(time)
    let hourAgo = now.addingTimeInterval(-3600)
    if options.benchTiles > 0 {
        return (0..<options.benchTiles).map { index in
            Scenario(name: "bench-\(index)",
                     snapshot: HearthSnapshot(mode: .working, toolCalls: 400, assistantMessages: 200, userMessages: 10,
                                              subagents: index % 3, lastActivity: now, started: hourAgo),
                     tick: 1.2, tools: 1...3, names: ["Bash", "Edit", "Read", "Task"])
        }
    }
    return [
        Scenario(name: "working-busy",
                 snapshot: HearthSnapshot(mode: .working, toolCalls: 400, assistantMessages: 200, userMessages: 10,
                                          lastActivity: now, started: hourAgo),
                 tick: 1.2, tools: 1...3, names: ["Bash", "Edit", "Read", "Task"]),
        Scenario(name: "working-pilot",
                 snapshot: HearthSnapshot(mode: .working, lastActivity: now, started: now.addingTimeInterval(-30)),
                 tick: nil, tools: 0...0, names: []),
        Scenario(name: "working-subagents",
                 snapshot: HearthSnapshot(mode: .working, toolCalls: 20, assistantMessages: 10, userMessages: 2,
                                          subagents: 2, lastActivity: now, started: now.addingTimeInterval(-600)),
                 tick: 2.4, tools: 1...1, names: ["Read", "Edit"]),
        Scenario(name: "waiting",
                 snapshot: HearthSnapshot(mode: .waiting, toolCalls: 50, lastActivity: now.addingTimeInterval(-30),
                                          started: hourAgo),
                 tick: nil, tools: 0...0, names: []),
        Scenario(name: "idle-recent",
                 snapshot: HearthSnapshot(mode: .idle, toolCalls: 50, lastActivity: now.addingTimeInterval(-180),
                                          started: hourAgo),
                 tick: nil, tools: 0...0, names: []),
        Scenario(name: "idle-old",
                 snapshot: HearthSnapshot(mode: .idle, toolCalls: 50, lastActivity: now.addingTimeInterval(-2400),
                                          started: hourAgo),
                 tick: nil, tools: 0...0, names: []),
        Scenario(name: "ended",
                 snapshot: HearthSnapshot(mode: .ended, toolCalls: 50, lastActivity: now.addingTimeInterval(-600),
                                          started: hourAgo),
                 tick: nil, tools: 0...0, names: []),
    ]
}

// MARK: - Setup

guard let device = MTLCreateSystemDefaultDevice() else { fatalError("no Metal device") }
let compileStart = Date()
let library = try HearthSimulation.makeLibrary(device: device)
let compileSeconds = Date().timeIntervalSince(compileStart)
let simulation = try HearthSimulation(device: device, library: library)

var time = clockBase
var scenarios = makeScenarios(at: time)
let heats = scenarios.map { _ in HearthHeat() }
var ticks = [Int](repeating: 0, count: scenarios.count)
for (index, scenario) in scenarios.enumerated() {
    heats[index].update(snapshot: scenario.snapshot, time: time, wall: wall(time))
    let seed = Float((index * 37 + 11) % 100) / 100
    simulation.attach(heats[index], seed: seed, time: time, reducedMotion: options.reducedMotion)
}

// Layout: one card per row, the lamp cell at the card's trailing edge, the
// way TimelineSessionCardRow places it (34 x 46 pt).
let cell = CGSize(width: 34, height: 46)
let rowHeight: CGFloat = options.benchTiles > 0 ? 64 : 74
let columns = options.benchTiles > 6 ? 2 : 1
let rows = Int(ceil(Double(scenarios.count) / Double(columns)))
let columnWidth: CGFloat = options.benchTiles > 0 ? 110 : 160
let canvasPoints = CGSize(width: columnWidth * CGFloat(columns), height: rowHeight * CGFloat(rows) + 8)
let pixelWidth = Int(canvasPoints.width * options.scale)
let pixelHeight = Int(canvasPoints.height * options.scale)
func cellRect(_ index: Int) -> CGRect {
    let column = index / rows
    let row = index % rows
    return CGRect(x: (CGFloat(column) * columnWidth + columnWidth - 34 - 40) * options.scale,
                  y: (4 + CGFloat(row) * rowHeight + (rowHeight - cell.height) / 2) * options.scale,
                  width: cell.width * options.scale, height: cell.height * options.scale)
}

// Ember.card: dark 0x171210, light 0xFBF6EC.
let backgroundBytes: [Double] = options.light ? [0xFB, 0xF6, 0xEC] : [0x17, 0x12, 0x10]
let background = (backgroundBytes[0] / 255, backgroundBytes[1] / 255, backgroundBytes[2] / 255)

let targetDescriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: pixelWidth,
                                                                height: pixelHeight, mipmapped: false)
targetDescriptor.usage = [.renderTarget]
targetDescriptor.storageMode = .shared
let target = device.makeTexture(descriptor: targetDescriptor)!

try FileManager.default.createDirectory(atPath: options.out, withIntermediateDirectories: true)
let recording = options.benchTiles == 0
var ffmpeg: Process?
var ffmpegInput: FileHandle?
if recording {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ffmpeg")
    process.arguments = ["-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
                         "-s", "\(pixelWidth)x\(pixelHeight)", "-r", "\(options.fps)", "-i", "-",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
                         "\(options.out)/\(options.label).mp4"]
    let pipe = Pipe()
    process.standardInput = pipe
    try process.run()
    ffmpeg = process
    ffmpegInput = pipe.fileHandleForWriting
}

func writePNG(_ bytes: [UInt8], name: String) {
    let provider = CGDataProvider(data: Data(bytes) as CFData)!
    let image = CGImage(width: pixelWidth, height: pixelHeight, bitsPerComponent: 8, bitsPerPixel: 32,
                        bytesPerRow: pixelWidth * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue
                            | CGBitmapInfo.byteOrder32Little.rawValue),
                        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
    let url = URL(fileURLWithPath: "\(options.out)/\(name).png")
    let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(destination, image, nil)
    CGImageDestinationFinalize(destination)
}

// MARK: - Loop

let frameDelta = 1.0 / Double(options.fps)
let step = options.lowPower ? HearthSimulation.reducedStep : HearthSimulation.step
let totalFrames = Int((options.warmup + options.seconds) * Double(options.fps))
let warmupFrames = Int(options.warmup * Double(options.fps))
var gpuTimes: [Double] = []
var cpuTimes: [Double] = []
var stepCounts: [Int] = []
var burningCounts: [Int] = []
var settle = options.reducedMotion ? HearthSimulation.settleSteps : 0
var bytes = [UInt8](repeating: 0, count: pixelWidth * pixelHeight * 4)
let stills = Set([0, (totalFrames - warmupFrames) / 2, totalFrames - warmupFrames - 1])

for frame in 0..<totalFrames {
    time += frameDelta
    let now = wall(time)
    // Card updates: counters grow on each scenario's tick.
    for index in scenarios.indices {
        guard let tick = scenarios[index].tick else { continue }
        let due = Int((time - clockBase) / tick)
        guard due > ticks[index] else { continue }
        ticks[index] = due
        var snapshot = scenarios[index].snapshot
        snapshot.toolCalls += Int.random(in: scenarios[index].tools)
        snapshot.assistantMessages += 1
        snapshot.tool = scenarios[index].names[due % scenarios[index].names.count]
        snapshot.lastActivity = now
        scenarios[index].snapshot = snapshot
        heats[index].update(snapshot: snapshot, time: time, wall: now)
    }

    let cpuStart = DispatchTime.now().uptimeNanoseconds
    let burning = simulation.advance(time: time, delta: frameDelta, wall: now, reducedMotion: options.reducedMotion)
    let steps: Int
    if options.reducedMotion {
        steps = min(15, settle)
        settle -= steps
    } else {
        steps = burning > 0 ? simulation.steps(for: frameDelta, step: step, maximum: options.lowPower ? 4 : 3) : 0
    }
    let command = simulation.queue.makeCommandBuffer()!
    let slot = simulation.encodeSimulation(command, steps: steps, step: step, time: time)
    let pass = MTLRenderPassDescriptor()
    pass.colorAttachments[0].texture = target
    pass.colorAttachments[0].loadAction = .clear
    pass.colorAttachments[0].storeAction = .store
    pass.colorAttachments[0].clearColor = MTLClearColorMake(background.0, background.1, background.2, 1)
    let encoder = command.makeRenderCommandEncoder(descriptor: pass)!
    for index in scenarios.indices {
        guard let tile = simulation.tileIndex(of: heats[index]), !simulation.isWarming(tile) else { continue }
        simulation.encodeDraw(encoder, tile: tile, cell: cellRect(index), scale: options.scale,
                              viewport: CGSize(width: pixelWidth, height: pixelHeight),
                              sparks: !options.reducedMotion, lightBackground: options.light)
    }
    encoder.endEncoding()
    let cpuEnd = DispatchTime.now().uptimeNanoseconds
    command.commit()
    command.waitUntilCompleted()
    simulation.recordGPUTime(command)
    if let slot { simulation.absorbMeasurement(slot: slot, time: time) }

    guard frame >= warmupFrames else { continue }
    cpuTimes.append(Double(cpuEnd - cpuStart) / 1e6)
    gpuTimes.append(simulation.lastGPUTime * 1000)
    stepCounts.append(steps)
    burningCounts.append(burning)
    if recording {
        target.getBytes(&bytes, bytesPerRow: pixelWidth * 4,
                        from: MTLRegionMake2D(0, 0, pixelWidth, pixelHeight), mipmapLevel: 0)
        ffmpegInput!.write(Data(bytes))
        let index = frame - warmupFrames
        if stills.contains(index) { writePNG(bytes, name: "\(options.label)-f\(String(format: "%04d", index))") }
    }
}

ffmpegInput?.closeFile()
ffmpeg?.waitUntilExit()

func percentile(_ values: [Double], _ p: Double) -> Double {
    guard !values.isEmpty else { return 0 }
    let sorted = values.sorted()
    return sorted[min(sorted.count - 1, Int(Double(sorted.count - 1) * p))]
}

let stats: [String: Any] = [
    "label": options.label,
    "device": device.name,
    "tiles": scenarios.count,
    "fps": options.fps,
    "step_hz": Int((1 / step).rounded()),
    "low_power": options.lowPower,
    "reduced_motion": options.reducedMotion,
    "scale": Double(options.scale),
    "pixels": [pixelWidth, pixelHeight],
    "frames": gpuTimes.count,
    "shader_compile_ms": compileSeconds * 1000,
    "gpu_ms_mean": gpuTimes.reduce(0, +) / Double(max(1, gpuTimes.count)),
    "gpu_ms_p95": percentile(gpuTimes, 0.95),
    "cpu_encode_ms_mean": cpuTimes.reduce(0, +) / Double(max(1, cpuTimes.count)),
    "cpu_encode_ms_p95": percentile(cpuTimes, 0.95),
    "steps_per_frame_mean": Double(stepCounts.reduce(0, +)) / Double(max(1, stepCounts.count)),
    "burning_mean": Double(burningCounts.reduce(0, +)) / Double(max(1, burningCounts.count)),
]
let json = try JSONSerialization.data(withJSONObject: stats, options: [.prettyPrinted, .sortedKeys])
try json.write(to: URL(fileURLWithPath: "\(options.out)/\(options.label)-stats.json"))
print(String(data: json, encoding: .utf8)!)
