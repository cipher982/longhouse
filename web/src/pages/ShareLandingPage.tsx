import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, useParams } from "react-router-dom";
import { Button, Spinner } from "../components/ui";
import { useAuth } from "../lib/auth";
import { buildLoginUrl } from "../lib/loginRedirect";
import { useReadinessFlag } from "../lib/readiness-contract";
import {
  fetchSessionSharePreview,
  resolveSessionShare,
} from "../services/api/agents";
import { ApiError } from "../services/api/base";
import "../styles/share-landing.css";

function formatShareDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

function providerLabel(provider: string): string {
  const cleaned = provider.trim();
  if (!cleaned) return "agent";
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export default function ShareLandingPage() {
  const { token = "" } = useParams<{ token: string }>();
  const auth = useAuth();
  const safeToken = token.trim();
  const returnTo = `/share/${safeToken}`;
  const loginUrl = buildLoginUrl(returnTo);

  const previewQuery = useQuery({
    queryKey: ["session-share-preview", safeToken],
    queryFn: () => fetchSessionSharePreview(safeToken),
    enabled: Boolean(safeToken) && !auth.isAuthenticated,
    retry: false,
  });

  const resolveQuery = useQuery({
    queryKey: ["session-share-resolve", safeToken],
    queryFn: () => resolveSessionShare(safeToken),
    enabled: Boolean(safeToken) && !auth.isLoading && auth.isAuthenticated,
    retry: false,
  });

  const preview = previewQuery.data ?? null;
  const sharerName = preview?.sharer?.display_name?.trim() || "Someone";
  const startedLabel = useMemo(
    () => formatShareDate(preview?.started_at ?? null),
    [preview?.started_at],
  );
  const expiresLabel = useMemo(
    () => formatShareDate(preview?.expires_at ?? null),
    [preview?.expires_at],
  );
  const loading = auth.isLoading || previewQuery.isLoading || resolveQuery.isLoading;
  const previewError =
    previewQuery.error instanceof ApiError
      ? previewQuery.error.message
      : previewQuery.error
        ? "This share link could not be opened."
        : null;
  const resolveError =
    resolveQuery.error instanceof ApiError
      ? resolveQuery.error.message
      : resolveQuery.error
        ? "This share link could not be opened."
        : null;
  const ready = !loading || Boolean(previewError || resolveError);

  useReadinessFlag({ ready, screenshotReady: ready });

  const handleLogin = () => {
    window.location.assign(loginUrl);
  };

  if (resolveQuery.data) {
    return (
      <Navigate
        to={`/timeline/${resolveQuery.data.session_id}?share_token=${encodeURIComponent(safeToken)}`}
        replace
      />
    );
  }

  return (
    <main className="share-landing" data-testid="share-landing-page">
      <section className="share-landing__panel" aria-live="polite">
        {loading ? (
          <div className="share-landing__state" data-testid="share-landing-loading">
            <Spinner size="lg" />
            <span>Opening shared session...</span>
          </div>
        ) : previewError || resolveError ? (
          <div className="share-landing__state" data-testid="share-landing-error">
            <h1>Share link unavailable</h1>
            <p>{previewError || resolveError}</p>
          </div>
        ) : preview ? (
          <>
            <div className="share-landing__eyebrow">Shared Longhouse session</div>
            <h1>{sharerName} shared a {providerLabel(preview.provider)} session</h1>
            <dl className="share-landing__meta">
              {preview.device_name ? (
                <>
                  <dt>Device</dt>
                  <dd>{preview.device_name}</dd>
                </>
              ) : null}
              {startedLabel ? (
                <>
                  <dt>Started</dt>
                  <dd>{startedLabel}</dd>
                </>
              ) : null}
              {expiresLabel ? (
                <>
                  <dt>Expires</dt>
                  <dd>{expiresLabel}</dd>
                </>
              ) : null}
            </dl>
            {preview.note ? <p className="share-landing__note">{preview.note}</p> : null}
            <Button
              variant="primary"
              size="md"
              onClick={handleLogin}
              data-testid="share-landing-login-button"
            >
              Continue to Longhouse
            </Button>
          </>
        ) : null}
      </section>
    </main>
  );
}
