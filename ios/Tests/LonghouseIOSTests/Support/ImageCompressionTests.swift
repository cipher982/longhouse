import Foundation
import Testing
import UIKit
@testable import Longhouse

struct ImageCompressionTests {
    @Test
    func emptyDataIsRejected() {
        #expect(throws: ImageCompressionError.self) {
            _ = try ImageCompression.compress(Data())
        }
    }

    @Test
    func nonImageDataIsRejected() {
        let bytes = "not an image".data(using: .utf8)!
        #expect(throws: ImageCompressionError.self) {
            _ = try ImageCompression.compress(bytes)
        }
    }

    @Test
    func smallImagePassesThrough() throws {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 100, height: 80), format: format)
        let original = renderer.image { ctx in
            UIColor.red.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 100, height: 80))
        }
        let png = try #require(original.pngData())
        let out = try ImageCompression.compress(png)
        #expect(out.mimeType == "image/jpeg")
        #expect(out.width == 100)
        #expect(out.height == 80)
    }

    @Test
    func longestEdgeIsScaledTo2048() throws {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 4096, height: 3072), format: format)
        let original = renderer.image { ctx in
            UIColor.blue.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 4096, height: 3072))
        }
        let png = try #require(original.pngData())
        let out = try ImageCompression.compress(png)
        #expect(out.width == 2048)
        #expect(out.height == 1536)
    }
}

struct MultipartBodyTests {
    @Test
    func multipartUsesSharedFieldAndFileFraming() throws {
        let bytes = Data([0x00, 0xFF, 0x41, 0x0D])
        let attachment = ComposerAttachment(
            id: UUID(),
            filename: "shot.jpg",
            data: bytes,
            mimeType: "image/jpeg",
            thumbnail: nil,
        )
        let body = LonghouseAPI.buildMultipartBody(
            boundary: "Boundary-FIXED",
            text: "describe this",
            intent: "auto",
            clientRequestId: "ios-abc",
            model: "gpt-test",
            attachments: [attachment],
        )
        let encoded = try #require(String(data: body, encoding: .isoLatin1))
        #expect(encoded.contains("--Boundary-FIXED\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\ndescribe this\r\n"))
        #expect(encoded.contains("--Boundary-FIXED\r\nContent-Disposition: form-data; name=\"intent\"\r\n\r\nauto\r\n"))
        #expect(encoded.contains("--Boundary-FIXED\r\nContent-Disposition: form-data; name=\"client_request_id\"\r\n\r\nios-abc\r\n"))
        #expect(encoded.contains("--Boundary-FIXED\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ngpt-test\r\n"))
        #expect(encoded.contains("Content-Disposition: form-data; name=\"attachments\"; filename=\"shot.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"))
        let byteRange = try #require(body.range(of: bytes))
        #expect(body[byteRange] == bytes)
        #expect(encoded.hasSuffix("--Boundary-FIXED--\r\n"))
    }
}
