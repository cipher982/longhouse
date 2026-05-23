import Foundation
import UIKit
import ImageIO
import MobileCoreServices
import UniformTypeIdentifiers

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
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let cg = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw ImageCompressionError.decodeFailed
        }
        let original = UIImage(cgImage: cg)
        let scaled = downscale(original)
        guard let jpeg = scaled.jpegData(compressionQuality: jpegQuality) else {
            throw ImageCompressionError.encodeFailed
        }
        return CompressedImage(
            data: jpeg,
            mimeType: "image/jpeg",
            width: Int(scaled.size.width * scaled.scale),
            height: Int(scaled.size.height * scaled.scale),
        )
    }

    private static func downscale(_ image: UIImage) -> UIImage {
        let pixelWidth = image.size.width * image.scale
        let pixelHeight = image.size.height * image.scale
        let longest = max(pixelWidth, pixelHeight)
        guard longest > maxLongEdge else { return image }
        let scale = maxLongEdge / longest
        let target = CGSize(width: pixelWidth * scale, height: pixelHeight * scale)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = false
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        return renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
    }
}
