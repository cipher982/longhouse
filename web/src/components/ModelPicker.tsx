import { useRef, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchRecentModels } from "../services/api";
import { getProviderLabel } from "../lib/providers";

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

  const choose = (model: string) => {
    onChange(model);
    if (detailsRef.current) detailsRef.current.open = false;
  };
  return (
    <details
      ref={detailsRef}
      className={`launch-choice launch-choice--nested${compact ? " model-picker--compact" : ""}`}
      data-testid={testId}
    >
      <summary aria-haspopup="listbox">
        <span className="launch-choice-copy">
          <strong title={selectedModel || "Default"}>{selectedModel || "Default"}</strong>
          <small>{getProviderLabel(provider)}</small>
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
            <small>let the CLI on this machine choose</small>
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
