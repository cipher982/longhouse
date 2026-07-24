import Foundation

/// Edit-shape detection and diff stats, mirroring
/// `web/src/lib/sessionWorkspace/editSummary.ts`. Both clients must produce
/// byte-identical collapsed summaries against the shared fixture corpus.
///
/// See `docs/specs/transcript-action-visibility.md`.
enum EditSummary {
    /// Max LCS cells (old lines × new lines) spent on one interaction.
    static let diffCellBudget = 250_000

    enum Patch: Equatable {
        case replace(oldStr: String, newStr: String)
        case create(content: String)
        case delete(content: String)
        case patch(text: String)
    }

    struct Stat: Equatable {
        var filePath: String?
        /// Basename for collapsed headers; the full path stays in the body.
        var fileName: String?
        var added: Int = 0
        var removed: Int = 0
        /// False when the shape is unknown or the diff exceeded the budget.
        var hasStat: Bool = false
        var patch: Patch?

        static let none = Stat()
    }

    private static func countLines(_ text: String) -> Int {
        text.isEmpty ? 0 : text.components(separatedBy: "\n").count
    }

    private static func basename(_ path: String?) -> String? {
        guard let path else { return nil }
        let parts = path.split(separator: "/").map(String.init)
        return parts.last ?? path
    }

    /// Path field names vary across providers (Claude, Codex, OpenCode, Cursor).
    private static func filePath(_ event: SessionEvent) -> String? {
        for key in ["file_path", "path", "filePath", "filename"] {
            if let value = TimelineBuilder.presentedToolInputString(event, key), !value.isEmpty {
                return value
            }
        }
        return nil
    }

    /// Classify an edit-tool input into a shape we can render. Returns nil for
    /// shapes we do not understand — never fabricate a stat.
    static func patch(for event: SessionEvent) -> Patch? {
        func input(_ keys: [String]) -> String? {
            for key in keys {
                if let value = TimelineBuilder.presentedToolInputString(event, key) { return value }
            }
            return nil
        }

        let oldStr = input(["old_string", "oldString", "old_str"])
        let newStr = input(["new_string", "newString", "new_str"])
        if let oldStr, let newStr { return .replace(oldStr: oldStr, newStr: newStr) }
        // `old_string` alone is a removal; `new_string` alone is an insertion.
        if let oldStr { return .delete(content: oldStr) }
        if let newStr { return .create(content: newStr) }

        if let patchText = input(["patch", "diff"]) { return .patch(text: patchText) }
        if let content = input(["content", "contents", "text"]) { return .create(content: content) }
        return nil
    }

    /// Count `+`/`-` lines in unified-patch text. `+++`/`---` are file headers.
    private static func patchStats(_ text: String) -> (added: Int, removed: Int) {
        var added = 0
        var removed = 0
        for line in text.components(separatedBy: "\n") {
            if line.hasPrefix("+++") || line.hasPrefix("---") { continue }
            if line.hasPrefix("+") { added += 1 } else if line.hasPrefix("-") { removed += 1 }
        }
        return (added, removed)
    }

    /// No cache here: unlike the web render loop, `activitySummary` is not
    /// called per frame, and a `static var` cache would be a concurrency hazard.
    /// The budget check below is what keeps the cost bounded.
    static func stat(for event: SessionEvent) -> Stat {
        let path = filePath(event)
        guard let patch = patch(for: event) else {
            // Known file, unknown shape: still name the file, without a stat.
            guard let path else { return .none }
            return Stat(filePath: path, fileName: basename(path))
        }

        var stat = Stat(filePath: path, fileName: basename(path), patch: patch)
        switch patch {
        case .create(let content):
            stat.added = countLines(content)
            stat.hasStat = true
        case .delete(let content):
            stat.removed = countLines(content)
            stat.hasStat = true
        case .patch(let text):
            let counts = patchStats(text)
            stat.added = counts.added
            stat.removed = counts.removed
            stat.hasStat = true
        case .replace(let oldStr, let newStr):
            // Budget check *before* paying for the LCS table.
            guard countLines(oldStr) * countLines(newStr) <= diffCellBudget else { return stat }
            // Count from the diff we already have to build, rather than running
            // a second LCS pass just for the totals.
            for line in lineDiff(oldStr: oldStr, newStr: newStr) {
                if line.kind == .add { stat.added += 1 } else if line.kind == .remove { stat.removed += 1 }
            }
            stat.hasStat = true
        }
        return stat
    }

