import Foundation
import UIKit
import ImageIO

/// Compressed image payload destined for the multipart upload.
struct CompressedImage: Sendable {
    let data: Data
    let mimeType: String
    let width: Int
    let height: Int
}

enum ImageCompressionError: Error, LocalizedError {
    case decodeFailed
    case encodeFailed
    case empty

    var errorDescription: String? {
        switch self {
        case .decodeFailed: return "Could not decode image."
        case .encodeFailed: return "Could not encode image."
        case .empty: return "Empty image."
        }
    }
}

enum ImageCompression {
    static let maxLongEdge: CGFloat = 2048
    static let jpegQuality: CGFloat = 0.85

    /// Decode → downscale (longest edge ≤ 2048) → JPEG @ 0.85.
    /// JPEG is the broadest-compatible target across the server's allow-list
    /// (png/jpeg/webp/gif). HEIC roundtrips into Codex are unproven and
    /// adding a webp encode for iOS is not worth the dependency here.
    static func compress(_ data: Data) throws -> CompressedImage {
        guard !data.isEmpty else { throw ImageCompressionError.empty }
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
            throw ImageCompressionError.decodeFailed
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: Int(maxLongEdge),
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            throw ImageCompressionError.decodeFailed
        }
        let scaled = UIImage(cgImage: cg)
        guard let jpeg = scaled.jpegData(compressionQuality: jpegQuality) else {
            throw ImageCompressionError.encodeFailed
        }
        return CompressedImage(
            data: jpeg,
            mimeType: "image/jpeg",
            width: cg.width,
            height: cg.height,
        )
    }
}
