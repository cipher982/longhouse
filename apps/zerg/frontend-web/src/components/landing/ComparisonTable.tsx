/**
 * ComparisonTable
 *
 * Shows how Longhouse compares to alternative approaches for
 * tracking AI coding sessions: grep JSONL, Claude built-in history,
 * or not tracking at all.
 */

import { CheckCircleIcon, XCircleIcon } from "../icons";

type Support = "yes" | "no" | "partial";

interface Feature {
  name: string;
  description: string;
  grepJsonl: Support;
  claudeHistory: Support;
  noTracking: Support;
  longhouse: Support;
}

const features: Feature[] = [
  {
    name: "Searchable",
    description: "Full-text search across all sessions",
    grepJsonl: "partial",
    claudeHistory: "partial",
    noTracking: "no",
    longhouse: "yes",
  },
  {
    name: "Cross-provider",
    description: "Claude, Codex, Gemini, Cursor in one place",
    grepJsonl: "no",
    claudeHistory: "no",
    noTracking: "no",
    longhouse: "yes",
  },
  {
    name: "Persistent",
    description: "Sessions survive tool updates and reinstalls",
    grepJsonl: "partial",
    claudeHistory: "no",
    noTracking: "no",
    longhouse: "yes",
  },
  {
    name: "Visual timeline",
    description: "See what happened and when, at a glance",
    grepJsonl: "no",
    claudeHistory: "no",
    noTracking: "no",
    longhouse: "yes",
  },
  {
    name: "Resume sessions",
    description: "Pick up where you left off with full context",
    grepJsonl: "no",
    claudeHistory: "partial",
    noTracking: "no",
    longhouse: "yes",
  },
  {
    name: "Self-hosted",
    description: "Your data stays on your machine",
    grepJsonl: "yes",
    claudeHistory: "no",
    noTracking: "yes",
    longhouse: "yes",
  },
];

const approaches = [
  { key: "grepJsonl" as const, label: "grep JSONL" },
  { key: "claudeHistory" as const, label: "Claude History" },
  { key: "noTracking" as const, label: "No Tracking" },
  { key: "longhouse" as const, label: "Longhouse" },
];

function SupportCell({ value }: { value: Support }) {
  if (value === "yes") {
    return (
      <span className="comparison-cell comparison-cell--yes" aria-label="Yes">
        <CheckCircleIcon width={20} height={20} />
      </span>
    );
  }
  if (value === "partial") {
    return (
      <span className="comparison-cell comparison-cell--partial" aria-label="Partial">
        <svg
          width={20}
          height={20}
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
      </span>
    );
  }
  return (
    <span className="comparison-cell comparison-cell--no" aria-label="No">
      <XCircleIcon width={20} height={20} />
    </span>
  );
}

export function ComparisonTable() {
  return (
    <section className="landing-comparison">
      <div className="landing-section-inner">
        <p className="landing-section-label">Why Longhouse?</p>
        <h2 className="landing-section-title">Compare the alternatives</h2>
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
                      <SupportCell value={feature[a.key]} />
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
