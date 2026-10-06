import type { ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { Toaster } from "react-hot-toast";
import { AuthProvider } from "@/features/auth/auth";
import { ConfirmProvider } from "@/shared/ui/confirm";
import { TranscriptPersistence } from "./TranscriptPersistence";
import App from "./App";

/**
 * The tree main.tsx mounts in the browser and web/scripts/prerender.mjs renders
 * to static HTML. Both go through these two components so the prerendered DOM
 * is exactly what the browser hydrates; the only thing that differs is the
 * router (BrowserRouter vs StaticRouter), which the caller supplies.
 */
export function AppProviders({ queryClient, children }: { queryClient: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TranscriptPersistence />
        <ConfirmProvider>{children}</ConfirmProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export function AppContent() {
  return (
    <>
      <App />
      <Toaster
        position="top-right"
        toastOptions={{
          duration: 4000,
          style: {
            background: "#1A1410",
            color: "#F3EAD9",
            border: "1px solid #3d3428",
            borderRadius: "8px",
            fontSize: "14px",
            fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
          },
          success: {
            duration: 3000,
            iconTheme: {
              primary: "#5D9B4A",
              secondary: "#F3EAD9",
            },
          },
          error: {
            duration: 6000,
            iconTheme: {
              primary: "#C45040",
              secondary: "#F3EAD9",
            },
          },
        }}
      />
    </>
  );
}
