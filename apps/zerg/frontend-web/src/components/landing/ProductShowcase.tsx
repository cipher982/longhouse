/**
 * ProductShowcase
 *
 * Tabbed showcase of real product screenshots.
 * Replaces the demo video placeholder with actual product views.
 */

import { useState } from "react";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

type Tab = "chat" | "dashboard" | "canvas";

interface TabConfig {
  id: Tab;
  label: string;
  description: string;
  image: string;
  alt: string;
}

const tabs: TabConfig[] = [
  {
    id: "chat",
    label: "Chat",
    description: "Talk to your AI assistant naturally. It understands context, calls tools, and gets things done.",
    image: "/images/landing/chat-preview.png",
    alt: "Jarvis chat interface showing a conversation with AI assistant",
  },
  {
    id: "dashboard",
    label: "Dashboard",
    description: "Monitor all your agents in one place. See status, runs, and success rates at a glance.",
    image: "/images/landing/dashboard-preview.png",
    alt: "Dashboard showing agent status, runs, and monitoring",
  },
  {
    id: "canvas",
    label: "Workflow Builder",
    description: "Drag and drop to build workflows. Connect triggers, agents, and actions visually.",
    image: "/images/landing/canvas-preview.png",
    alt: "Visual workflow canvas with connected agent nodes",
  },
];

export function ProductShowcase() {
  const [activeTab, setActiveTab] = useState<Tab>("chat");
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
