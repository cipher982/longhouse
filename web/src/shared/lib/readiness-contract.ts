import { useEffect } from "react";

export const READY_ATTRIBUTE = "data-ready";
export const SCREENSHOT_READY_ATTRIBUTE = "data-screenshot-ready";

interface ReadinessOptions {
  ready: boolean;
  /** When set, captures wait on this instead of the interactive ready flag. */
  screenshotReady?: boolean;
}

function setBodyFlag(attribute: string, enabled: boolean) {
  if (enabled) {
    document.body.setAttribute(attribute, "true");
    return;
  }

  document.body.removeAttribute(attribute);
}

export function useReadinessFlag({
  ready,
  screenshotReady,
}: ReadinessOptions) {
  useEffect(() => {
    setBodyFlag(READY_ATTRIBUTE, ready);
    if (screenshotReady === undefined) {
      document.body.removeAttribute(SCREENSHOT_READY_ATTRIBUTE);
    } else {
      document.body.setAttribute(SCREENSHOT_READY_ATTRIBUTE, String(screenshotReady));
    }

    return () => {
      document.body.removeAttribute(READY_ATTRIBUTE);
      document.body.removeAttribute(SCREENSHOT_READY_ATTRIBUTE);
    };
  }, [ready, screenshotReady]);
}
