import React, { useCallback, useMemo, useState } from "react";
import clsx from "clsx";
import { useQuery } from "@tanstack/react-query";
import { useShelf } from "../../lib/useShelfState";
import { fetchFiches, type FicheSummary } from "../../services/api";
import { getNodeIcon } from "../../lib/iconUtils";

type ToolPaletteItem = {
  type: string;
  name: string;
};

type ShelfSection = "fiches" | "tools";

const TOOL_ITEMS: ToolPaletteItem[] = [
  { type: "http-request", name: "HTTP Request" },
  { type: "url-fetch", name: "URL Fetch" },
];

const SECTION_STATE_STORAGE_KEY = "canvas_section_state";
const DEFAULT_SECTION_STATE: Record<ShelfSection, boolean> = {
  fiches: false,
  tools: false,
};

type DraggableFiche = { id: number; name: string };
type DraggableTool = { type: string; name: string };

interface FicheShelfProps {
  onFicheDragStart: (event: React.DragEvent, fiche: DraggableFiche) => void;
  onToolDragStart: (event: React.DragEvent, tool: DraggableTool) => void;
  onFichePointerDown: (event: React.PointerEvent, fiche: DraggableFiche) => void;
  onToolPointerDown: (event: React.PointerEvent, tool: DraggableTool) => void;
}

export function FicheShelf({ onFicheDragStart, onToolDragStart, onFichePointerDown, onToolPointerDown }: FicheShelfProps) {
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

  // Fetch fiches for the shelf
  const { data: fiches = [] } = useQuery<FicheSummary[]>({
    queryKey: ["fiches", { scope: "my" }],
    queryFn: () => fetchFiches({ scope: "my" }),
    refetchInterval: 2000, // Poll every 2 seconds
  });

  const filteredFiches = useMemo(() => {
    const normalized = searchTerm.trim().toLowerCase();
    if (!normalized) {
      return fiches;
    }
    return fiches.filter((fiche) => fiche.name.toLowerCase().includes(normalized));
  }, [fiches, searchTerm]);

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
        id="fiche-shelf"
        data-testid="fiche-shelf"
        className={clsx("fiche-shelf", { open: isShelfOpen })}
      >
        <section className="fiche-shelf-section shelf-search">
          <label htmlFor="canvas-shelf-search" className="shelf-search-label">
            Search
          </label>
          <input
            id="canvas-shelf-search"
            type="search"
            className="shelf-search-input"
            placeholder="Filter fiches or tools"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
          />
        </section>

        <section className="fiche-shelf-section">
          <button
            type="button"
            className="shelf-section-toggle"
            onClick={() => toggleSection("fiches")}
            aria-expanded={!collapsedSections.fiches}
            aria-controls="shelf-fiche-list"
          >
            <span className="caret">{collapsedSections.fiches ? "▸" : "▾"}</span>
            <span>Fiches</span>
            <span className="count">{filteredFiches.length}</span>
          </button>
          {!collapsedSections.fiches &&
            (filteredFiches.length > 0 ? (
              <div id="shelf-fiche-list" className="fiche-shelf-content">
                {filteredFiches.map((fiche) => (
                  <div
                    key={fiche.id}
                    className="fiche-shelf-item fiche-pill"
                    data-testid={`shelf-fiche-${fiche.id}`}
                    draggable={true}
                    role="button"
                    tabIndex={0}
                    aria-grabbed="false"
                    aria-label={`Drag fiche ${fiche.name} onto the canvas`}
                    onDragStart={(event) => onFicheDragStart(event, { id: fiche.id, name: fiche.name })}
                    onDragEnd={(event) => {
                      if (event.currentTarget instanceof HTMLElement) {
                        event.currentTarget.setAttribute('aria-grabbed', 'false');
                      }
                    }}
                    onPointerDown={(event) => onFichePointerDown(event as React.PointerEvent<HTMLDivElement>, { id: fiche.id, name: fiche.name })}
                  >
                    <div className="fiche-pill-icon">
                      {getNodeIcon("fiche", undefined, { width: 18, height: 18 })}
                    </div>
                    <div className="fiche-name">{fiche.name}</div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="shelf-empty">
                {searchTerm ? `No fiches found for "${searchTerm}".` : "No fiches available."}
              </p>
            ))}
        </section>

        <section
          id="tool-palette"
          data-testid="tool-palette"
          className="fiche-shelf-section"
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
