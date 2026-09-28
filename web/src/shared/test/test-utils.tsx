import type { ReactNode } from "react";
import { MemoryRouter, type MemoryRouterProps } from "react-router";

/**
 * MemoryRouter wrapper for tests. Mirrors the production BrowserRouter,
 * which runs React Router v7 with default behavior (no future flags).
 */
export function TestRouter({
  children,
  initialEntries,
  initialIndex,
}: {
  children: ReactNode;
  initialEntries?: MemoryRouterProps["initialEntries"];
  initialIndex?: MemoryRouterProps["initialIndex"];
}) {
  return (
    <MemoryRouter
      initialEntries={initialEntries}
      initialIndex={initialIndex}
    >
      {children}
    </MemoryRouter>
  );
}
