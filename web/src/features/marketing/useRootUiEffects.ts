import { useContext, useEffect } from "react";
import { PageMetaCollectorContext } from "@/shared/hooks/usePageMeta";

export function useRootUiEffects(enabled: boolean) {
  const collector = useContext(PageMetaCollectorContext);
  if (collector) collector.uiEffects = enabled;

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
