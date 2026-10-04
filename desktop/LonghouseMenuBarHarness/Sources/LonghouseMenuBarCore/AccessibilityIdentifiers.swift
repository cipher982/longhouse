import Foundation

public enum LonghouseMenuBarAccessibilityID {
    public static let panel = "LonghouseMenuBar.Panel"

    public enum Error {
        public static let headline = "LonghouseMenuBar.Error.Headline"
        public static let message = "LonghouseMenuBar.Error.Message"
        public static let retryButton = "LonghouseMenuBar.Error.RetryButton"
    }

    /// Banner shown whenever the producer refresh is failing, including when a
    /// cached snapshot is still on screen.
    public enum StaleBanner {
        public static let container = "LonghouseMenuBar.StaleBanner"
        public static let headline = "LonghouseMenuBar.StaleBanner.Headline"
        public static let detail = "LonghouseMenuBar.StaleBanner.Detail"
        public static let command = "LonghouseMenuBar.StaleBanner.Command"
    }

    public enum Header {
        public static let statusGlyph = "LonghouseMenuBar.Header.StatusGlyph"
        public static let headline = "LonghouseMenuBar.Header.Headline"
    }

    public enum Hearth {
        public static let quietSessions = "LonghouseMenuBar.Hearth.QuietSessions"
        public static let unmanagedAgents = "LonghouseMenuBar.Hearth.UnmanagedAgents"
        public static let healthLine = "LonghouseMenuBar.Hearth.HealthLine"
    }

    public enum Feedback {
        public static let container = "LonghouseMenuBar.Feedback"
        public static let title = "LonghouseMenuBar.Feedback.Title"
        public static let detail = "LonghouseMenuBar.Feedback.Detail"
    }

    public enum Button {
        public static let refresh = "LonghouseMenuBar.Button.Refresh"
        public static let doctor = "LonghouseMenuBar.Button.Doctor"
        public static let repair = "LonghouseMenuBar.Button.Repair"
        public static let copyDiagnostics = "LonghouseMenuBar.Button.CopyDiagnostics"
        public static let openLogs = "LonghouseMenuBar.Button.OpenLogs"
        public static let openLonghouse = "LonghouseMenuBar.Button.OpenLonghouse"
        public static let stopAllBackgroundBridges = "LonghouseMenuBar.Button.StopAllBackgroundBridges"
    }
}
