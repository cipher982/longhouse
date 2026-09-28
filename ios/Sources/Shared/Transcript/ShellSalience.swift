// Content-aware salience for shell tool calls (spec: Change B in
// docs/specs/timeline-reading-experience.md).
//
// Handwritten twin of web/src/lib/sessionWorkspace/shellSalience.ts.
// Behavioral parity is enforced by config/shell-salience-fixtures.json,
// which both test suites run in full. Change the fixtures first, then both
// implementations. Fail closed at every rule: the classifier detects
// boring, never danger — anything unrecognized keeps the action tier.

import Foundation

public struct ShellSalience: Sendable, Equatable {
    public let tier: ToolTier
    public let aggregate: ToolAggregate
}

public enum ShellSalienceClassifier {
    public static func isShellTool(_ toolName: String) -> Bool {
        ShellClassifierConstants.shellTools.contains(toolName)
    }

    /// Classify a raw shell command. Returns the demoted salience for
    /// read-only commands, or nil for anything unrecognized.
    public static func classify(_ command: String?) -> ShellSalience? {
        guard let command, !command.isEmpty, command.count <= 4000 else { return nil }
        if hasOpaqueStructure(command) { return nil }
        if !hasBalancedQuotes(command) { return nil }

        let segments = splitCommandSegments(command)
        var firstReadHead: String? = nil

        for rawSegment in segments {
            let segment = stripAssignments(String(rawSegment).trimmingCharacters(in: .whitespaces))
            if segment.isEmpty { continue }
            let parts = segment.split(whereSeparator: { $0.isWhitespace }).map(String.init)
            guard let head = parts.first else { continue }
            // Bare names only: a path like /tmp/ls is not the trusted ls.
            if head.contains("/") { return nil }
            if head == "cd" { continue }
            if head == "sed" {
                guard sedIsRead(parts) else { return nil }
                if firstReadHead == nil { firstReadHead = head }
                continue
            }
            if head == "git" {
                guard gitIsRead(parts) else { return nil }
                if firstReadHead == nil { firstReadHead = head }
                continue
            }
            guard ShellClassifierConstants.readOnlyCommands.contains(head) else { return nil }
            if firstReadHead == nil { firstReadHead = head }
        }

        guard let head = firstReadHead else { return nil }
        let aggregate = ShellClassifierConstants.aggregateByHead[head]
            ?? ShellClassifierConstants.defaultReadAggregate
        return ShellSalience(tier: .noise, aggregate: aggregate)
    }

    /// Parse a `Process exited with code N` line from wrapped tool output.
    /// Mirrors the web `parseLonghouseOutput` exit extraction closely enough
    /// for the demotion gate: nil when no exit line is present.
    public static func parseExitCode(_ output: String?) -> Int? {
        guard let output else { return nil }
        for line in output.split(separator: "\n", maxSplits: 12, omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("Process exited with code ") {
                return Int(trimmed.dropFirst("Process exited with code ".count))
            }
        }
        return nil
    }

    // MARK: - Grammar (mirror shellSalience.ts exactly)

    private static let controlKeywords: Set<String> = ["for", "while", "if", "until", "case", "function"]

    static func hasOpaqueStructure(_ s: String) -> Bool {
        let chars = Array(s)
        let n = chars.count
        var i = 0
        while i < n {
            let c = chars[i]
            if c == "\n" || c == "\r" || c == "`" { return true }
            if c == "$", i + 1 < n, chars[i + 1] == "(" { return true }
            if c == "<", i + 1 < n, chars[i + 1] == "<" || chars[i + 1] == "(" { return true }
            if c == ">" {
                if i + 1 < n, chars[i + 1] == "(" || chars[i + 1] == ">" || chars[i + 1] == "|" { return true }
                if i > 0, chars[i - 1].isNumber { return true }
                let prevAmp = i > 0 && chars[i - 1] == "&"
                let nextAmp = i + 1 < n && chars[i + 1] == "&"
                if !prevAmp && !nextAmp { return true }
            }
            if c == "|", i + 1 < n, chars[i + 1] == "&" { return true }
            if c == "&" {
                if i + 1 < n, chars[i + 1] == ">" { return true }
                let prevAmp = i > 0 && chars[i - 1] == "&"
                let nextAmp = i + 1 < n && chars[i + 1] == "&"
                if !prevAmp && !nextAmp { return true }
            }
            if c == "(" {
                var j = i + 1
                while j < n, chars[j].isWhitespace { j += 1 }
                if j < n, chars[j] == ")" { return true }
            }
            i += 1
        }
        // (^|[\s;(])(for|while|if|until|case|function)\s
        for keyword in controlKeywords {
            var searchRange = s.startIndex..<s.endIndex
            while let range = s.range(of: keyword, range: searchRange) {
                let precededOK = range.lowerBound == s.startIndex || {
                    let prev = s[s.index(before: range.lowerBound)]
                    return prev.isWhitespace || prev == ";" || prev == "("
                }()
                let followedOK = range.upperBound < s.endIndex && s[range.upperBound].isWhitespace
                if precededOK && followedOK { return true }
                searchRange = range.upperBound..<s.endIndex
            }
        }
        return false
    }

