import { useEffect, useState } from "react";

export function useDocumentVisible(): boolean {
  const [isVisible, setIsVisible] = useState(() => typeof document === "undefined" || !document.hidden);

  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }

    const handleVisibilityChange = () => {
      setIsVisible(!document.hidden);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  return isVisible;
}
