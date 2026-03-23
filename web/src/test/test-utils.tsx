import type { ReactNode } from "react";
import { MemoryRouter, type MemoryRouterProps } from "react-router-dom";

/**
 * MemoryRouter wrapper with React Router v7 future flags enabled.
 * Use this in tests to match the production BrowserRouter configuration.
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
      future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}
    >
      {children}
    </MemoryRouter>
  );
}
