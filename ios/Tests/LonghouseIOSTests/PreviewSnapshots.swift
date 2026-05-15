#if DEBUG
import SnapshottingTests
import XCTest

/// Renders every #Preview in the app target to a PNG attached to the
/// xcresult. Run via `ios/scripts/render-previews.sh`.
///
/// EmergeTools SnapshotPreviews discovers previews at runtime — no
/// per-preview boilerplate. Each preview becomes its own dynamically
/// generated XCTest case so failures localize to a single preview.
final class PreviewSnapshots: SnapshotTest {
    /// Return nil to render every preview in the binary.
    /// Override with specific preview type names to keep the run fast
    /// while iterating on a particular screen.
    override class func snapshotPreviews() -> [String]? {
        return nil
    }
}
#endif
