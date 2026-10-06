/**
 * The app bar's page slot. On a session route the global header shrinks to one
 * ~44px bar and the session page renders its title, state and actions into
 * this slot, so the page has one top bar instead of two stacked ones.
 *
 * `null` means no slot: other routes, the demo shell before it mounts, and
 * tests that render a page without Layout. Callers then render inline.
 */
import { createContext, useContext } from "react";

export const HeaderSlotContext = createContext<HTMLElement | null>(null);

export function useHeaderSlot(): HTMLElement | null {
  return useContext(HeaderSlotContext);
}

/** Routes that render a single session: they get the compact app bar. */
export function isSessionRoute(pathname: string): boolean {
  return /^\/(timeline|sessions)\/[^/]+\/?$/.test(pathname);
}
