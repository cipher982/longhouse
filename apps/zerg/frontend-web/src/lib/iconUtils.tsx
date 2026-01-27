import React from "react";
import { FicheIcon, GlobeIcon, SignalIcon, WrenchIcon, ZapIcon } from "../components/icons";

export type NodeType = "fiche" | "tool" | "trigger";

interface IconOptions {
  width?: number;
  height?: number;
  className?: string;
}

/**
 * Shared icon mapping for the canvas system.
 * Centralizes icon selection for sidebar, drag preview, and canvas nodes.
 */
export function getNodeIcon(
  type: NodeType,
  subType?: string,
  options: IconOptions = { width: 20, height: 20 }
): React.ReactElement {
  if (type === "fiche") {
    return <FicheIcon {...options} />;
  }

  if (type === "trigger") {
    return <ZapIcon {...options} />;
  }

  // Tools
  switch (subType) {
    case "http-request":
      return <GlobeIcon {...options} />;
    case "url-fetch":
      return <SignalIcon {...options} />;
    default:
      return <WrenchIcon {...options} />;
  }
}
