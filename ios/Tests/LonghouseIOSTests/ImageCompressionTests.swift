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
    func bodyContainsTextAndAttachmentParts() throws {
        let attachment = ComposerAttachment(
            id: UUID(),
            filename: "shot.jpg",
            data: Data([0xFF, 0xD8, 0xFF, 0xD9]),
            mimeType: "image/jpeg",
            thumbnail: nil,
        )
        let body = LonghouseAPI.buildMultipartBody(
            boundary: "Boundary-FIXED",
            text: "describe this",
            intent: "auto",
            clientRequestId: "ios-abc",
            attachments: [attachment],
        )
        let s = try #require(String(data: body, encoding: .isoLatin1))
        #expect(s.contains("Content-Disposition: form-data; name=\"text\""))
        #expect(s.contains("describe this"))
        #expect(s.contains("name=\"intent\""))
        #expect(s.contains("name=\"client_request_id\""))
        #expect(s.contains("ios-abc"))
        #expect(s.contains("name=\"attachments\"; filename=\"shot.jpg\""))
        #expect(s.contains("Content-Type: image/jpeg"))
        #expect(s.contains("--Boundary-FIXED--"))
    }

    @Test
    func bodyOmitsClientRequestIdWhenNil() throws {
        let body = LonghouseAPI.buildMultipartBody(
            boundary: "B",
            text: "hi",
            intent: "auto",
            clientRequestId: nil,
            attachments: [],
        )
        let s = try #require(String(data: body, encoding: .utf8))
        #expect(!s.contains("client_request_id"))
    }
}
