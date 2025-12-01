import { useEffect } from "react";
import { useRoutes } from "react-router-dom";
import Layout from "../components/Layout";
import LandingPage from "../pages/LandingPage";
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

// Authenticated app wrapper - only wraps routes that need auth
function AuthenticatedApp({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard clientId={config.googleClientId}>
      <ShelfProvider>
        <Layout>{children}</Layout>
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
    // Authenticated routes
    {
      path: "/dashboard",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <DashboardPage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
    },
    {
      path: "/canvas",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <CanvasPage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
    },
    {
      path: "/agent/:agentId/thread/:threadId?",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <ChatPage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
    },
    {
      path: "/profile",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <ProfilePage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
    },
    {
      path: "/settings/integrations",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <IntegrationsPage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
    },
    {
      path: "/admin",
      element: (
        <AuthenticatedApp>
          <ErrorBoundary>
            <AdminPage />
          </ErrorBoundary>
        </AuthenticatedApp>
      )
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
