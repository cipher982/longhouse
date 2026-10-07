import { Navigate, useRoutes } from "react-router";
import DocsLayout from "./DocsLayout";
import DocsOverviewPage from "./OverviewPage";
import DocsQuickStartPage from "./QuickStartPage";
import DocsSearchPage from "./SearchPage";
import DocsRemoteControlPage from "./RemoteControlPage";
import DocsCLIReferencePage from "./CLIReferencePage";
import DocsMachineAPIPage from "./MachineAPIPage";
import DocsIntegrationsPage from "./IntegrationsPage";
import DocsConfigurationPage from "./ConfigurationPage";

/**
 * Everything under /docs, as one lazily loaded chunk (app/routeChunks.ts), so
 * a landing-page visitor never downloads the docs. A prerendered docs page
 * links this chunk and its CSS in its own <head> (web/scripts/prerender.mjs)
 * and main.tsx loads it before hydrating, so the static page never flashes.
 */
export default function DocsRoutes() {
  return useRoutes([
    {
      element: <DocsLayout />,
      children: [
        { index: true, element: <DocsOverviewPage /> },
        { path: "quickstart", element: <DocsQuickStartPage /> },
        { path: "search", element: <DocsSearchPage /> },
        { path: "remote-control", element: <DocsRemoteControlPage /> },
        { path: "cli", element: <DocsCLIReferencePage /> },
        { path: "api", element: <DocsMachineAPIPage /> },
        { path: "integrations", element: <DocsIntegrationsPage /> },
        { path: "configuration", element: <DocsConfigurationPage /> },
      ],
    },
    // Unknown docs pages go to the landing page, like any unknown route.
    { path: "*", element: <Navigate to="/" replace /> },
  ]);
}
