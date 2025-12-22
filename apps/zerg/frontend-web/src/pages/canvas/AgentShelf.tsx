import React, { useCallback, useMemo, useState } from "react";
import clsx from "clsx";
import { useQuery } from "@tanstack/react-query";
import { useShelf } from "../../lib/useShelfState";
import { fetchAgents, type AgentSummary } from "../../services/api";
import { getNodeIcon } from "../../lib/iconUtils";

type ToolPaletteItem = {
  type: string;
  name: string;
};

type ShelfSection = "agents" | "tools";

const TOOL_ITEMS: ToolPaletteItem[] = [
  { type: "http-request", name: "HTTP Request" },
  { type: "url-fetch", name: "URL Fetch" },
];

const SECTION_STATE_STORAGE_KEY = "canvas_section_state";
const DEFAULT_SECTION_STATE: Record<ShelfSection, boolean> = {
  agents: false,
  tools: false,
};

type DraggableAgent = { id: number; name: string };
type DraggableTool = { type: string; name: string };

interface AgentShelfProps {
  onAgentDragStart: (event: React.DragEvent, agent: DraggableAgent) => void;
  onToolDragStart: (event: React.DragEvent, tool: DraggableTool) => void;
  onAgentPointerDown: (event: React.PointerEvent, agent: DraggableAgent) => void;
  onToolPointerDown: (event: React.PointerEvent, tool: DraggableTool) => void;
}