    struct DiffLine: Equatable {
        enum Kind: String { case equal, add, remove }
        let kind: Kind
        let text: String
    }

    /// Renderable diff lines for a stat's patch, mirroring `EditDiffView` on
    /// web. Returns nil for shapes with no patch.
    static func diffLines(for stat: Stat, context: Int = 2) -> [DiffLine]? {
        guard let patch = stat.patch else { return nil }
        switch patch {
        case .create(let content):
            return content.components(separatedBy: "\n").map { DiffLine(kind: .add, text: $0) }
        case .delete(let content):
            return content.components(separatedBy: "\n").map { DiffLine(kind: .remove, text: $0) }
        case .patch(let text):
            return text.components(separatedBy: "\n").map { line in
                if line.hasPrefix("+"), !line.hasPrefix("+++") { return DiffLine(kind: .add, text: line) }
                if line.hasPrefix("-"), !line.hasPrefix("---") { return DiffLine(kind: .remove, text: line) }
                return DiffLine(kind: .equal, text: line)
            }
        case .replace(let oldStr, let newStr):
            guard stat.hasStat else { return nil }
            return collapseUnchanged(lineDiff(oldStr: oldStr, newStr: newStr), context: context)
        }
    }

    /// LCS line diff. Scoped to the small strings edit tools produce; callers
    /// must apply `diffCellBudget` first.
    static func lineDiff(oldStr: String, newStr: String) -> [DiffLine] {
        let a = oldStr.isEmpty ? [] : oldStr.components(separatedBy: "\n")
        let b = newStr.isEmpty ? [] : newStr.components(separatedBy: "\n")
        let m = a.count
        let n = b.count
        if m == 0 { return b.map { DiffLine(kind: .add, text: $0) } }
        if n == 0 { return a.map { DiffLine(kind: .remove, text: $0) } }

        var dp = [[Int]](repeating: [Int](repeating: 0, count: n + 1), count: m + 1)
        for i in stride(from: m - 1, through: 0, by: -1) {
            for j in stride(from: n - 1, through: 0, by: -1) {
                dp[i][j] = a[i] == b[j] ? dp[i + 1][j + 1] + 1 : max(dp[i + 1][j], dp[i][j + 1])
            }
        }

        var out: [DiffLine] = []
        var i = 0
        var j = 0
        while i < m, j < n {
            if a[i] == b[j] {
                out.append(DiffLine(kind: .equal, text: a[i]))
                i += 1
                j += 1
            } else if dp[i + 1][j] >= dp[i][j + 1] {
                out.append(DiffLine(kind: .remove, text: a[i]))
                i += 1
            } else {
                out.append(DiffLine(kind: .add, text: b[j]))
                j += 1
            }
        }
        while i < m { out.append(DiffLine(kind: .remove, text: a[i])); i += 1 }
        while j < n { out.append(DiffLine(kind: .add, text: b[j])); j += 1 }
        return out
    }

    /// Collapse long unchanged runs into a single marker, keeping `context`
    /// lines either side of each change.
    static func collapseUnchanged(_ lines: [DiffLine], context: Int = 2) -> [DiffLine] {
        guard lines.count > context * 2 + 1 else { return lines }
        var keep = [Bool](repeating: false, count: lines.count)
        for (i, line) in lines.enumerated() where line.kind != .equal {
            for k in max(0, i - context)...min(lines.count - 1, i + context) { keep[k] = true }
        }

        var out: [DiffLine] = []
        var skipped = 0
        func flush() {
            guard skipped > 0 else { return }
            out.append(DiffLine(
                kind: .equal,
                text: "… \(skipped) unchanged line\(skipped == 1 ? "" : "s") …"
            ))
            skipped = 0
        }
        for (i, line) in lines.enumerated() {
            if keep[i] {
                flush()
                out.append(line)
            } else {
                skipped += 1
            }
        }
        flush()
        return out
    }

    /// `timelineModel.ts +4 −1`, or the bare name when no stat is available.
    static func format(_ stat: Stat) -> String? {
        guard let name = stat.fileName else { return nil }
        guard stat.hasStat else { return name }
        return "\(name) +\(stat.added) −\(stat.removed)"
    }
}
