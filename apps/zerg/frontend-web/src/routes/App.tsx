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
import { AuthGuard } from "../lib/auth";

// Lazy-loaded pages (heavy dependencies - reduces initial bundle by ~700KB)
const ChatPage = lazy(() => import("../pages/ChatPage"));
const CanvasPage = lazy(() => import("../pages/CanvasPage"));
const JarvisChatPage = lazy(() => import("../pages/JarvisChatPage"));
import { ShelfProvider } from "../lib/useShelfState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { usePerformanceMonitoring, useBundleSizeWarning } from "../lib/usePerformance";
import config from "../lib/config";

// Loading fallback for lazy-loaded pages
function PageLoader() {
  return (
    <div className="page-loader">
      <div className="page-loader-spinner" />
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

  const routes = useRoutes([
    // Root route: redirect to dashboard when auth disabled (dev/test mode)
    // In production (auth enabled), show landing page (it handles its own auth redirect)
    {
      path: "/",
      element: config.authEnabled ? (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      ) : (
        <Navigate to="/dashboard" replace />
      )
    },
    // Landing page accessible at /landing for dev preview when auth is disabled
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
                <JarvisChatPage />
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
          path: "/canvas",
          element: (
            <ErrorBoundary>
              <Suspense fallback={<PageLoader />}>
                <CanvasPage />
              </Suspense>
            </ErrorBoundary>
          )
        },
        {
          path: "/agent/:agentId/thread/:threadId?",
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
      ]
    },
    // Fallback for unknown SPA routes - redirect to dashboard (dev) or landing (prod)
    // NOTE: Static files (.html, .js, etc.) are served by Vite before reaching React Router
    {
      path: "*",
      element: config.authEnabled ? (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      ) : (
        <Navigate to="/dashboard" replace />
      )
    },
  ]);

  return routes;
}
