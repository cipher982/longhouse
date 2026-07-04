import { useRoutes, Outlet, Navigate, useLocation } from "react-router-dom";
import Layout from "../components/Layout";
import LandingPage from "../pages/LandingPage";
import LoginPage from "../pages/LoginPage";
import DocsLayout from "../pages/docs/DocsLayout";
import DocsOverviewPage from "../pages/docs/OverviewPage";
import DocsQuickStartPage from "../pages/docs/QuickStartPage";
import DocsSearchPage from "../pages/docs/SearchPage";
import DocsRemoteControlPage from "../pages/docs/RemoteControlPage";
import DocsCLIReferencePage from "../pages/docs/CLIReferencePage";
import DocsMachineAPIPage from "../pages/docs/MachineAPIPage";
import DocsIntegrationsPage from "../pages/docs/IntegrationsPage";
import DocsConfigurationPage from "../pages/docs/ConfigurationPage";
import ChangelogPage from "../pages/ChangelogPage";
import PrivacyPage from "../pages/PrivacyPage";
import SecurityPage from "../pages/SecurityPage";
import ProfilePage from "../pages/ProfilePage";
import SettingsPage from "../pages/SettingsPage";
import DevicesPage from "../pages/DevicesPage";
import AdminPage from "../pages/AdminPage";
import ObservabilityPage from "../pages/ObservabilityPage";
import RunnersPage from "../pages/RunnersPage";
import RunnerDetailPage from "../pages/RunnerDetailPage";
import SessionsPage from "../pages/SessionsPage";
import SessionDetailPage from "../pages/SessionDetailPage";
import ShareLandingPage from "../pages/ShareLandingPage";
import DemoBanner from "../components/DemoBanner";
import { AuthGuard } from "../lib/auth";
import { ErrorBoundary } from "../components/ErrorBoundary";
import {
  usePerformanceMonitoring,
} from "../lib/usePerformance";
import config from "../lib/config";

type RoutingConfig = {
  demoMode: boolean;
  singleTenant: boolean;
};

// Authenticated app wrapper - wraps all authenticated routes with a single instance
// This prevents remounting Layout/StatusFooter/WebSocket on navigation
function AuthenticatedApp() {
  return (
    <AuthGuard clientId={config.googleClientId}>
      <Layout>
        <Outlet />
      </Layout>
    </AuthGuard>
  );
}

// Demo app wrapper — Layout with DemoBanner, no AuthGuard
function DemoApp() {
  return (
    <>
      <DemoBanner />
      <Layout>
        <Outlet />
      </Layout>
    </>
  );
}

function LandingAliasRedirect() {
  const location = useLocation();
  return <Navigate to={{ pathname: "/", search: location.search }} replace />;
}

export function buildAppRoutes({ demoMode, singleTenant: _singleTenant }: RoutingConfig) {
  // Public reference pages — shared by demo and normal modes
  const publicInfoRoutes = [
    {
      path: "/login",
      element: (
        <ErrorBoundary>
          <LoginPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/share/:token",
      element: (
        <ErrorBoundary>
          <ShareLandingPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/docs",
      element: (
        <ErrorBoundary>
          <DocsLayout />
        </ErrorBoundary>
      ),
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
    {
      path: "/changelog",
      element: (
        <ErrorBoundary>
          <ChangelogPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/privacy",
      element: (
        <ErrorBoundary>
          <PrivacyPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/security",
      element: (
        <ErrorBoundary>
          <SecurityPage />
        </ErrorBoundary>
      ),
    },
  ];

  const demoRoutes = [
    // Marketing / public pages
    {
      path: "/",
      element: (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      ),
    },
    ...publicInfoRoutes,
    // Demo timeline — wrapped in Layout with DemoBanner, no AuthGuard
    {
      element: <DemoApp />,
      children: [
        {
          path: "/timeline",
          element: (
            <ErrorBoundary>
              <SessionsPage />
            </ErrorBoundary>
          ),
        },
        {
          path: "/timeline/:sessionId",
          element: (
            <ErrorBoundary>
              <SessionDetailPage />
            </ErrorBoundary>
          ),
        },
        {
          path: "/demo",
          element: <Navigate to="/timeline" replace />,
        },
      ],
    },
    // Fallback: anything else -> landing
    {
      path: "*",
      element: <Navigate to="/" replace />,
    },
  ];

  // Single-tenant instances (provisioned by control plane) skip marketing pages entirely.
  // Root "/" goes straight to the authenticated app — timeline is the home page.
  const marketingRoutes = config.singleTenant
    ? []
    : [
        // Root: landing page for visitors, auto-redirects authenticated users to /timeline
        {
          path: "/",
          element: (
            <ErrorBoundary>
              <LandingPage />
            </ErrorBoundary>
          ),
        },
        // Keep /landing as an alias (bookmarks, old links)
        {
          path: "/landing",
          element: <LandingAliasRedirect />,
        },
  ];

  return (
    demoMode
      ? demoRoutes
      : [
          ...marketingRoutes,
          ...publicInfoRoutes,
          // Authenticated routes - nested under a single AuthenticatedApp wrapper
          {
            element: <AuthenticatedApp />,
            children: [
              // Single-tenant: "/" goes straight to timeline (no landing page)
              ...(config.singleTenant
                ? [
                    {
                      path: "/",
                      element: <Navigate to="/timeline" replace />,
                    },
                  ]
                : []),
              {
                path: "/profile",
                element: (
                  <ErrorBoundary>
                    <ProfilePage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/settings",
                element: (
                  <ErrorBoundary>
                    <SettingsPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/settings/devices",
                element: (
                  <ErrorBoundary>
                    <DevicesPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/admin",
                element: (
                  <ErrorBoundary>
                    <AdminPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/health",
                element: (
                  <ErrorBoundary>
                    <ObservabilityPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/observability",
                element: <Navigate to="/health" replace />,
              },
              {
                path: "/runners",
                element: (
                  <ErrorBoundary>
                    <RunnersPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/runners/:id",
                element: (
                  <ErrorBoundary>
                    <RunnerDetailPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/timeline",
                element: (
                  <ErrorBoundary>
                    <SessionsPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/timeline/:sessionId",
                element: (
                  <ErrorBoundary>
                    <SessionDetailPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/sessions",
                element: <Navigate to="/timeline" replace />,
              },
              {
                path: "/sessions/:sessionId",
                element: (
                  <ErrorBoundary>
                    <SessionDetailPage />
                  </ErrorBoundary>
                ),
              },
            ],
          },
          // Fallback for unknown SPA routes - send to landing page
          // NOTE: Static files (.html, .js, etc.) are served by Vite before reaching React Router
          {
            path: "*",
            element: <Navigate to="/" replace />,
          },
        ]
  );
}

export default function App() {
  // Performance monitoring
  usePerformanceMonitoring("App", { includeBundleSizeWarning: true });

  const routes = useRoutes(
    buildAppRoutes({ demoMode: config.demoMode, singleTenant: config.singleTenant }),
  );

  return routes;
}
