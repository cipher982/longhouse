import { useEffect, lazy, Suspense } from "react";
import { useRoutes, Outlet, Navigate } from "react-router-dom";
import Layout from "../components/Layout";
import LandingPage from "../pages/LandingPage";
import PricingPage from "../pages/PricingPage";
import DocsPage from "../pages/DocsPage";
import ChangelogPage from "../pages/ChangelogPage";
import PrivacyPage from "../pages/PrivacyPage";
import SecurityPage from "../pages/SecurityPage";
import DashboardPage from "../pages/DashboardPage";
import ProfilePage from "../pages/ProfilePage";
import SettingsPage from "../pages/SettingsPage";
import IntegrationsPage from "../pages/IntegrationsPage";
import ContactsPage from "../pages/ContactsPage";
import KnowledgeSourcesPage from "../pages/KnowledgeSourcesPage";
import AdminPage from "../pages/AdminPage";
import RunnersPage from "../pages/RunnersPage";
import RunnerDetailPage from "../pages/RunnerDetailPage";
import TraceExplorerPage from "../pages/TraceExplorerPage";
import ReliabilityPage from "../pages/ReliabilityPage";
import SessionsPage from "../pages/SessionsPage";
import SessionDetailPage from "../pages/SessionDetailPage";
import DemoBanner from "../components/DemoBanner";
import { Spinner } from "../components/ui";
import { AuthGuard } from "../lib/auth";

// Lazy-loaded pages (heavy dependencies - reduces initial bundle by ~700KB)
const ChatPage = lazy(() => import("../pages/ChatPage"));
const OikosChatPage = lazy(() => import("../pages/OikosChatPage"));
const ForumPage = lazy(() => import("../pages/ForumPage"));
const SwarmOpsPage = lazy(() => import("../pages/SwarmOpsPage"));
import { ShelfProvider } from "../lib/useShelfState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { usePerformanceMonitoring, useBundleSizeWarning } from "../lib/usePerformance";
import config from "../lib/config";

// Loading fallback for lazy-loaded pages
function PageLoader() {
  return (
    <div className="page-loader">
      <Spinner size="lg" className="page-loader-spinner" />
    </div>
  );
}

// Authenticated app wrapper - wraps all authenticated routes with a single instance
// This prevents remounting Layout/StatusFooter/WebSocket on navigation
function AuthenticatedApp() {
  return (
    <AuthGuard clientId={config.googleClientId}>
      <ShelfProvider>
        <Layout>
          <Outlet />
        </Layout>
      </ShelfProvider>
    </AuthGuard>
  );
}

// Demo app wrapper — Layout with DemoBanner, no AuthGuard
function DemoApp() {
  return (
    <ShelfProvider>
      <DemoBanner />
      <Layout>
        <Outlet />
      </Layout>
    </ShelfProvider>
  );
}

