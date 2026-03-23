import { useEffect } from "react";

export function useRootUiEffects(enabled: boolean) {
  useEffect(() => {
    const container = document.getElementById("react-root");
    const previous = container?.getAttribute("data-ui-effects");

    if (container) {
      container.setAttribute("data-ui-effects", enabled ? "on" : "off");
    }

    return () => {
      if (!container) {
        return;
      }
      if (previous) {
        container.setAttribute("data-ui-effects", previous);
      } else {
        container.removeAttribute("data-ui-effects");
      }
    };
  }, [enabled]);
}
