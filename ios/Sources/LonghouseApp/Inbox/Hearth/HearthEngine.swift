import OSLog
import QuartzCore
import UIKit

private let hearthLogger = Logger(subsystem: "ai.longhouse.ios", category: "Hearth")

/// Drives every timeline fire: one shared `HearthSimulation`, one display
/// link, and a small `CAMetalLayer` per row (`HearthLayerView`). The layers
/// live inside the rows, so scrolling, momentum and overscroll move them on
/// the compositor with no redraw; only the fire's own motion costs frames.
///
/// Energy rules (the web renderer's, plus the phone's):
/// - The loop runs only while a fire on screen is burning, has pending
///   events or live sparks. Idle coals are a still frame, redrawn every 30 s
///   as they cool. Offscreen rows, a covered timeline (pushed detail, another
///   tab) and a backgrounded app draw nothing.
/// - 30 fps presentation over a 60 Hz solver; Low Power Mode or serious
///   thermal state drops to 15 fps over a 30 Hz solver (a quarter of the
///   draws, half the solver work).
/// - Reduce Motion and critical thermal state settle a still frame per
///   change and never loop.
@MainActor
final class HearthEngine {
    static let shared = HearthEngine()

    private static let heatTimeToLive: Double = 120
    private static let coolRedraw: TimeInterval = 30
    private static let maxInFlight = 2

    private enum Phase {
        case idle
        case loading
        case ready(HearthSimulation)
        case failed
    }

    private struct Entry {
        let heat = HearthHeat()
        var orphanSince: Double?
    }

    private var phase = Phase.idle
    private let views = NSHashTable<HearthLayerView>.weakObjects()
    private var entries: [String: Entry] = [:]
    private var tileKeys = [String?](repeating: nil, count: HearthSimulation.capacity)
    private var tileHiddenSince = [Double?](repeating: nil, count: HearthSimulation.capacity)
    private var link: CADisplayLink?
    private var coolTimer: Timer?
    private var lastFrame: Double?
    private var settle = 0
    private var inFlight = 0
    private var active = UIApplication.shared.applicationState != .background
    private var observers: [NSObjectProtocol] = []
    private(set) var frames = 0

    /// False once Metal failed to start; rows then draw a static glyph.
    var isAvailable: Bool {
        if case .failed = phase { return false }
        return true
    }

