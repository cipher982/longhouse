/**
 * ProductShowcase
 *
 * Tabbed showcase of real product screenshots, regenerated from the current UI
 * with curated fixtures by `make landing-screenshots`.
 */

import { useEffect, useState } from "react";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

type Tab = "timeline" | "search" | "session";

interface TabConfig {
  id: Tab;
  label: string;
  description: string;
  image: string;
  mobileImage: string;
  alt: string;
}

const tabs: TabConfig[] = [
  {
    id: "timeline",
    label: "Timeline",
    description: "Every session across your machines, most recently touched first. Open one to see what it is doing right now and pick up where it stopped.",
    image: "/images/landing/timeline-preview.webp?v=20260916-1",
    mobileImage: "/images/landing/timeline-preview-mobile.webp?v=20260916-1",
    alt: "Longhouse timeline with live Claude Code, Codex, and Cursor sessions across three machines, and recent history",
  },
  {
    id: "search",
    label: "Search",
    description: "Find the session by what you remember typing, then jump into it. Weeks of sessions across supported CLIs, not folders of provider logs.",
    image: "/images/landing/search-preview.webp?v=20260916-1",
    mobileImage: "/images/landing/search-preview-mobile.webp?v=20260916-1",
    alt: "Searching for flaky returns past sessions from several providers and machines, weeks apart",
  },
  {
    id: "session",
    label: "Session Detail",
    description: "The full transcript and every tool call, plus the composer. Read what it did, then tell it what to do next without going back to the terminal that started it.",
    image: "/images/landing/session-detail-preview.webp?v=20260916-1",
    mobileImage: "/images/landing/session-detail-preview-mobile.webp?v=20260916-1",
    alt: "A live Claude Code session: transcript, tool calls, and the composer to steer it",
  },
];

export function ProductShowcase() {
  const [activeTab, setActiveTab] = useState<Tab>("timeline");
  const activeConfig = tabs.find((t) => t.id === activeTab)!;

  useEffect(() => {
    // These are presentation assets, not user data. Fetch their compact WebP
    // variants after first paint so a tab click never waits on the network.
    const phone = window.matchMedia?.("(max-width: 640px)").matches;
    const warmScreenshots = () => tabs.forEach(({ image, mobileImage }) => {
      const preload = new Image();
      preload.src = phone ? mobileImage : image;
    });

    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (idleWindow.requestIdleCallback) {
      const id = idleWindow.requestIdleCallback(warmScreenshots, { timeout: 1500 });
      return () => idleWindow.cancelIdleCallback?.(id);
    }

    const id = window.setTimeout(warmScreenshots, 300);
    return () => window.clearTimeout(id);
  }, []);

  return (
    <div className="product-showcase">
      <div className="product-showcase-toolbar">
        <div className="product-showcase-tabs" role="tablist" aria-label="Longhouse session views">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls="product-showcase-panel"
              id={`product-showcase-tab-${tab.id}`}
              className={`product-showcase-tab ${activeTab === tab.id ? "active" : ""}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <p className="product-showcase-description">{activeConfig.description}</p>
      </div>

      <div
        className="product-showcase-content"
        id="product-showcase-panel"
        role="tabpanel"
        aria-labelledby={`product-showcase-tab-${activeConfig.id}`}
      >
        <div className="product-showcase-frame">
          <AppScreenshotFrame
            src={activeConfig.image}
            mobileSrc={activeConfig.mobileImage}
            alt={activeConfig.alt}
            title={activeConfig.label}
            loading="eager"
            fetchPriority="high"
          />
        </div>
      </div>
    </div>
  );
}
