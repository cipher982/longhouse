import { useEffect, type RefObject } from "react";
import { useLatest } from "./useLatest";

interface UseClickOutsideOptions {
  enabled?: boolean;
  refs: Array<RefObject<Element | null>>;
  onClickOutside: () => void;
}

export function useClickOutside({
  enabled = true,
  refs,
  onClickOutside,
}: UseClickOutsideOptions) {
  const latestRefs = useLatest(refs);
  const latestOnClickOutside = useLatest(onClickOutside);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const handleMouseDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }

      if (latestRefs.current.some((ref) => ref.current?.contains(target))) {
        return;
      }

      latestOnClickOutside.current();
    };

    document.addEventListener("mousedown", handleMouseDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
    };
  }, [enabled, latestOnClickOutside, latestRefs]);
}
