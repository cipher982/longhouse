import { useState } from "react";
import { resolveDemoSeed } from "./seed";

export function useDemoSeed(): string {
  const [seed] = useState(() => {
    if (typeof window === "undefined") return "longhouse-demo";
    const explicit = new URLSearchParams(window.location.search).get("demoSeed");
    return resolveDemoSeed(explicit, window.sessionStorage);
  });
  return seed;
}