    private init() {
        let center = NotificationCenter.default
        let names: [Notification.Name] = [
            UIApplication.didEnterBackgroundNotification, UIApplication.willEnterForegroundNotification,
            UIApplication.didBecomeActiveNotification, ProcessInfo.thermalStateDidChangeNotification,
            .NSProcessInfoPowerStateDidChange, UIAccessibility.reduceMotionStatusDidChangeNotification,
        ]
        for name in names {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let background = note.name == UIApplication.didEnterBackgroundNotification
                MainActor.assumeIsolated { self?.environmentChanged(background: background) }
            })
        }
    }

    // MARK: - Policy

    private var lowPower: Bool {
        ProcessInfo.processInfo.isLowPowerModeEnabled || ProcessInfo.processInfo.thermalState == .serious
    }

    private var still: Bool {
        UIAccessibility.isReduceMotionEnabled || ProcessInfo.processInfo.thermalState == .critical
    }

    private func environmentChanged(background: Bool) {
        active = !background && UIApplication.shared.applicationState != .background
        if !active {
            setRunning(false)
            return
        }
        settle = HearthSimulation.settleSteps
        lastFrame = nil
        requestFrame()
    }

    // MARK: - Views

    func register(_ view: HearthLayerView) {
        start()
        views.add(view)
        if entries[view.key] == nil { entries[view.key] = Entry() }
        entries[view.key]?.orphanSince = nil
        update(view)
    }

    func unregister(_ view: HearthLayerView) {
        views.remove(view)
        let key = view.key
        if !views.allObjects.contains(where: { $0.key == key }) {
            entries[key]?.orphanSince = CACurrentMediaTime()
        }
        requestFrame()
    }

    func update(_ view: HearthLayerView) {
        guard let snapshot = view.snapshot, let entry = entries[view.key] else { return }
        if entry.heat.snapshot.map({ !Self.sameCard($0, snapshot) }) ?? true {
            entry.heat.update(snapshot: snapshot, time: CACurrentMediaTime(), wall: Date())
            settle = HearthSimulation.settleSteps
        }
        requestFrame()
    }

    /// The heat keeps the tool name it last saw, so compare the card itself.
    private static func sameCard(_ kept: HearthSnapshot, _ next: HearthSnapshot) -> Bool {
        var kept = kept
        if next.tool == nil { kept.tool = nil }
        return kept == next
    }

    func requestFrame() {
        visibilityDirty = true
        guard active, case .ready = phase else { return }
        if link == nil {
            let link = CADisplayLink(target: HearthLinkTarget(engine: self), selector: #selector(HearthLinkTarget.tick))
            link.add(to: .main, forMode: .common)
            self.link = link
        }
        let fps = Float(lowPower ? 15 : 30)
        link?.preferredFrameRateRange = CAFrameRateRange(minimum: 15, maximum: fps, preferred: fps)
        setRunning(true)
    }

    /// Loop state changes are logged (not every frame) so QA can prove the
    /// fire stops: `scripts/ops/sim.sh logs | grep Hearth`.
    private func setRunning(_ running: Bool) {
        link?.isPaused = !running
        guard running != loopRunning else { return }
        loopRunning = running
        let tiles = tileKeys.compactMap { $0 }.count
        hearthLogger.info("Hearth loop \(running ? "running" : "paused", privacy: .public) tiles=\(tiles) frames=\(self.frames) gpu_ms=\(self.gpuTime * 1000, format: .fixed(precision: 2))")
    }

    private var loopRunning = false
    private var onScreen: [HearthLayerView] = []
    private var visibilityDirty = true

    // MARK: - Startup

    private func start() {
        guard case .idle = phase else { return }
        guard let device = MTLCreateSystemDefaultDevice() else {
            phase = .failed
            return
        }
        phase = .loading
        // Compiling the source and its pipelines takes a few hundred ms cold;
        // keep it off the main thread. The rows show nothing until it lands.
        let box = UncheckedBox(device)
        DispatchQueue.global(qos: .utility).async {
            let result: UncheckedBox<HearthSimulation>?
            do {
                let library = try HearthSimulation.makeLibrary(device: box.value)
                result = UncheckedBox(try HearthSimulation(device: box.value, library: library))
            } catch {
                hearthLogger.error("Hearth unavailable: \(String(describing: error), privacy: .public)")
                result = nil
            }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { HearthEngine.shared.started(result?.value) }
            }
        }
    }

    private func started(_ simulation: HearthSimulation?) {
        guard let simulation else {
            phase = .failed
            for view in views.allObjects { view.engineFailed() }
            return
        }
        phase = .ready(simulation)
        coolTimer = Timer.scheduledTimer(withTimeInterval: Self.coolRedraw, repeats: true) { _ in
            MainActor.assumeIsolated {
                HearthEngine.shared.settle = HearthSimulation.settleSteps
                HearthEngine.shared.requestFrame()
            }
        }
        coolTimer?.tolerance = 5
        requestFrame()
    }

    // MARK: - Frame

    fileprivate func tick() {
        guard case .ready(let simulation) = phase, active else {
            setRunning(false)
            return
        }
        guard inFlight < Self.maxInFlight else { return }
        let now = CACurrentMediaTime()
        let wall = Date()
        let delta = min(0.1, lastFrame.map { now - $0 } ?? 1.0 / 30)
        lastFrame = now
        let still = still
        // Which rows are on screen changes only with scrolling; a quarter-second
        // answer is enough (tiles linger 4 s, new rows wake us on arrival), and
        // the window conversion is the loop's largest CPU cost per frame.
        if visibilityDirty || frames % 8 == 0 {
            onScreen = views.allObjects.filter(\.isOnScreen)
            visibilityDirty = false
        }
        let onScreen = onScreen.filter { $0.window != nil }
        assignTiles(simulation, onScreen: onScreen, now: now, still: still)

        // Heats with no tile still advance (their beds cool, events retire).
        for (key, entry) in entries where !tileKeys.contains(key) {
            entry.heat.step(time: now, delta: delta, wall: wall) { _ in }
        }
        let burning = simulation.advance(time: now, delta: delta, wall: wall, reducedMotion: still)
        let step = lowPower ? HearthSimulation.reducedStep : HearthSimulation.step
        let steps: Int
        if still {
            // Settle a still frame a few steps per frame, then draw it once.
            steps = min(14, settle)
            settle -= steps
        } else {
            steps = burning > 0 ? simulation.steps(for: delta, step: step, maximum: lowPower ? 4 : 2) : 0
        }

        guard let command = simulation.queue.makeCommandBuffer() else { return }
        command.label = "Timeline Hearth"
        let slot = simulation.encodeSimulation(command, steps: steps, step: step, time: now)
        if !still || settle == 0 {
            for view in onScreen { draw(view, simulation: simulation, command: command, sparks: !still) }
        }
        inFlight += 1
        command.addCompletedHandler { buffer in
            let gpu = buffer.gpuEndTime - buffer.gpuStartTime
            DispatchQueue.main.async {
                MainActor.assumeIsolated { HearthEngine.shared.completed(slot: slot, gpu: gpu) }
            }
        }
        command.commit()
        frames += 1

        let pending = tileKeys.contains { key in key.flatMap { entries[$0]?.heat.hasPending } ?? false }
        let keepGoing = still ? settle > 0 : burning > 0 || pending || now < simulation.sparkUntil
        if !keepGoing {
            setRunning(false)
            lastFrame = nil
        }
        retireHeats(now: now)
    }

    private func completed(slot: Int?, gpu: Double) {
        inFlight -= 1
        guard case .ready(let simulation) = phase else { return }
        if let slot { simulation.absorbMeasurement(slot: slot, time: CACurrentMediaTime()) }
        if gpu > 0 { gpuTime = gpu }
    }

    private(set) var gpuTime: Double = 0

    /// Rows on screen get a tile. A row that leaves the screen (scrolled
    /// away, timeline covered) parks its tile: the fire's state is kept and
    /// nothing advances it, so coming back shows it burning as it was. Parked
    /// tiles are given up only when the atlas needs room, oldest first.
    private func assignTiles(_ simulation: HearthSimulation, onScreen: [HearthLayerView], now: Double, still: Bool) {
        let visibleKeys = Set(onScreen.map(\.key))
        for index in tileKeys.indices {
            guard let key = tileKeys[index] else { continue }
            if entries[key] == nil {
                release(index, simulation)
            } else if visibleKeys.contains(key) {
                tileHiddenSince[index] = nil
                simulation.setParked(index, false)
            } else if tileHiddenSince[index] == nil {
                tileHiddenSince[index] = now
                simulation.setParked(index, true)
            }
        }
        for key in visibleKeys where !tileKeys.contains(key) {
            guard let heat = entries[key]?.heat, heat.snapshot != nil else { continue }
            if simulation.freeTile == nil {
                let parked = tileKeys.indices.filter { tileKeys[$0] != nil && tileHiddenSince[$0] != nil }
                guard let victim = parked.min(by: { tileHiddenSince[$0]! < tileHiddenSince[$1]! }) else { break }
                release(victim, simulation)
            }
            guard let index = simulation.attach(heat, seed: Self.seed(key), time: now, reducedMotion: still) else { break }
            tileKeys[index] = key
            tileHiddenSince[index] = nil
            settle = HearthSimulation.settleSteps
        }
    }

    private func release(_ index: Int, _ simulation: HearthSimulation) {
        simulation.detach(index)
        tileKeys[index] = nil
        tileHiddenSince[index] = nil
    }

    private func retireHeats(now: Double) {
        for (key, entry) in entries {
            if let since = entry.orphanSince, now - since > Self.heatTimeToLive {
                entries[key] = nil
            }
        }
    }

    private func draw(_ view: HearthLayerView, simulation: HearthSimulation, command: MTLCommandBuffer, sparks: Bool) {
        let layer = view.metalLayer
        // A fire still being simulated ahead keeps the layer as it is (empty
        // for a new row) for the few frames that takes.
        var tile = tileKeys.firstIndex(of: view.key)
        if let warming = tile, simulation.isWarming(warming) {
            // Nothing to show yet; a layer still holding another fire clears.
            guard view.drawnTile != nil else { return }
            tile = nil
        }
        // A row without a tile (atlas full) clears once rather than keep a frame.
        guard tile != nil || view.drawnTile != nil else { return }
        guard layer.drawableSize.width > 0, let drawable = layer.nextDrawable() else { return }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = drawable.texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 0)
        guard let encoder = command.makeRenderCommandEncoder(descriptor: pass) else { return }
        if let tile {
            let geometry = HearthGeometry(cell: view.cell)
            let scale = layer.contentsScale
            let cell = CGRect(x: -geometry.bounds.minX * scale, y: -geometry.bounds.minY * scale,
                              width: view.cell.width * scale, height: view.cell.height * scale)
            simulation.encodeDraw(encoder, tile: tile, cell: cell, scale: scale, viewport: layer.drawableSize,
                                  sparks: sparks, lightBackground: view.lightBackground)
        }
        encoder.endEncoding()
        command.present(drawable)
        view.drawnTile = tile
    }

    private static func seed(_ key: String) -> Float {
        let hash = key.utf8.reduce(UInt32(2_166_136_261)) { ($0 ^ UInt32($1)) &* 16_777_619 }
        return Float(hash % 10_000) / 10_000
    }
}