export function AgentShelf({ onAgentDragStart, onToolDragStart, onAgentPointerDown, onToolPointerDown }: AgentShelfProps) {
  const { isShelfOpen } = useShelf();
  const [searchTerm, setSearchTerm] = useState("");
  const [collapsedSections, setCollapsedSections] = useState<Record<ShelfSection, boolean>>(() => {
    if (typeof window === "undefined") {
      return { ...DEFAULT_SECTION_STATE };
    }
    try {
      const stored = window.localStorage.getItem(SECTION_STATE_STORAGE_KEY);
      if (!stored) {
        return { ...DEFAULT_SECTION_STATE };
      }
      const parsed = JSON.parse(stored) as Partial<Record<ShelfSection, boolean>>;
      return { ...DEFAULT_SECTION_STATE, ...parsed };
    } catch (error) {
      console.warn("Failed to parse shelf section state:", error);
      return { ...DEFAULT_SECTION_STATE };
    }
  });

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(SECTION_STATE_STORAGE_KEY, JSON.stringify(collapsedSections));
    } catch (error) {
      console.warn("Failed to persist shelf section state:", error);
    }
  }, [collapsedSections]);

  const toggleSection = useCallback((section: ShelfSection) => {
    setCollapsedSections((prev) => ({
      ...prev,
      [section]: !prev[section],
    }));
  }, []);

  // Fetch agents for the shelf
  const { data: agents = [] } = useQuery<AgentSummary[]>({
    queryKey: ["agents", { scope: "my" }],
    queryFn: () => fetchAgents({ scope: "my" }),
    refetchInterval: 2000, // Poll every 2 seconds
  });

  const filteredAgents = useMemo(() => {
    const normalized = searchTerm.trim().toLowerCase();
    if (!normalized) {
      return agents;
    }
    return agents.filter((agent) => agent.name.toLowerCase().includes(normalized));
  }, [agents, searchTerm]);

  const filteredTools = useMemo(() => {
    const normalized = searchTerm.trim().toLowerCase();
    if (!normalized) {
      return TOOL_ITEMS;
    }
    return TOOL_ITEMS.filter((tool) => tool.name.toLowerCase().includes(normalized));
  }, [searchTerm]);

  return (
    <>
      <div
        id="agent-shelf"
        data-testid="agent-shelf"
        className={clsx("agent-shelf", { open: isShelfOpen })}
      >
        <section className="agent-shelf-section shelf-search">
          <label htmlFor="canvas-shelf-search" className="shelf-search-label">
            Search
          </label>
          <input
            id="canvas-shelf-search"
            type="search"
            className="shelf-search-input"
            placeholder="Filter agents or tools"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
          />
        </section>

        <section className="agent-shelf-section">
          <button
            type="button"
            className="shelf-section-toggle"
            onClick={() => toggleSection("agents")}
            aria-expanded={!collapsedSections.agents}
            aria-controls="shelf-agent-list"
          >
            <span className="caret">{collapsedSections.agents ? "▸" : "▾"}</span>
            <span>Agents</span>
            <span className="count">{filteredAgents.length}</span>
          </button>
          {!collapsedSections.agents &&
            (filteredAgents.length > 0 ? (
              <div id="shelf-agent-list" className="agent-shelf-content">
                {filteredAgents.map((agent) => (
                  <div
                    key={agent.id}
                    className="agent-shelf-item agent-pill"
                    data-testid={`shelf-agent-${agent.id}`}
                    draggable={true}
                    role="button"
                    tabIndex={0}
                    aria-grabbed="false"
                    aria-label={`Drag agent ${agent.name} onto the canvas`}
                    onDragStart={(event) => onAgentDragStart(event, { id: agent.id, name: agent.name })}
                    onDragEnd={(event) => {
                      if (event.currentTarget instanceof HTMLElement) {
                        event.currentTarget.setAttribute('aria-grabbed', 'false');
                      }
                    }}
                    onPointerDown={(event) => onAgentPointerDown(event as React.PointerEvent<HTMLDivElement>, { id: agent.id, name: agent.name })}
                  >
                    <div className="agent-pill-icon">
                      {getNodeIcon("agent", undefined, { width: 18, height: 18 })}
                    </div>
                    <div className="agent-name">{agent.name}</div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="shelf-empty">
                {searchTerm ? `No agents found for "${searchTerm}".` : "No agents available."}
              </p>
            ))}
        </section>

        <section
          id="tool-palette"
          data-testid="tool-palette"
          className="agent-shelf-section"
        >
          <button
            type="button"
            className="shelf-section-toggle"
            onClick={() => toggleSection("tools")}
            aria-expanded={!collapsedSections.tools}
            aria-controls="shelf-tool-list"
          >
            <span className="caret">{collapsedSections.tools ? "▸" : "▾"}</span>
            <span>Tools</span>
            <span className="count">{filteredTools.length}</span>
          </button>
          {!collapsedSections.tools &&
            (filteredTools.length > 0 ? (
              <div id="shelf-tool-list" className="tool-palette-content">
                {filteredTools.map((tool) => {
                  return (
                    <div
                      key={tool.type}
                      className="tool-palette-item"
                      data-testid={`tool-${tool.type}`}
                      draggable={true}
                      role="button"
                      tabIndex={0}
                      aria-grabbed="false"
                      aria-label={`Drag tool ${tool.name} onto the canvas`}
                      onDragStart={(event) => onToolDragStart(event, tool)}
                      onDragEnd={(event) => {
                        if (event.currentTarget instanceof HTMLElement) {
                          event.currentTarget.setAttribute('aria-grabbed', 'false');
                        }
                      }}
                      onPointerDown={(event) => onToolPointerDown(event as React.PointerEvent<HTMLDivElement>, tool)}
                    >
                      <div className="tool-icon">
                        {getNodeIcon("tool", tool.type, { width: 18, height: 18 })}
                      </div>
                      <div className="tool-name">{tool.name}</div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="shelf-empty">
                {searchTerm ? `No tools found for "${searchTerm}".` : "No tools available."}
              </p>
            ))}
        </section>
      </div>

      {/* Scrim overlay (decorative, pointer-events: none to allow drag/drop) */}
      <div
        className={clsx("shelf-scrim", { "shelf-scrim--visible": isShelfOpen })}
      />
    </>
  );
}
