import { HOST_LINK_COPY } from "./copy";
import { hostLinkStore, type HostLinkStore } from "./store";
import { useHostLink } from "./useHostLink";

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = String(seconds % 60).padStart(2, "0");
  return `${minutes}m ${remainingSeconds}s`;
}

export function HostLinkBanner({
  store = hostLinkStore,
}: {
  store?: HostLinkStore;
}) {
  const hostLink = useHostLink(store);

  if (hostLink.state === "updating" && hostLink.showUpdating) {
    return (
      <div
        className="host-link-banner host-link-banner--updating"
        data-testid="host-link-banner"
        data-state="updating"
        role="status"
        aria-live="polite"
      >
        {HOST_LINK_COPY.updatingBar}
      </div>
    );
  }

  if (hostLink.state === "slow_update") {
    return (
      <div
        className="host-link-banner host-link-banner--slow"
        data-testid="host-link-banner"
        data-state="slow_update"
        role="status"
        aria-live="polite"
      >
        {HOST_LINK_COPY.slowUpdateHeadline} · {formatElapsed(hostLink.elapsedSeconds)}
      </div>
    );
  }

  if (hostLink.reloadAvailable) {
    return (
      <div
        className="host-link-banner host-link-banner--reload"
        data-testid="host-link-banner"
        data-state="reload"
        role="status"
        aria-live="polite"
      >
        <button type="button" onClick={() => window.location.reload()}>
          {HOST_LINK_COPY.reload}
        </button>
      </div>
    );
  }

  return null;
}
