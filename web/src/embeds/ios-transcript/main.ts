// Entry for the iOS transcript document (ios/Resources/Transcript/transcript.html).
// The app's WKWebView calls these three globals; their names and arguments are
// the bridge protocol, so they must not change without the Swift side.
import { renderTranscript, waitForTranscriptFrame } from "./render";
import { installScrollOwner, scrollStats, setStickToBottom } from "./scroll";

declare global {
  interface Window {
    renderTranscript: typeof renderTranscript;
    setStickToBottom: typeof setStickToBottom;
    waitForTranscriptFrame: typeof waitForTranscriptFrame;
    __lhScrollStats: typeof scrollStats;
  }
}

window.waitForTranscriptFrame = waitForTranscriptFrame;
window.setStickToBottom = setStickToBottom;
window.renderTranscript = renderTranscript;
window.__lhScrollStats = scrollStats;
installScrollOwner(document.getElementById("root")!);
