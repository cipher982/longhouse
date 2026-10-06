/**
 * ⌘K session switcher (session-view-terminal-parity C8): type to filter the
 * rail's sessions, ↑/↓ to move, ↵ to open. The preview reads the same
 * workspace cache entry the session page uses, so a session the rail has
 * already warmed previews instantly; one it has not is fetched once the
 * highlight rests on it, not for every row an arrow key passes over.
 */
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentSessionWorkspaceResponse } from "@/shared/api/agents";
import { agentSessionWorkspaceQueryOptions } from "@/shared/api/useAgentSessions";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { useEscapeKey } from "@/shared/hooks/useEscapeKey";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { SearchIcon } from "@/shared/ui/icons";
import type { RailActiveSession } from "./sessionRailContext";

const PREVIEW_CHARS = 600;

/** Cut long markdown at a paragraph, else a line, else a hard cut, and mark
 * the cut with "…". A cut that leaves a code fence open is moved back to
 * before that fence, so the preview never renders a runaway code block. */
export function trimPreviewMarkdown(text: string, max = PREVIEW_CHARS): string {
  if (text.length <= max) return text;
  const head = text.slice(0, max);
  const block = head.lastIndexOf("\n\n");
  const line = head.lastIndexOf("\n");
  let cut = block > max / 3 ? head.slice(0, block) : line > max / 3 ? head.slice(0, line) : head;
  if ((cut.match(/^```/gm) ?? []).length % 2 === 1) {
    cut = cut.slice(0, cut.lastIndexOf("```"));
  }
  return `${cut.trimEnd()}…`;
}

function PreviewMarkdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

/** Every whitespace-separated term must appear in the title, machine or provider. */
export function filterSwitcherRows<T extends Pick<RailActiveSession, "title" | "host" | "provider">>(
  rows: readonly T[],
  query: string,
): T[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...rows];
  return rows.filter((row) => {
    const haystack = [row.title, row.host, row.provider].filter(Boolean).join(" ").toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

/** The newest ask and the newest reply, from a cached workspace page, and
 * whether the ask came after the reply (it is still waiting for one). */
export function previewFromWorkspace(workspace: AgentSessionWorkspaceResponse | undefined) {
  const items = workspace?.projection.items ?? [];
  let ask: string | null = null;
  let reply: string | null = null;
  let askIsNewer = false;
  for (let index = items.length - 1; index >= 0 && (ask == null || reply == null); index -= 1) {
    const event = items[index].event;
    const text = event?.content_text?.trim();
    if (!event || !text || event.tool_name) continue;
    if (event.role === "assistant" && reply == null) reply = trimPreviewMarkdown(text);
    if (event.role === "user" && ask == null) {
      ask = trimPreviewMarkdown(text);
      askIsNewer = reply == null;
    }
  }
  return { ask, reply, askIsNewer };
}

/** The highlighted session's workspace: whatever the cache already holds at
 * once, and a fetch only after the highlight has rested for a moment. */
function useSwitcherPreview(sessionId: string) {
  const queryClient = useQueryClient();
  const settledId = useDebouncedValue(sessionId, 250);
  const options = agentSessionWorkspaceQueryOptions(sessionId, { limit: 200 });
  const query = useQuery({ ...options, enabled: settledId === sessionId });
  const cached = queryClient.getQueryData<AgentSessionWorkspaceResponse>(options.queryKey);
  return { data: query.data ?? cached, loading: !cached && query.data == null };
}

function SwitcherPreview({ row }: { row: RailActiveSession }) {
  const { data, loading } = useSwitcherPreview(row.id);
  const { ask, reply, askIsNewer } = previewFromWorkspace(data);
  const askBlock = ask ? (
    <div className="session-switcher__ask">
      <span>{askIsNewer ? "Newest ask, no reply yet" : "Last ask"}</span>
      <div className="session-switcher__md">
        <PreviewMarkdown text={ask} />
      </div>
    </div>
  ) : null;
  return (
    <div className="session-switcher__preview" data-testid="session-switcher-preview">
      <h3>{row.title}</h3>
      <div className="session-switcher__meta">
        {[row.provider, row.host, row.stateText].filter(Boolean).join(" · ")}
      </div>
      {askIsNewer ? askBlock : null}
      {reply ? (
        <>
          {askIsNewer ? <p className="session-switcher__label">Earlier reply</p> : null}
          <div className="session-switcher__reply session-switcher__md" data-testid="session-switcher-reply">
            <PreviewMarkdown text={reply} />
          </div>
        </>
      ) : loading ? (
        <p className="session-switcher__empty">Loading…</p>
      ) : askIsNewer ? null : (
        <p className="session-switcher__empty">No reply yet.</p>
      )}
      {askIsNewer ? null : askBlock}
    </div>
  );
}

export function SessionSwitcher({
  rows,
  activeSessionId,
  shortcutLabel,
  onOpen,
  onClose,
}: {
  rows: readonly RailActiveSession[];
  activeSessionId: string | null;
  shortcutLabel: string;
  onOpen: (sessionId: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const matches = useMemo(() => filterSwitcherRows(rows, query), [rows, query]);
  const selected = matches[Math.min(highlight, Math.max(0, matches.length - 1))] ?? null;
  const inputRef = useRef<HTMLInputElement>(null);
  useEscapeKey(onClose, true);

  // The keyboard selection never leaves the visible list.
  useEffect(() => {
    if (!selected) return;
    document.getElementById(`session-switcher-${selected.id}`)?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setHighlight((index) => Math.min(Math.max(0, index + step), Math.max(0, matches.length - 1)));
    } else if (event.key === "Enter" && selected) {
      event.preventDefault();
      onOpen(selected.id);
    }
  };

  return (
    <div
      className="session-switcher__scrim"
      data-testid="session-switcher"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="session-switcher"
        role="dialog"
        aria-modal="true"
        aria-label="Switch session"
        onKeyDown={(event) => {
          // The filter field is the dialog's one stop; Tab never leaves it.
          if (event.key === "Tab") {
            event.preventDefault();
            inputRef.current?.focus();
          }
        }}
      >
        <label className="session-switcher__query">
          <SearchIcon width={15} height={15} />
          <input
            ref={inputRef}
            autoFocus
            value={query}
            placeholder="Switch to a session…"
            aria-label="Filter sessions"
            onChange={(event) => {
              setQuery(event.target.value);
              setHighlight(0);
            }}
            onKeyDown={onKeyDown}
            role="combobox"
            aria-expanded="true"
            aria-controls="session-switcher-list"
            aria-activedescendant={selected ? `session-switcher-${selected.id}` : undefined}
          />
          <span className="session-switcher__keys">↑↓ move · ↵ open · esc close · {shortcutLabel}</span>
        </label>
        <ol className="session-switcher__list" id="session-switcher-list" role="listbox">
          {matches.length === 0 ? <li className="session-switcher__empty">No session matches.</li> : null}
          {matches.map((row, index) => (
            <li key={row.id} role="presentation">
              <button
                type="button"
                id={`session-switcher-${row.id}`}
                role="option"
                aria-selected={row.id === selected?.id}
                className={`session-rail__row${row.id === selected?.id ? " is-active" : ""}`}
                data-testid="session-switcher-row"
                onMouseEnter={() => setHighlight(index)}
                onClick={() => onOpen(row.id)}
              >
                {row.provider ? (
                  <ProviderGlyph provider={row.provider} size={14} className="session-rail__glyph" />
                ) : (
                  <span className="session-rail__glyph" aria-hidden="true" />
                )}
                <span className="session-rail__title">{row.title}</span>
                <span className="session-rail__key">
                  <span className={`session-rail__dot session-rail__dot--${row.tone}`} aria-hidden="true" />
                  {row.id === activeSessionId ? "open" : null}
                </span>
                <span className="session-rail__sub">{[row.host, row.stateText].filter(Boolean).join(" · ")}</span>
              </button>
            </li>
          ))}
        </ol>
        {selected ? <SwitcherPreview row={selected} /> : <div className="session-switcher__preview" />}
      </div>
    </div>
  );
}
