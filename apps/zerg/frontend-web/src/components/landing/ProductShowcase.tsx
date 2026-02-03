/**
 * ProductShowcase
 *
 * Tabbed showcase of real product screenshots.
 * Shows Timeline and Session Detail views.
 */

import { useState } from "react";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

type Tab = "timeline" | "session";

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
    description: "All your AI coding sessions in one place. See when, what, and how long.",
    image: "/images/landing/dashboard-preview.png",
    alt: "Session timeline showing Claude Code sessions with timestamps and summaries",
  },
  {
    id: "session",
    label: "Session Detail",
    description: "Expand any session to see every tool call, file edit, and conversation turn.",
    image: "/images/landing/chat-preview.png",
    alt: "Detailed session view showing tool calls and conversation",
  },
];

export function ProductShowcase() {
  const [activeTab, setActiveTab] = useState<Tab>("timeline");
  const activeConfig = tabs.find((t) => t.id === activeTab)!;

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
          />
        </div>
      </div>
    </div>
  );
}
