/**
 * The demo domain: everything a renderer needs to play the hero demo,
 * with ZERO Remotion imports. The web landing page consumes this via
 * the "@longhouse/video/demo" subpath so its bundle never touches
 * Remotion; the ControlRoom composition consumes it relatively.
 */
export * from "./script";
export * from "./recordings";
export { TerminalGrid, type GridTimeline } from "../terminal/TerminalGrid";