/// CADisplayLink retains its target; this keeps the engine out of that cycle.
private final class HearthLinkTarget: NSObject {
    weak var engine: HearthEngine?

    init(engine: HearthEngine) {
        self.engine = engine
    }

    @MainActor @objc func tick() {
        engine?.tick()
    }
}

private struct UncheckedBox<Value>: @unchecked Sendable {
    let value: Value
    init(_ value: Value) { self.value = value }
}

/// One row's fire: a transparent Metal layer covering the lamp cell plus the
/// flame tip, sparks and light spill around it (`HearthGeometry.bounds`).
final class HearthLayerView: UIView {
    override class var layerClass: AnyClass { CAMetalLayer.self }

    var metalLayer: CAMetalLayer { layer as! CAMetalLayer }
    var key = ""
    var cell = CGSize(width: 34, height: 46)
    var snapshot: HearthSnapshot?
    var lightBackground = false
    var drawnTile: Int?
    var onFailure: (() -> Void)?
    private var registered = false

    init() {
        super.init(frame: .zero)
        isUserInteractionEnabled = false
        isOpaque = false
        backgroundColor = .clear
        let layer = metalLayer
        layer.device = MTLCreateSystemDefaultDevice()
        layer.pixelFormat = .bgra8Unorm
        layer.isOpaque = false
        layer.framebufferOnly = true
        // The web caps its canvas at 2x; the fire is soft and a 3x layer
        // would cost 2.25x the fragments for nothing visible.
        layer.contentsScale = min(2, UIScreen.main.scale)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    /// On screen (with a margin) in a window that is showing.
    var isOnScreen: Bool {
        guard let window, !isHidden, window.windowScene?.activationState != .background else { return false }
        let frame = convert(bounds, to: window)
        return frame.intersects(window.bounds.insetBy(dx: 0, dy: -48))
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let scale = metalLayer.contentsScale
        let size = CGSize(width: (bounds.width * scale).rounded(), height: (bounds.height * scale).rounded())
        if metalLayer.drawableSize != size {
            metalLayer.drawableSize = size
            drawnTile = nil
            if registered { HearthEngine.shared.requestFrame() }
        }
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil, !registered, !key.isEmpty {
            registered = true
            HearthEngine.shared.register(self)
        } else if window == nil, registered {
            registered = false
            HearthEngine.shared.unregister(self)
        }
    }

    func apply(key: String, snapshot: HearthSnapshot, lightBackground: Bool) {
        let rekeyed = key != self.key
        if rekeyed && registered {
            registered = false
            HearthEngine.shared.unregister(self)
        }
        self.key = key
        self.snapshot = snapshot
        if self.lightBackground != lightBackground {
            self.lightBackground = lightBackground
            // Redraw for the new appearance; a layer with content clears if it has no tile.
            if drawnTile != nil { drawnTile = -1 }
        }
        // A layer that may still hold another session's fire clears first.
        if rekeyed { drawnTile = -1 }
        if window != nil && !registered {
            registered = true
            HearthEngine.shared.register(self)
        } else if registered {
            HearthEngine.shared.update(self)
        }
    }

    func engineFailed() {
        onFailure?()
    }
}
