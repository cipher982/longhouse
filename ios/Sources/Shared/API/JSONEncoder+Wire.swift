import Foundation

extension JSONEncoder {
    /// Encodes served models back to the wire's snake_case, the shape the
    /// shared TypeScript presentation model reads. The decoder (`snakeCase`)
    /// camel-cases every key except those inside a tool input, which is the
    /// provider's own object; this is its inverse.
    static let wire: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .custom { codingPath in
            guard let key = codingPath.last else { return WireCodingKey("") }
            if codingPath.dropLast().contains(where: { freeFormKeys.contains($0.stringValue) }) {
                return WireCodingKey(key.stringValue)
            }
            // ToolPresentation names the input `toolInputValue`; the wire calls it `tool_input_json`.
            if key.stringValue == "toolInputValue" { return WireCodingKey("tool_input_json") }
            return WireCodingKey(wireKey(key.stringValue))
        }
        return encoder
    }()

    /// Keys whose value is a provider-owned object, passed through untouched.
    private static let freeFormKeys: Set<String> = ["toolInputJson", "toolInputValue"]

    private static let keyCacheLock = NSLock()
    nonisolated(unsafe) private static var keyCache: [String: String] = [:]

    private static func wireKey(_ key: String) -> String {
        keyCacheLock.lock()
        let cached = keyCache[key]
        keyCacheLock.unlock()
        if let cached { return cached }
        var converted = ""
        converted.reserveCapacity(key.count + 4)
        for character in key {
            if character.isUppercase {
                converted.append("_")
                converted.append(contentsOf: character.lowercased())
            } else {
                converted.append(character)
            }
        }
        keyCacheLock.lock()
        // Bounded: free-form objects can carry arbitrary keys.
        if keyCache.count < 4096 { keyCache[key] = converted }
        keyCacheLock.unlock()
        return converted
    }
}

private struct WireCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init(_ stringValue: String) {
        self.stringValue = stringValue
        self.intValue = nil
    }

    init?(stringValue: String) {
        self.init(stringValue)
    }

    init?(intValue: Int) {
        self.stringValue = String(intValue)
        self.intValue = intValue
    }
}
