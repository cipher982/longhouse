import { useEffect, useRef, useState, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchRecentModels } from "@/shared/api/index";
import { getProviderLabel } from "@/shared/lib/providers";
import "./model-chip.css";

export interface ModelPickerProps {
  deviceId: string | null;
  provider: string;
  value: string;
  onChange: (model: string) => void;
  testId: string;
  compact?: boolean;
  pickerRef?: RefObject<HTMLDetailsElement | null>;
}

const MODEL_TIME = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

// The rest of the launch sheet prices a machine in relative time ("online 4h",
// "Last seen 5 months ago"); a recency list is the same kind of statement, so an
// absolute "Apr 15, 2026, 7:11 AM" here read as a different unit for the same
// idea. Mirrors lastSeenLabel's shape rather than inventing a second one.
function lastUsedLabel(lastUsedAt: string | null): string {
  if (!lastUsedAt) return "Last used: unknown";
  const date = new Date(lastUsedAt);
  if (Number.isNaN(date.getTime())) return `Last used: ${lastUsedAt}`;
  const elapsedMs = date.getTime() - Date.now();
  const minutes = Math.round(elapsedMs / 60_000);
  if (Math.abs(minutes) < 60) return `Last used ${MODEL_TIME.format(minutes, "minute")}`;
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return `Last used ${MODEL_TIME.format(hours, "hour")}`;
  const days = Math.round(hours / 24);
  if (Math.abs(days) < 30) return `Last used ${MODEL_TIME.format(days, "day")}`;
  return `Last used ${date.toLocaleDateString(undefined, { dateStyle: "medium" })}`;
}
export default function ModelPicker({
  deviceId,
  provider,
  value,
  onChange,
  testId,
  compact = false,
  pickerRef: externalPickerRef,
}: ModelPickerProps) {
  const internalPickerRef = useRef<HTMLDetailsElement | null>(null);
  const detailsRef = externalPickerRef ?? internalPickerRef;
  const recentModelsQuery = useQuery({
    queryKey: ["recent-models", deviceId, provider],
    queryFn: () => fetchRecentModels(deviceId!, provider),
    enabled: Boolean(deviceId && provider),
    staleTime: 15_000,
    refetchOnMount: "always",
  });
  const selectedModel = value.trim();
  const providerLabel = getProviderLabel(provider);
  // The provider is already named by the row above in the launch sheet and by
  // the composer header, so a bare provider name here reads as a second value
  // rather than as a caption -- "Default / Codex" directly under
  // "Codex / Coding agent" looked like a duplicate row. Say what the row IS,
  // and why no model name appears when one is not pinned: the agent's own
  // config on that machine decides.
  const caption = selectedModel
    ? "Model · set for this session"
    : `Model · let ${providerLabel} on this machine choose`;

  const choose = (model: string) => {
    onChange(model);
    if (detailsRef.current) detailsRef.current.open = false;
  };
  if (compact) {
    return (
      <CompactModelPicker
        detailsRef={detailsRef}
        testId={testId}
        providerLabel={providerLabel}
        selectedModel={selectedModel}
        models={recentModelsQuery.data?.models ?? []}
        onChange={onChange}
        choose={choose}
      />
    );
  }
  return (
    <details
      ref={detailsRef}
      className="launch-choice launch-choice--nested"
      data-testid={testId}
    >
      <summary aria-haspopup="listbox">
        <span className="launch-choice-copy">
          <strong title={selectedModel || "Default"}>{selectedModel || "Default"}</strong>
          <small>{caption}</small>
        </span>
      </summary>
      <div className="launch-choice-panel model-picker-panel">
        <button
          type="button"
          className="launch-option-row"
          onClick={() => choose("")}
        >
          <span>
            <strong>Default</strong>
            <small>let {providerLabel} on this machine choose</small>
          </span>
          <span>{!selectedModel ? "✓" : ""}</span>
        </button>
        {(recentModelsQuery.data?.models ?? []).map((recent) => (
          <button
            key={recent.model}
            type="button"
            className="launch-option-row"
            onClick={() => choose(recent.model)}
          >
            <span>
              <strong>{recent.model}</strong>
              <small>{lastUsedLabel(recent.last_used_at)}</small>
            </span>
            <span>{recent.model === selectedModel ? "✓" : ""}</span>
          </button>
        ))}
        <label className="launch-manual-path">
          <span>Other model id…</span>
          <input
            type="text"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder="e.g. provider/model-id"
            autoComplete="off"
            spellCheck={false}
            data-testid={`${testId}-input`}
          />
        </label>
      </div>
    </details>
  );
}

