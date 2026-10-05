import Foundation

enum HearthMode: Equatable {
    case working
    case waiting
    case idle
    case ended
}

enum HearthToolKind: Equatable {
    case exec
    case edit
    case read
    case agent
    case other

    init(name: String?) {
        let normalized = (name ?? "").lowercased()
            .replacingOccurrences(of: #"^mcp__[^_]+__"#, with: "", options: .regularExpression)
        if ["bash", "shell", "exec", "run", "terminal", "command"].contains(where: normalized.hasPrefix) {
            self = .exec
        } else if ["edit", "write", "multiedit", "notebookedit", "apply_patch", "patch", "str_replace"].contains(where: normalized.hasPrefix) {
            self = .edit
        } else if ["read", "grep", "glob", "find", "search", "ls", "view", "webfetch", "web_fetch", "fetch"].contains(where: normalized.hasPrefix) {
            self = .read
        } else if ["agent", "task", "spawn", "hatch"].contains(where: normalized.hasPrefix) {
            self = .agent
        } else {
            self = .other
        }
    }
}

struct HearthSnapshot: Equatable {
    struct ChildCounters: Equatable {
        var toolCalls: Int?
        var assistantMessages: Int?
        var userMessages: Int?
    }

    var mode: HearthMode
    var toolCalls: Int
    var assistantMessages: Int
    var userMessages: Int
    var subagents: Int
    var children: [String: ChildCounters]
    var tool: String?
    var lastActivity: Date?
    var started: Date?
    /// False while the card cannot be trusted (offline, unknown state): counter
    /// changes then rebase instead of firing events.
    var acceptsEvents = true

    init(
        mode: HearthMode,
        toolCalls: Int = 0,
        assistantMessages: Int = 0,
        userMessages: Int = 0,
        subagents: Int = 0,
        children: [String: ChildCounters] = [:],
        tool: String? = nil,
        lastActivity: Date? = nil,
        started: Date? = nil
    ) {
        self.mode = mode
        self.toolCalls = toolCalls
        self.assistantMessages = assistantMessages
        self.userMessages = userMessages
        self.subagents = subagents
        self.children = children
        self.tool = tool
        self.lastActivity = lastActivity
        self.started = started
    }
}

enum HearthEvent: Equatable {
    case tool(HearthToolKind)
    case message
    case prompt
}

final class HearthHeat {
    static let ambientTemperature: Float = 300
    static let hotTemperature: Float = 1350
    static let litTemperature: Float = 1150
    static let waitingTemperature: Float = 1020
    static let surfaceCoolingTime: TimeInterval = 1200
    static let coreCoolingTime: TimeInterval = 10800
    static let heightFloor: Float = 0.42
    static let heightTop: Float = 0.8

    private struct Deposit {
        let end: TimeInterval
        let rate: Float
    }

    private struct Pending {
        let at: TimeInterval
        let event: HearthEvent
    }

    private(set) var snapshot: HearthSnapshot?
    private(set) var surface = HearthHeat.ambientTemperature
    private(set) var core = HearthHeat.ambientTemperature
    private(set) var work: Float = 0
    private var surfaceBaseline = HearthHeat.ambientTemperature
    private var coreBaseline = HearthHeat.ambientTemperature
    private var deposits: [Deposit] = []
    private var pending: [Pending] = []
    private var lastEventTime = -TimeInterval.infinity
    private var lastUpdateTime = -TimeInterval.infinity
    private var lastEventWall: Date?
    private var wasActive = false

    func update(snapshot next: HearthSnapshot, time: TimeInterval, wall: Date) {
        let previous = snapshot
        var retained = next
        if retained.tool == nil { retained.tool = previous?.tool }
        snapshot = retained
        guard let previous else {
            surfaceBaseline = next.mode == .ended ? Self.ambientTemperature : Self.litTemperature
            coreBaseline = surfaceBaseline
            if next.mode == .working {
                surface = Self.litTemperature
                core = Self.litTemperature
                work = 18 + densityWork(next)
            } else {
                let bed = initialBed(next, wall: wall)
                surface = bed.surface
                core = bed.core
            }
            wasActive = next.mode == .working
            lastUpdateTime = time
            return
        }

        let spread = max(0.3, min(2.5, time - lastUpdateTime))
        lastUpdateTime = time
        guard next.mode != .ended, next.acceptsEvents else {
            pending.removeAll()
            deposits.removeAll()
            lastEventTime = -.infinity
            work = 0
            if next.mode == .ended {
                surface = Self.ambientTemperature
                core = Self.ambientTemperature
                surfaceBaseline = surface
                coreBaseline = core
                lastEventWall = nil
                wasActive = false
            } else if wasActive {
                stopHeating(wall: wall)
            }
            return
        }

        var tools = increase(previous.toolCalls, next.toolCalls)
        var messages = increase(previous.assistantMessages, next.assistantMessages)
        var prompts = increase(previous.userMessages, next.userMessages)
        for (childID, current) in next.children {
            guard let baseline = previous.children[childID] else { continue }
            tools += increase(baseline.toolCalls, current.toolCalls)
            messages += increase(baseline.assistantMessages, current.assistantMessages)
            prompts += increase(baseline.userMessages, current.userMessages)
        }

        prompts = min(prompts, 24)
        tools = min(tools, 24 - prompts)
        messages = min(messages, 24 - prompts - tools)
        let kind = HearthToolKind(name: retained.tool)
        let credited = Array(repeating: HearthEvent.prompt, count: prompts)
            + Array(repeating: HearthEvent.tool(kind), count: tools)
            + Array(repeating: HearthEvent.message, count: messages)
        var shown = Array(credited.indices)
        if credited.count > 8 {
            shown = Array(0..<prompts)
            let rest = Array(prompts..<credited.count)
            let room = max(0, 8 - prompts)
            for index in 0..<room {
                shown.append(rest[index * rest.count / room])
            }
        }
        let visibleIndices = Set(shown)
        for (offset, index) in shown.enumerated() {
            pending.append(Pending(
                at: time + spread * Double(offset) / Double(max(1, shown.count)),
                event: credited[index]
            ))
        }
        if pending.count > 24 { pending.removeFirst(pending.count - 24) }
        for index in credited.indices where !visibleIndices.contains(index) {
            deposit(credited[index], time: time)
        }
        if !credited.isEmpty { lastEventWall = wall }
        if wasActive && !isActive(time: time) { stopHeating(wall: wall) }
    }

