/**
 * ProductShowcase
 *
 * Tabbed showcase of real product screenshots.
 * Shows Timeline and Session Detail views.
 */

import { useEffect, useState } from "react";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

type Tab = "timeline" | "search" | "session";

interface TabConfig {
  id: Tab;
  label: string;
  description: string;
  image: string;
  alt: string;
}

const tabs: TabConfig[] = [
  {
    id: "timeline",
    label: "Timeline",
    description: "See imported and Longhouse-launched sessions in one archive across providers and machines.",
    image: "/images/landing/timeline-preview.webp?v=20260709-3",
    alt: "Session timeline showing Claude Code sessions with timestamps and summaries",
  },
  {
    id: "search",
    label: "Search",
    description: "Find the session where auth, retries, or that migration was already solved.",
    image: "/images/landing/search-preview.webp?v=20260709-3",
    alt: "Search results filtering sessions by keyword with highlighted matches",
  },
  {
    id: "session",
    label: "Session Detail",
    description: "Read the raw transcript, tool calls, and exact context you want to continue from.",
    image: "/images/landing/session-detail-preview.webp?v=20260709-3",
    alt: "Detailed session view showing tool calls and conversation",
  },
];

interface ProductShowcaseProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function ProductShowcase({ screenshotTheme }: ProductShowcaseProps) {
  const [activeTab, setActiveTab] = useState<Tab>("timeline");
  const activeConfig = tabs.find((t) => t.id === activeTab)!;

  useEffect(() => {
    // These are presentation assets, not user data. Fetch their compact WebP
    // variants after first paint so a tab click never waits on the network.
    const warmScreenshots = () => tabs.forEach(({ image }) => {
      const preload = new Image();
      preload.src = image;
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
      <div className="product-showcase-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`product-showcase-tab ${activeTab === tab.id ? "active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="product-showcase-content">
        <p className="product-showcase-description">{activeConfig.description}</p>

        <div className="product-showcase-frame">
          <AppScreenshotFrame
            src={activeConfig.image}
            alt={activeConfig.alt}
            title={activeConfig.label}
            aspectRatio="16/9"
            showChrome={true}
            theme={screenshotTheme}
            loading="eager"
            fetchPriority="high"
          />
        </div>
      </div>
    </div>
  );
}
