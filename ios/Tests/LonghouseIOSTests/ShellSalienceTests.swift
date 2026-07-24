import XCTest
@testable import Longhouse

/// Shell salience classifier conformance — runs the exact corpus the web
/// suite runs (config/shell-salience-fixtures.json). A divergence from the
/// TS twin or a false demotion fails here; change the fixtures first, then
/// both implementations.
final class ShellSalienceTests: XCTestCase {
    private struct FixtureCase: Decodable {
        let command: String
        let expect: String
        let aggregate: String?
    }

    private struct FixtureFile: Decodable {
        let cases: [FixtureCase]
    }

    private func loadCases() throws -> [FixtureCase] {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("config/shell-salience-fixtures.json")
        let data = try Data(contentsOf: fixtureURL)
        return try JSONDecoder().decode(FixtureFile.self, from: data).cases
    }

    func testConformanceCorpus() throws {
        let cases = try loadCases()
        XCTAssertGreaterThan(cases.filter { $0.expect == "read" }.count, 20)
        XCTAssertGreaterThan(cases.filter { $0.expect == "opaque" }.count, 40)

        for fixture in cases {
            let result = ShellSalienceClassifier.classify(fixture.command)
            if fixture.expect == "opaque" {
                XCTAssertNil(result, "false demotion: \(fixture.command)")
            } else {
                XCTAssertNotNil(result, "failed to demote: \(fixture.command)")
                XCTAssertEqual(result?.tier, .noise, fixture.command)
                XCTAssertEqual(result?.aggregate.rawValue, fixture.aggregate, fixture.command)
            }
        }
    }

    func testShellToolNames() {
        for name in ["Bash", "shell", "shell_command", "exec_command", "run_shell_command"] {
            XCTAssertTrue(ShellSalienceClassifier.isShellTool(name), name)
        }
        XCTAssertFalse(ShellSalienceClassifier.isShellTool("write_stdin"))
        XCTAssertFalse(ShellSalienceClassifier.isShellTool("Read"))
    }

    func testExitCodeParsing() {
        let wrapped = [
            "Chunk ID: fixture",
            "Wall time: 0.1 seconds",
            "Process exited with code 1",
            "Output:",
            "no matches",
        ].joined(separator: "\n")
        XCTAssertEqual(ShellSalienceClassifier.parseExitCode(wrapped), 1)
        XCTAssertNil(ShellSalienceClassifier.parseExitCode("plain output"))
        XCTAssertNil(ShellSalienceClassifier.parseExitCode(nil))
    }

    func testShellReadsJoinExplorationRuns() {
        func pair(_ id: String, _ command: String, output: String = "ok") -> (SessionEvent, SessionEvent) {
            let call = SessionEvent(
                id: id,
                role: "assistant",
                contentText: nil,
                toolName: "Bash",
                toolInputJSON: ["command": .string(command)],
                toolOutputText: nil,
                toolCallId: "call-\(id)",
                toolCallState: nil,
                timestamp: "2026-01-01T00:00:00Z",
                inActiveContext: true,
                isHeadBranch: true,
                inputOrigin: nil
            )
            let result = SessionEvent(
                id: "\(id)-result",
                role: "tool",
                contentText: nil,
                toolName: "Bash",
                toolInputJSON: nil,
                toolOutputText: output,
                toolCallId: "call-\(id)",
                toolCallState: nil,
                timestamp: "2026-01-01T00:00:00Z",
                inActiveContext: true,
                isHeadBranch: true,
                inputOrigin: nil
            )
            return (call, result)
        }

        let (readCall, readResult) = pair("1", "grep -rn pattern web/src")
        XCTAssertTrue(
            TimelineBuilder.isActivityEligible(call: readCall, result: readResult, pairing: .id)
        )

        let (mutateCall, mutateResult) = pair("2", "rm -rf node_modules")
        XCTAssertTrue(
            TimelineBuilder.isActivityEligible(call: mutateCall, result: mutateResult, pairing: .id)
        )

        let failedOutput = "Process exited with code 1\nOutput:\nno matches"
        let (failedCall, failedResult) = pair("3", "grep -rn missing web/src", output: failedOutput)
        XCTAssertFalse(
            TimelineBuilder.isActivityEligible(call: failedCall, result: failedResult, pairing: .id)
        )
    }
}
