import { useEffect } from "react";
import { useRoutes } from "react-router-dom";
import Layout from "../components/Layout";
import DashboardPage from "../pages/DashboardPage";
import ChatPage from "../pages/ChatPage";
import CanvasPage from "../pages/CanvasPage";
import ProfilePage from "../pages/ProfilePage";
import AdminPage from "../pages/AdminPage";
import { AuthGuard } from "../lib/auth";
import { ShelfProvider } from "../lib/useShelfState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { usePerformanceMonitoring, useBundleSizeWarning } from "../lib/usePerformance";
import config from "../lib/config";

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
      path: "/admin",
      element: (
        <ErrorBoundary>
          <AdminPage />
        </ErrorBoundary>
      )
    },
    {
      path: "*",
      element: (
        <ErrorBoundary>
          <DashboardPage />
        </ErrorBoundary>
      )
    },
  ]);

  return (
    <AuthGuard clientId={config.googleClientId}>
      <ShelfProvider>
        <Layout>{routes}</Layout>
      </ShelfProvider>
    </AuthGuard>
  );
}
