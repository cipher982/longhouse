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
  /** Autoplaying clip for the tab; the image doubles as its poster. */
  video?: string;
  alt: string;
}

const tabs: TabConfig[] = [
  {
    id: "timeline",
    label: "Timeline",
    description: "Every session across your machines, most recently touched first. Open one to see what it is doing right now and pick up where it stopped.",
    image: "/images/landing/timeline-preview.webp?v=20260709-3",
    video: "/videos/timeline-clip.mp4?v=20260808",
    alt: "Session timeline showing Claude Code sessions with timestamps and summaries",
  },
  {
    id: "search",
    label: "Search",
    description: "Find the session by what you remember typing, then jump into it. Weeks of sessions across four CLIs, not folders of provider logs.",
    image: "/images/landing/search-preview.webp?v=20260709-3",
    alt: "Search results filtering sessions by keyword with highlighted matches",
  },
  {
    id: "session",
    label: "Session Detail",
    description: "The full transcript and every tool call, plus the composer. Read what it did, then tell it what to do next without going back to the terminal that started it.",
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
      <div className="product-showcase-toolbar">
        <div className="product-showcase-tabs" role="tablist" aria-label="Longhouse session views">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              className={`product-showcase-tab ${activeTab === tab.id ? "active" : ""}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <p className="product-showcase-description">{activeConfig.description}</p>
      </div>

      <div className="product-showcase-content">
        <div className="product-showcase-frame">
          <AppScreenshotFrame
            src={activeConfig.image}
            videoSrc={activeConfig.video}
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
