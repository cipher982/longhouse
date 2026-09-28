import Foundation
import SwiftUI

enum ToolCallState: String, Codable, Hashable, Sendable, CaseIterable {
    case running
    case completed
    case dropped
}

struct ToolPresentationChild: Codable, Hashable, Sendable {
    let version: Int
    let childId: String
    let disposition: String
    let toolName: String
    let label: String
    let icon: String
    let color: String
    let tier: String
    let aggregate: String?
    let mcpNamespace: String?
    let toolInputValue: JSONValue?
    let ruleId: String
    let sourceSpan: [Int]
    let inputComplete: Bool
    let resultForwarded: Bool
}

struct ShellSummaryOperation: Codable, Hashable, Sendable {
    let key: String
    let label: String
    let executable: String
    let subcommands: [String]
    let count: Int
}

struct ShellCommandSummary: Codable, Hashable, Sendable {
    let version: Int
    let confidence: String
    let operations: [ShellSummaryOperation]
    let candidateCount: Int
    let truncated: Bool
    let dynamic: Bool
    let parseError: String?
    let parserId: String
    let shapeRegistryVersion: Int
}

struct ToolPresentation: Codable, Hashable, Sendable {
    let version: Int
    let disposition: String
    let toolName: String
    let sourceToolName: String
    let executionMethod: String?
    let label: String
    let icon: String
    let color: String
    let tier: String
    let aggregate: String?
    let mcpNamespace: String?
    let toolInputValue: JSONValue?
    let ruleId: String
    let wrapperRecedes: Bool
    let children: [ToolPresentationChild]
    let shellSummary: ShellCommandSummary?

    init(
        version: Int,
        disposition: String,
        toolName: String,
        sourceToolName: String,
        executionMethod: String?,
        label: String,
        icon: String,
        color: String,
        tier: String,
        aggregate: String?,
        mcpNamespace: String?,
        toolInputValue: JSONValue?,
        ruleId: String,
        wrapperRecedes: Bool,
        children: [ToolPresentationChild],
        shellSummary: ShellCommandSummary? = nil
    ) {
        self.version = version
        self.disposition = disposition
        self.toolName = toolName
        self.sourceToolName = sourceToolName
        self.executionMethod = executionMethod
        self.label = label
        self.icon = icon
        self.color = color
        self.tier = tier
        self.aggregate = aggregate
        self.mcpNamespace = mcpNamespace
        self.toolInputValue = toolInputValue
        self.ruleId = ruleId
        self.wrapperRecedes = wrapperRecedes
        self.children = children
        self.shellSummary = shellSummary
    }
}
