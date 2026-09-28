import Foundation
import OSLog

extension JSONDecoder {
    static let snakeCase: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .custom { codingPath in
            guard let key = codingPath.last else {
                return LonghouseJSONCodingKey("")
            }
            if codingPath.dropLast().contains(where: { pathKey in
                pathKey.stringValue == "tool_input_json" || pathKey.stringValue == "toolInputJson"
            }) {
                return LonghouseJSONCodingKey(key.stringValue)
            }
            return LonghouseJSONCodingKey(JSONDecoder.longhouseConvertFromSnakeCase(key.stringValue))
        }
        return decoder
    }()

    // API keys are a small fixed vocabulary, and the conversion (split,
    // ICU uppercasing, join) ran for every key of every decoded object.
    private static let snakeCaseCacheLock = NSLock()
    nonisolated(unsafe) private static var snakeCaseCache: [String: String] = [:]

    private static func longhouseConvertFromSnakeCase(_ key: String) -> String {
        guard key.contains("_") else { return key }
        snakeCaseCacheLock.lock()
        let cached = snakeCaseCache[key]
        snakeCaseCacheLock.unlock()
        if let cached { return cached }
        let converted = convertSnakeCaseKey(key)
        snakeCaseCacheLock.lock()
        // Bounded: free-form objects can carry arbitrary keys.
        if snakeCaseCache.count < 4096 { snakeCaseCache[key] = converted }
        snakeCaseCacheLock.unlock()
        return converted
    }

    private static func convertSnakeCaseKey(_ key: String) -> String {
        let parts = key.split(separator: "_", omittingEmptySubsequences: false)
        guard let first = parts.first else { return key }
        return String(first) + parts.dropFirst().map { part in
            guard let firstCharacter = part.first else { return "" }
            return firstCharacter.uppercased() + part.dropFirst()
        }.joined()
    }
}

private struct LonghouseJSONCodingKey: CodingKey {
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
