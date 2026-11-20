import SwiftUI
import UIKit

/// Full-screen look at an image waiting in the composer tray: pinch or
/// double-tap to zoom, Done to go back to typing, Remove to drop it.
struct AttachmentPreview: View {
    let image: UIImage
    let onRemove: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            ZoomableImage(image: image)
                .ignoresSafeArea()
                .accessibilityLabel("Attached image")
        }
        .overlay(alignment: .top) {
            HStack {
                Button("Remove", role: .destructive) {
                    onRemove()
                    dismiss()
                }
                .accessibilityIdentifier("session-chat-attachment-preview-remove")
                Spacer()
                Button("Done") { dismiss() }
                    .fontWeight(.semibold)
                    .accessibilityIdentifier("session-chat-attachment-preview-done")
            }
            .buttonStyle(.bordered)
            .tint(.white)
            .padding(.horizontal, 16)
            .padding(.top, 8)
        }
        .statusBarHidden()
    }
}

/// UIScrollView zoom: pinch, pan while zoomed, double-tap to toggle 1x and
/// 3x around the tap. SwiftUI gestures alone do not give pan-with-bounds.
private struct ZoomableImage: UIViewRepresentable {
    let image: UIImage

    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.delegate = context.coordinator
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 6
        scroll.showsVerticalScrollIndicator = false
        scroll.showsHorizontalScrollIndicator = false
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.backgroundColor = .black

        let imageView = UIImageView(image: image)
        imageView.contentMode = .scaleAspectFit
        imageView.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(imageView)
        NSLayoutConstraint.activate([
            imageView.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor),
            imageView.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor),
            imageView.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            imageView.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            imageView.widthAnchor.constraint(equalTo: scroll.frameLayoutGuide.widthAnchor),
            imageView.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor),
        ])
        context.coordinator.imageView = imageView

        let doubleTap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTapped(_:)))
        doubleTap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(doubleTap)
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {
        context.coordinator.imageView?.image = image
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        weak var imageView: UIImageView?

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

        @objc func doubleTapped(_ gesture: UITapGestureRecognizer) {
            guard let scroll = gesture.view as? UIScrollView else { return }
            if scroll.zoomScale > scroll.minimumZoomScale {
                scroll.setZoomScale(scroll.minimumZoomScale, animated: true)
                return
            }
            let point = gesture.location(in: imageView)
            let scale: CGFloat = 3
            let size = CGSize(width: scroll.bounds.width / scale, height: scroll.bounds.height / scale)
            scroll.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2, width: size.width, height: size.height), animated: true)
        }
    }
}
