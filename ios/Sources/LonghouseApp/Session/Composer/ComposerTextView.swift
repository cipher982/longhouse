import SwiftUI
import UIKit

/// The composer's text box. A UITextView because SwiftUI's TextField only
/// pastes text: with a screenshot on the clipboard, long-press offered no
/// Paste at all. Here Paste appears for images too and hands them to
/// `onPasteImages`; text pastes as usual.
struct ComposerTextView: UIViewRepresentable {
    @Binding var text: String
    @Binding var isFocused: Bool
    let placeholder: String
    let maxLines: Int
    /// Nil when images cannot be attached; Paste then stays text-only.
    let onPasteImages: (([UIImage]) -> Void)?
    /// Hardware Return without Shift. Return true when it was handled (sent).
    let onReturn: () -> Bool
    /// Hardware Tab. Return true when it was handled (queued).
    let onTab: () -> Bool

    func makeUIView(context: Context) -> PastingTextView {
        let view = PastingTextView()
        view.delegate = context.coordinator
        view.font = UIFont.preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.textColor = .label
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.isScrollEnabled = false
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.accessibilityIdentifier = "session-chat-composer"
        view.placeholderLabel.font = view.font
        view.placeholderLabel.adjustsFontForContentSizeCategory = true
        return view
    }

    func updateUIView(_ view: PastingTextView, context: Context) {
        context.coordinator.parent = self
        if view.text != text { view.text = text }
        view.placeholderLabel.text = placeholder
        view.placeholderLabel.isHidden = !text.isEmpty
        view.accessibilityLabel = text.isEmpty ? placeholder : nil
        view.onPasteImages = onPasteImages
        view.onReturn = onReturn
        view.onTab = onTab
        // Focus follows the binding; UIKit reports back through the delegate.
        if isFocused, !view.isFirstResponder, view.window != nil {
            DispatchQueue.main.async { _ = view.becomeFirstResponder() }
        } else if !isFocused, view.isFirstResponder {
            DispatchQueue.main.async { _ = view.resignFirstResponder() }
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView view: PastingTextView, context: Context) -> CGSize? {
        let width = proposal.width ?? view.bounds.width
        guard width > 0, width.isFinite else { return nil }
        let lineHeight = (view.font ?? UIFont.preferredFont(forTextStyle: .body)).lineHeight
        let cap = ceil(lineHeight * CGFloat(maxLines))
        let natural = ceil(view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height)
        let scrolls = natural > cap
        if view.isScrollEnabled != scrolls {
            DispatchQueue.main.async { view.isScrollEnabled = scrolls }
        }
        return CGSize(width: width, height: max(ceil(lineHeight), min(natural, cap)))
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ComposerTextView

        init(parent: ComposerTextView) { self.parent = parent }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
            (textView as? PastingTextView)?.placeholderLabel.isHidden = !textView.text.isEmpty
        }

        func textViewDidBeginEditing(_ textView: UITextView) {
            if !parent.isFocused { parent.isFocused = true }
        }

        func textViewDidEndEditing(_ textView: UITextView) {
            if parent.isFocused { parent.isFocused = false }
        }
    }
}

final class PastingTextView: UITextView {
    let placeholderLabel = UILabel()
    var onPasteImages: (([UIImage]) -> Void)?
    var onReturn: (() -> Bool)?
    var onTab: (() -> Bool)?

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        placeholderLabel.textColor = .placeholderText
        placeholderLabel.numberOfLines = 1
        placeholderLabel.isAccessibilityElement = false
        placeholderLabel.translatesAutoresizingMaskIntoConstraints = false
        addSubview(placeholderLabel)
        NSLayoutConstraint.activate([
            placeholderLabel.leadingAnchor.constraint(equalTo: leadingAnchor),
            placeholderLabel.topAnchor.constraint(equalTo: topAnchor),
            placeholderLabel.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    private var clipboardImagesPastable: Bool {
        onPasteImages != nil && UIPasteboard.general.hasImages
    }

    override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
        if action == #selector(paste(_:)), clipboardImagesPastable { return true }
        return super.canPerformAction(action, withSender: sender)
    }

    override func paste(_ sender: Any?) {
        // An image copied from a web page often carries its URL as text too;
        // the image is what the user meant to paste.
        if clipboardImagesPastable, let images = UIPasteboard.general.images, !images.isEmpty {
            onPasteImages?(images)
            return
        }
        super.paste(sender)
    }

    override var keyCommands: [UIKeyCommand]? {
        let send = UIKeyCommand(input: "\r", modifierFlags: [], action: #selector(hardwareReturn))
        send.wantsPriorityOverSystemBehavior = true
        let queue = UIKeyCommand(input: "\t", modifierFlags: [], action: #selector(hardwareTab))
        queue.wantsPriorityOverSystemBehavior = true
        return [send, queue]
    }

    @objc private func hardwareReturn() {
        if onReturn?() != true { insertText("\n") }
    }

    @objc private func hardwareTab() {
        _ = onTab?()
    }
}
