import { lazy, Suspense } from "react";
import { useRoutes, Outlet, Navigate } from "react-router";
import Layout from "./Layout";
import LandingPage from "@/features/marketing/landing/LandingPage";
import BlogIndexPage from "@/features/marketing/blog/BlogIndexPage";
import ProviderIntegrationsPostPage from "@/features/marketing/blog/ProviderIntegrationsPostPage";
import LoginPage from "@/features/auth/LoginPage";
import DocsLayout from "@/features/marketing/docs/DocsLayout";
import DocsOverviewPage from "@/features/marketing/docs/OverviewPage";
import DocsQuickStartPage from "@/features/marketing/docs/QuickStartPage";
import DocsSearchPage from "@/features/marketing/docs/SearchPage";
import DocsRemoteControlPage from "@/features/marketing/docs/RemoteControlPage";
import DocsCLIReferencePage from "@/features/marketing/docs/CLIReferencePage";
import DocsMachineAPIPage from "@/features/marketing/docs/MachineAPIPage";
import DocsIntegrationsPage from "@/features/marketing/docs/IntegrationsPage";
import DocsConfigurationPage from "@/features/marketing/docs/ConfigurationPage";
import ChangelogPage from "@/features/marketing/legal/ChangelogPage";
import PrivacyPage from "@/features/marketing/legal/PrivacyPage";
import SecurityPage from "@/features/marketing/legal/SecurityPage";
import TermsPage from "@/features/marketing/legal/TermsPage";
import DemoBanner from "./DemoBanner";
import { AuthGuard } from "@/features/auth/auth";
import { ErrorBoundary } from "./ErrorBoundary";
import {
  usePerformanceMonitoring,
} from "./usePerformance";
import config from "@/shared/lib/config";
import { Spinner } from "@/shared/ui/Spinner";

// Pages behind the app shell load on demand. Anonymous visitors to the
// landing, docs and legal pages never download them, or the markdown, syntax
// highlighting and drag-and-drop libraries only these pages use.
const ProfilePage = lazy(() => import("@/features/auth/ProfilePage"));
const SettingsPage = lazy(() => import("@/features/auth/SettingsPage"));
const DevicesPage = lazy(() => import("@/features/machines/DevicesPage"));
const MachinesPage = lazy(() => import("@/features/machines/MachinesPage"));
const MachineDetailPage = lazy(() => import("@/features/machines/MachineDetailPage"));
const RunnerDetailPage = lazy(() => import("@/features/runners/RunnerDetailPage"));
const SessionsPage = lazy(() => import("@/features/timeline/SessionsPage"));
const SessionDetailPage = lazy(() => import("@/features/session/SessionDetailPage"));

// Suspense sits inside Layout so the shell (nav, status footer, WebSocket)
// stays mounted while a page chunk loads.
function PageOutlet() {
  return (
    <Suspense
      fallback={
        <div className="route-loading">
          <Spinner size="md" label="Loading page" />
        </div>
      }
    >
      <Outlet />
    </Suspense>
  );
}

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
        <PageOutlet />
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
        <PageOutlet />
      </Layout>
    </>
  );
}

export function buildAppRoutes({ demoMode, singleTenant: _singleTenant }: RoutingConfig) {
  // Public reference pages — shared by demo and normal modes
  const publicInfoRoutes = [
    // Always-available landing page, even on single-tenant instances where "/"
    // goes straight to the timeline. LandingPage skips its auth redirect here.
    {
      path: "/landing",
      element: (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/blog",
      element: (
        <ErrorBoundary>
          <BlogIndexPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/blog/provider-integrations",
      element: (
        <ErrorBoundary>
          <ProviderIntegrationsPostPage />
        </ErrorBoundary>
      ),
    },
    {
      path: "/login",
      element: (
        <ErrorBoundary>
          <LoginPage />
        </ErrorBoundary>
      ),
    },
    // No "/share/:token" route: session sharing is disabled until the share
    // tables exist under the live catalog, so no share link can be minted and
    // none can be opened. `ShareLandingPage` is kept, unmounted, next to the
    // shelved server routes in `zerg/routers/session_shares.py`. Until then
    // "/share/..." falls through to the "*" redirect below.
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
    {
      path: "/terms",
      element: (
        <ErrorBoundary>
          <TermsPage />
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
              // Health and the Runners list folded into Machines (2026-10).
              {
                path: "/health",
                element: <Navigate to="/machines" replace />,
              },
              {
                path: "/observability",
                element: <Navigate to="/machines" replace />,
              },
              {
                path: "/runners",
                element: <Navigate to="/machines" replace />,
              },
              {
                path: "/machines",
                element: (
                  <ErrorBoundary>
                    <MachinesPage />
                  </ErrorBoundary>
                ),
              },
              {
                path: "/machines/:deviceId",
                element: (
                  <ErrorBoundary>
                    <MachineDetailPage />
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