interface CompactModelPickerProps {
  detailsRef: RefObject<HTMLDetailsElement | null>;
  testId: string;
  providerLabel: string;
  selectedModel: string;
  models: { model: string; last_used_at: string | null; label?: string | null }[];
  onChange: (model: string) => void;
  choose: (model: string) => void;
}

// The composer's model chip. It sits at the bottom of the window, so its menu
// opens upward; a menu that opened downward from here landed below the
// viewport (2026-10-06). The chip names the model the way the session header
// does ("opus 5.5") and the menu keeps the exact id under each name.
function CompactModelPicker({
  detailsRef,
  testId,
  providerLabel,
  selectedModel,
  models,
  onChange,
  choose,
}: CompactModelPickerProps) {
  useEffect(() => {
    const close = (event: Event) => {
      const details = detailsRef.current;
      if (!details?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
        details.open = false;
        details.querySelector("summary")?.focus();
        return;
      }
      if (event.target instanceof Node && !details.contains(event.target)) {
        details.open = false;
      }
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, [detailsRef]);

  // The free-text box is its own draft: it starts with an id the list does
  // not show and is cleared by picking a row, but typing never clears it,
  // even when a prefix of what is typed matches a listed id.
  const [manualDraft, setManualDraft] = useState<string | null>(null);
  // A model chosen elsewhere (a row, or an edited outbox entry restoring its
  // model) replaces the draft; the draft's own keystrokes never do.
  useEffect(() => {
    setManualDraft((draft) => (draft !== null && draft.trim() === selectedModel ? draft : null));
  }, [selectedModel]);
  const isListed = models.some((recent) => recent.model === selectedModel);
  const manualValue = manualDraft ?? (isListed ? "" : selectedModel);
  const pick = (model: string) => {
    setManualDraft(null);
    choose(model);
  };
  const selectedLabel = selectedModel
    ? models.find((recent) => recent.model === selectedModel)?.label || selectedModel
    : "Default model";
  return (
    <details ref={detailsRef} className="model-chip" data-testid={testId}>
      <summary
        aria-haspopup="listbox"
        aria-label={`Model: ${selectedLabel}`}
        title={selectedModel || `${providerLabel} on this machine chooses`}
      >
        <span className="model-chip__label">{selectedLabel}</span>
        <svg className="model-chip__caret" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M2 6.5 5 3.5l3 3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </summary>
      <div className="model-chip__panel" data-popover-panel>
        <div className="model-chip__heading">Model for the next turn</div>
        {models.map((recent) => {
          const selected = recent.model === selectedModel;
          return (
            <button
              key={recent.model}
              type="button"
              className={`model-chip__option${selected ? " is-selected" : ""}`}
              aria-pressed={selected}
              onClick={() => pick(recent.model)}
            >
              <span className="model-chip__option-copy">
                <span className="model-chip__option-name">{recent.label || recent.model}</span>
                <small>
                  <code>{recent.model}</code> · {lastUsedLabel(recent.last_used_at)}
                </small>
              </span>
              {selected ? <CheckIcon /> : null}
            </button>
          );
        })}
        {models.length > 0 ? <div className="model-chip__divider" /> : null}
        <button
          type="button"
          className={`model-chip__option${!selectedModel ? " is-selected" : ""}`}
          aria-pressed={!selectedModel}
          onClick={() => pick("")}
        >
          <span className="model-chip__option-copy">
            <span className="model-chip__option-name">Default</span>
            <small>{providerLabel}'s config on this machine decides</small>
          </span>
          {!selectedModel ? <CheckIcon /> : null}
        </button>
        <label className="model-chip__manual">
          <span>Other model id</span>
          <input
            type="text"
            value={manualValue}
            onChange={(event) => {
              setManualDraft(event.target.value);
              onChange(event.target.value);
            }}
            placeholder="provider/model-id"
            autoComplete="off"
            spellCheck={false}
            data-testid={`${testId}-input`}
          />
        </label>
      </div>
    </details>
  );
}

function CheckIcon() {
  return (
    <svg className="model-chip__check" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      <path d="M3 7.5 5.8 10 11 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