export default function App() {
  // Performance monitoring
  usePerformanceMonitoring('App');
  useBundleSizeWarning();

  useEffect(() => {
    // Signal to Playwright/legacy helpers that the React app finished booting.
    if (typeof window !== "undefined") {
      (window as typeof window & { __APP_READY__?: boolean }).__APP_READY__ = true;
    }
  }, []);

  const demoRoutes = [
    // Marketing / public pages
    {
      path: "/",
      element: (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/pricing",
      element: (
        <ErrorBoundary>
          <PricingPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/docs",
      element: (
        <ErrorBoundary>
          <DocsPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/changelog",
      element: (
        <ErrorBoundary>
          <ChangelogPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/privacy",
      element: (
        <ErrorBoundary>
          <PrivacyPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/security",
      element: (
        <ErrorBoundary>
          <SecurityPage />
        </ErrorBoundary>
      )
    },
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
          )
        },
        {
          path: "/timeline/:sessionId",
          element: (
            <ErrorBoundary>
              <SessionDetailPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/demo",
          element: <Navigate to="/timeline" replace />
        },
      ]
    },
    // Fallback: anything else -> landing
    {
      path: "*",
      element: <Navigate to="/" replace />
    },
  ];

  const routes = useRoutes(config.demoMode ? demoRoutes : [
    // Root: go straight to the app
    {
      path: "/",
      element: <Navigate to="/timeline" replace />
    },
    // Landing page at /landing for dev preview
    {
      path: "/landing",
      element: (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      )
    },
    // Public info pages - NO AuthGuard
    {
      path: "/pricing",
      element: (
        <ErrorBoundary>
          <PricingPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/docs",
      element: (
        <ErrorBoundary>
          <DocsPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/changelog",
      element: (
        <ErrorBoundary>
          <ChangelogPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/privacy",
      element: (
        <ErrorBoundary>
          <PrivacyPage />
        </ErrorBoundary>
      )
    },
    {
      path: "/security",
      element: (
        <ErrorBoundary>
          <SecurityPage />
        </ErrorBoundary>
      )
    },
    // Authenticated routes - nested under a single AuthenticatedApp wrapper
    {
      element: <AuthenticatedApp />,
      children: [
        {
          path: "/chat",
          element: (
            <ErrorBoundary>
              <Suspense fallback={<PageLoader />}>
                <OikosChatPage />
              </Suspense>
            </ErrorBoundary>
          )
        },
        {
          path: "/dashboard",
          element: (
            <ErrorBoundary>
              <DashboardPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/forum",
          element: (
            <ErrorBoundary>
              <Suspense fallback={<PageLoader />}>
                <ForumPage />
              </Suspense>
            </ErrorBoundary>
          )
        },
        {
          path: "/runs",
          element: (
            <ErrorBoundary>
              <Suspense fallback={<PageLoader />}>
                <SwarmOpsPage />
              </Suspense>
            </ErrorBoundary>
          )
        },
        {
          path: "/fiche/:ficheId/thread/:threadId?",
          element: (
            <ErrorBoundary>
              <Suspense fallback={<PageLoader />}>
                <ChatPage />
              </Suspense>
            </ErrorBoundary>
          )
        },
        {
          path: "/profile",
          element: (
            <ErrorBoundary>
              <ProfilePage />
            </ErrorBoundary>
          )
        },
        {
          path: "/settings",
          element: (
            <ErrorBoundary>
              <SettingsPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/settings/integrations",
          element: (
            <ErrorBoundary>
              <IntegrationsPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/settings/knowledge",
          element: (
            <ErrorBoundary>
              <KnowledgeSourcesPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/settings/contacts",
          element: (
            <ErrorBoundary>
              <ContactsPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/admin",
          element: (
            <ErrorBoundary>
              <AdminPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/runners",
          element: (
            <ErrorBoundary>
              <RunnersPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/runners/:id",
          element: (
            <ErrorBoundary>
              <RunnerDetailPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/traces",
          element: (
            <ErrorBoundary>
              <TraceExplorerPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/traces/:traceId",
          element: (
            <ErrorBoundary>
              <TraceExplorerPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/reliability",
          element: (
            <ErrorBoundary>
              <ReliabilityPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/timeline",
          element: (
            <ErrorBoundary>
              <SessionsPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/timeline/:sessionId",
          element: (
            <ErrorBoundary>
              <SessionDetailPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/sessions",
          element: <Navigate to="/timeline" replace />
        },
        {
          path: "/sessions/:sessionId",
          element: (
            <ErrorBoundary>
              <SessionDetailPage />
            </ErrorBoundary>
          )
        },
      ]
    },
    // Fallback for unknown SPA routes - redirect to timeline
    // AuthGuard will show login if unauthenticated
    // NOTE: Static files (.html, .js, etc.) are served by Vite before reaching React Router
    {
      path: "*",
      element: <Navigate to="/timeline" replace />
    },
  ]);

  return routes;
}
