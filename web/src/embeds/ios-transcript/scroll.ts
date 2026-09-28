// Stickiness is split by what each side can actually see. Native owns the
// INTENT ("the user deliberately scrolled up") because only it observes the
// drag. This side owns the GEOMETRY, because only it can read viewport
// height and content height in the same frame.
//
// The WebView's height is not constant: the floating control card, the
// keyboard, and the safe area all resize it through SwiftUI. UIScrollView
// does not re-clamp contentOffset when its bounds change, so a pinned
// transcript silently ends up short of (or past) the last row until the
// user drags. Nothing else in the app watches for that, so every viewport
// or content-height change has to re-pin here.
let stickToBottom = true;

export function isStickingToBottom(): boolean {
  return stickToBottom;
}

/// `window.setStickToBottom`: native publishes the user's scroll intent.
export function setStickToBottom(stick: unknown): void {
  stickToBottom = !!stick;
}

export function scrollToBottom(): void {
  const pin = () => {
    // WebKit can retain an out-of-range DOM scrollY even after UIKit
    // clamps its native offset. Supply the real maximum, not document height.
    if (stickToBottom && window.innerHeight > 0) {
      window.scrollTo(0, Math.max(0, document.documentElement.scrollHeight - window.innerHeight));
    }
  };
  pin();
  requestAnimationFrame(pin);
}

export function repinIfSticky(): void {
  if (stickToBottom) requestAnimationFrame(scrollToBottom);
}

/// Re-pin on every viewport resize (window and visual viewport) and every
/// content-height change of the transcript root, not only on renders.
export function installRepinObservers(root: Element): void {
  window.addEventListener("resize", repinIfSticky);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", repinIfSticky);
  }
  // WebKit always has ResizeObserver; the guard only spares jsdom.
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(repinIfSticky).observe(root);
  }
}