    func step(time: TimeInterval, delta: TimeInterval, wall: Date, emit: (HearthEvent) -> Void) {
        guard let snapshot else { return }
        let due = pending.filter { $0.at <= time && time - $0.at < 5 }
        pending.removeAll { $0.at <= time }
        for item in due {
            deposit(item.event, time: time)
            lastEventTime = time
            lastEventWall = wall
            switch item.event {
            case .prompt: surface += 0.3 * (Self.hotTemperature - surface)
            case .tool: surface += 0.03 * (Self.hotTemperature - surface)
            case .message: break
            }
            emit(item.event)
        }
        deposits.removeAll { $0.end <= time }
        let rate = deposits.reduce(Float(0)) { $0 + $1.rate }
        let active = isActive(time: time)
        let base: Float = active && snapshot.mode == .working ? 18 + densityWork(snapshot) : 0
        let elapsed = max(0, delta)
        work += (rate + base - work) * Float(1 - exp(-elapsed / 8))

        if snapshot.mode == .ended {
            surface = Self.ambientTemperature
            core = Self.ambientTemperature
            surfaceBaseline = surface
            coreBaseline = core
            wasActive = false
        } else if active {
            let power = 0.35 + min(0.6, Double(rate) / 200)
            surface += (Self.hotTemperature - surface) * Float(1 - exp(-power * elapsed / 25))
            if surface > core {
                core += (surface - core) * Float(1 - exp(-elapsed / 300))
            }
            wasActive = true
        } else {
            if wasActive {
                stopHeating(wall: lastEventWall.map { min(wall, $0.addingTimeInterval(5)) } ?? wall)
            }
            let bed = coolBed(wall: wall)
            surface = bed.surface
            core = bed.core
        }
    }

    func target(time: TimeInterval) -> Float {
        guard isActive(time: time) else { return 0 }
        return Self.heightFloor + (Self.heightTop - Self.heightFloor)
            * Float(1 - exp(-Double(max(0, work)) / 60))
    }

    var hasPending: Bool { !pending.isEmpty }

    func isActive(time: TimeInterval) -> Bool {
        guard let snapshot, snapshot.mode != .ended, snapshot.acceptsEvents else { return false }
        return snapshot.mode == .working || pending.contains { time < $0.at + 5 }
            || deposits.contains { $0.end > time } || time - lastEventTime < 5
    }

    private func increase(_ previous: Int?, _ next: Int?) -> Int {
        guard let previous, let next else { return 0 }
        return next > previous ? next - previous : 0
    }

    private func densityWork(_ snapshot: HearthSnapshot) -> Float {
        guard let started = snapshot.started, let lastActivity = snapshot.lastActivity else { return 0 }
        let span = lastActivity.timeIntervalSince(started)
        guard span >= 60, snapshot.toolCalls > 0 else { return 0 }
        return Float(min(90, Double(snapshot.toolCalls) / span * 75))
    }

    private func deposit(_ event: HearthEvent, time: TimeInterval) {
        switch event {
        case .tool: deposits.append(Deposit(end: time + 4, rate: 150 / 4))
        case .message: deposits.append(Deposit(end: time + 4, rate: 90 / 4))
        case .prompt: break
        }
        if deposits.count > 24 { deposits.removeFirst(deposits.count - 24) }
    }

    private func stopHeating(wall: Date) {
        surfaceBaseline = surface
        coreBaseline = max(core, surface)
        wasActive = false
        lastEventWall = max(lastEventWall ?? wall, wall)
    }

    private func cool(_ temperature: Float, seconds: TimeInterval, tau: TimeInterval) -> Float {
        Self.ambientTemperature + (temperature - Self.ambientTemperature) * Float(exp(-max(0, seconds) / tau))
    }

    private func initialBed(_ snapshot: HearthSnapshot, wall: Date) -> (surface: Float, core: Float) {
        guard snapshot.mode != .ended, let lastActivity = snapshot.lastActivity else {
            return (Self.ambientTemperature, Self.ambientTemperature)
        }
        let idle = wall.timeIntervalSince(lastActivity)
        let surface = cool(Self.litTemperature, seconds: idle, tau: Self.surfaceCoolingTime)
        let core = cool(Self.litTemperature, seconds: idle, tau: Self.coreCoolingTime)
        if snapshot.mode == .waiting {
            return (max(surface, Self.waitingTemperature), max(core, Self.waitingTemperature))
        }
        return (surface, core)
    }

    private func coolBed(wall: Date) -> (surface: Float, core: Float) {
        let last = [snapshot?.lastActivity, lastEventWall].compactMap { $0 }.max()
        let idle = last.map { wall.timeIntervalSince($0) } ?? .infinity
        let surface = cool(surfaceBaseline, seconds: idle, tau: Self.surfaceCoolingTime)
        let core = cool(coreBaseline, seconds: idle, tau: Self.coreCoolingTime)
        if snapshot?.mode == .waiting {
            return (max(surface, Self.waitingTemperature), max(core, Self.waitingTemperature))
        }
        return (surface, core)
    }
}
