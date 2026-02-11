/**
 * ComparisonTable
 *
 * Shows how Longhouse compares to alternative approaches for
 * tracking AI coding sessions: no tracking, grep JSONL, Claude
 * built-in history, or Longhouse.
 *
 * Uses descriptive text labels (not just icons) so HN readers
 * can quickly scan the value proposition.
 */

import { CheckCircleIcon, XCircleIcon } from "../icons";

type CellValue =
  | { type: "yes"; label?: string }
  | { type: "no" }
  | { type: "partial"; label: string }
  | { type: "na" };

interface Feature {
  name: string;
  description: string;
  noTracking: CellValue;
  grepJsonl: CellValue;
  claudeHistory: CellValue;
  longhouse: CellValue;
}

const features: Feature[] = [
  {
    name: "Searchable",
    description: "Find that session where you fixed auth",
    noTracking: { type: "no" },
    grepJsonl: { type: "partial", label: "Regex only" },
    claudeHistory: { type: "partial", label: "Limited" },
    longhouse: { type: "yes", label: "Full-text search" },
  },
  {
    name: "Cross-tool",
    description: "All your AI agents in one place",
    noTracking: { type: "no" },
    grepJsonl: { type: "no" },
    claudeHistory: { type: "partial", label: "Claude only" },
    longhouse: { type: "yes", label: "Claude + Codex + Gemini" },
  },
  {
    name: "Persistent",
    description: "Sessions survive updates and reinstalls",
    noTracking: { type: "no" },
    grepJsonl: { type: "partial", label: "If you know where" },
    claudeHistory: { type: "partial", label: "Per-project" },
    longhouse: { type: "yes", label: "Unified timeline" },
  },
  {
    name: "Visual timeline",
    description: "See what happened and when, at a glance",
    noTracking: { type: "no" },
    grepJsonl: { type: "no" },
    claudeHistory: { type: "no" },
    longhouse: { type: "yes" },
  },
  {
    name: "Resume sessions",
    description: "Pick up where you left off with full context",
    noTracking: { type: "no" },
    grepJsonl: { type: "no" },
    claudeHistory: { type: "partial", label: "Built-in" },
    longhouse: { type: "yes", label: "Timeline + Forum" },
  },
  {
    name: "Self-hosted",
    description: "Your data stays on your machine",
    noTracking: { type: "na" },
    grepJsonl: { type: "na" },
    claudeHistory: { type: "no" },
    longhouse: { type: "yes" },
  },
];

type ApproachKey = "noTracking" | "grepJsonl" | "claudeHistory" | "longhouse";

const approaches: { key: ApproachKey; label: string }[] = [
  { key: "noTracking", label: "No Tracking" },
  { key: "grepJsonl", label: "grep JSONL" },
  { key: "claudeHistory", label: "Claude History" },
  { key: "longhouse", label: "Longhouse" },
];

function CellContent({ value }: { value: CellValue }) {
  if (value.type === "yes") {
    return (
      <span className="comparison-cell comparison-cell--yes">
        <CheckCircleIcon width={18} height={18} aria-hidden="true" />
        {value.label && <span className="comparison-cell-label">{value.label}</span>}
      </span>
    );
  }
  if (value.type === "partial") {
    return (
      <span className="comparison-cell comparison-cell--partial">
        <svg
          width={18}
          height={18}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="8" y1="12" x2="16" y2="12" />
        </svg>
        <span className="comparison-cell-label">{value.label}</span>
      </span>
    );
  }
  if (value.type === "na") {
    return (
      <span className="comparison-cell comparison-cell--na" aria-label="Not applicable">
        <span className="comparison-cell-label comparison-cell-label--muted">N/A</span>
      </span>
    );
  }
  return (
    <span className="comparison-cell comparison-cell--no" aria-label="No">
      <XCircleIcon width={18} height={18} />
    </span>
  );
}

export function ComparisonTable() {
  return (
    <section className="landing-comparison">
      <div className="landing-section-inner">
        <p className="landing-section-label">Why Longhouse?</p>
        <h2 className="landing-section-title">Why not just grep?</h2>
        <p className="landing-section-subtitle">
          Most developers lose their AI session history. Here is how the options stack up.
        </p>

        <div className="comparison-table-wrapper">
          <table className="comparison-table" role="table">
            <thead>
              <tr>
                <th className="comparison-feature-header">Feature</th>
                {approaches.map((a) => (
                  <th
                    key={a.key}
                    className={`comparison-approach-header ${a.key === "longhouse" ? "comparison-approach-header--highlighted" : ""}`}
                  >
                    {a.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {features.map((feature) => (
                <tr key={feature.name}>
                  <td className="comparison-feature-name">
                    <span className="comparison-feature-title">{feature.name}</span>
                    <span className="comparison-feature-desc">{feature.description}</span>
                  </td>
                  {approaches.map((a) => (
                    <td
                      key={a.key}
                      className={a.key === "longhouse" ? "comparison-td--highlighted" : ""}
                    >
                      <CellContent value={feature[a.key]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
