import { useEffect } from "react";
import { useRoutes, Outlet } from "react-router-dom";
import Layout from "../components/Layout";
import LandingPage from "../pages/LandingPage";
import PricingPage from "../pages/PricingPage";
import DocsPage from "../pages/DocsPage";
import ChangelogPage from "../pages/ChangelogPage";
import PrivacyPage from "../pages/PrivacyPage";
import SecurityPage from "../pages/SecurityPage";
import DashboardPage from "../pages/DashboardPage";
import ChatPage from "../pages/ChatPage";
import CanvasPage from "../pages/CanvasPage";
import ProfilePage from "../pages/ProfilePage";
import IntegrationsPage from "../pages/IntegrationsPage";
import AdminPage from "../pages/AdminPage";
import { AuthGuard } from "../lib/auth";
import { ShelfProvider } from "../lib/useShelfState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { usePerformanceMonitoring, useBundleSizeWarning } from "../lib/usePerformance";
import config from "../lib/config";

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
    // Landing page - NO AuthGuard (public)
    {
      path: "/",
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
              <CanvasPage />
            </ErrorBoundary>
          )
        },
        {
          path: "/agent/:agentId/thread/:threadId?",
          element: (
            <ErrorBoundary>
              <ChatPage />
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
          path: "/settings/integrations",
          element: (
            <ErrorBoundary>
              <IntegrationsPage />
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
      ]
    },
    // Fallback - redirect to landing for unknown routes
    {
      path: "*",
      element: (
        <ErrorBoundary>
          <LandingPage />
        </ErrorBoundary>
      )
    },
  ]);

  return routes;
}
