import { useEffect } from "react";

export const READY_ATTRIBUTE = "data-ready";
export const SCREENSHOT_READY_ATTRIBUTE = "data-screenshot-ready";

interface ReadinessOptions {
  ready: boolean;
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
  screenshotReady = false,
}: ReadinessOptions) {
  useEffect(() => {
    setBodyFlag(READY_ATTRIBUTE, ready);
    setBodyFlag(SCREENSHOT_READY_ATTRIBUTE, screenshotReady);

    return () => {
      document.body.removeAttribute(READY_ATTRIBUTE);
      document.body.removeAttribute(SCREENSHOT_READY_ATTRIBUTE);
    };
  }, [ready, screenshotReady]);
}