    static func hasBalancedQuotes(_ s: String) -> Bool {
        var single = false
        var double = false
        var escaped = false
        for ch in s {
            if escaped {
                escaped = false
                continue
            }
            if ch == "\\", !single {
                escaped = true
                continue
            }
            if ch == "'", !double { single.toggle() } else if ch == "\"", !single { double.toggle() }
        }
        return !single && !double
    }

    /// Split command chains only at operators outside quotes. Regex patterns
    /// and jq expressions commonly contain literal `|`/`;` characters.
    static func splitCommandSegments(_ command: String) -> [String] {
        let chars = Array(command)
        var segments: [String] = []
        var start = 0
        var single = false
        var double = false
        var i = 0
        while i < chars.count {
            let ch = chars[i]
            if ch == "\\", !single {
                i += 2
                continue
            }
            if ch == "'", !double {
                single.toggle()
                i += 1
                continue
            }
            if ch == "\"", !single {
                double.toggle()
                i += 1
                continue
            }
            if !single && !double && (ch == ";" || ch == "|" || ch == "&") {
                if ch != "&" || i + 1 < chars.count && chars[i + 1] == "&" {
                    segments.append(String(chars[start ..< i]).trimmingCharacters(in: .whitespaces))
                    if (ch == "&" || ch == "|"), i + 1 < chars.count, chars[i + 1] == ch { i += 1 }
                    start = i + 1
                }
            }
            i += 1
        }
        segments.append(String(chars[start ..< chars.count]).trimmingCharacters(in: .whitespaces))
        return segments
    }

    /// Strip leading VAR=value assignments from a segment.
    static func stripAssignments(_ segment: String) -> String {
        var s = segment
        while true {
            let parts = s.split(whereSeparator: { $0.isWhitespace }).map(String.init)
            guard let first = parts.first, parts.count > 1, isAssignment(first) else { return s }
            s = parts.dropFirst().joined(separator: " ")
        }
    }

    private static func isAssignment(_ word: String) -> Bool {
        guard let eq = word.firstIndex(of: "="), eq != word.startIndex else { return false }
        let name = word[word.startIndex..<eq]
        guard let firstChar = name.first, firstChar.isLetter || firstChar == "_" else { return false }
        return name.allSatisfy { $0.isLetter || $0.isNumber || $0 == "_" }
    }

    /// `sed` is read only in an explicit print shape: -n, no in-place in any
    /// spelling, print-only script (`-n '120,160p'`).
    static func sedIsRead(_ parts: [String]) -> Bool {
        let args = Array(parts.dropFirst())
        if args.contains(where: { $0 == "-i" || $0.hasPrefix("-i") || $0.hasPrefix("--in-place") }) {
            return false
        }
        guard args.contains("-n") else { return false }
        guard let script = args.first(where: { !$0.hasPrefix("-") }) else { return false }
        var body = script
        if let first = body.first, first == "'" || first == "\"" { body.removeFirst() }
        if let last = body.last, last == "'" || last == "\"" { body.removeLast() }
        guard body.last == "p" else { return false }
        let addresses = body.dropLast()
        return addresses.allSatisfy { $0.isNumber || $0 == "," || $0 == "$" || $0 == ";" || $0.isWhitespace }
    }

    /// `git` is read only for an allowlisted subcommand, skipping global options.
    static func gitIsRead(_ parts: [String]) -> Bool {
        var i = 1
        while i < parts.count {
            let p = parts[i]
            if p == "-C" || p == "-c" || p == "--git-dir" || p == "--work-tree" {
                i += 2
                continue
            }
            if p.hasPrefix("--git-dir=") || p.hasPrefix("--work-tree=") || p == "--no-pager" {
                i += 1
                continue
            }
            break
        }
        return i < parts.count && ShellClassifierConstants.gitReadSubcommands.contains(parts[i])
    }
}
