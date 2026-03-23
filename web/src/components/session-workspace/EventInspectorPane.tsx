import type { ReactNode } from "react";
import { EmptyState } from "../ui";
import type { TimelineSelection } from "../../lib/sessionWorkspace";
import {
  formatFullDate,
  getToolDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolSummary,
  isOutsideActiveContext,
  parseLonghouseOutput,
} from "../../lib/sessionWorkspace";

interface EventInspectorPaneProps {
  selection: TimelineSelection | null;
  onSelectKey: (key: string) => void;
}

function InspectorSection({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="inspector-section">
      <div className="inspector-section__label">{label}</div>
      {children}
    </div>
  );
}

export function EventInspectorPane({ selection, onSelectKey }: EventInspectorPaneProps) {
  if (!selection) {
    return (
      <div className="event-inspector">
        <div className="event-inspector__header">
          <div className="event-inspector__title">Tool Inspector</div>
          <div className="event-inspector__subtitle">Select a tool event to inspect raw details.</div>
        </div>
        <div className="event-inspector__body">
          <EmptyState
            title="No tool selected"
            description="Messages render directly in the transcript. Select a tool call to inspect input, output, and timing."
          />
        </div>
      </div>
    );
  }

  if (selection.kind === "message") {
    const outsideActiveContext = isOutsideActiveContext(selection.event);

    return (
      <div className="event-inspector">
        <div className="event-inspector__header">
          <div className="event-inspector__title">Transcript Focus</div>
          <div className="event-inspector__subtitle">{formatFullDate(selection.event.timestamp)}</div>
        </div>
        <div className="event-inspector__body">
          <EmptyState
            title="Message already visible in the transcript"
            description="Use this pane for tool calls, raw command output, and structured event metadata."
          />
          <InspectorSection label="Metadata">
            <div className="inspector-meta-list">
              <div className="inspector-meta-item">
                <span>Role</span>
                <strong>{selection.event.role}</strong>
              </div>
              <div className="inspector-meta-item">
                <span>Event ID</span>
                <strong>{selection.event.id}</strong>
              </div>
              {outsideActiveContext ? (
                <div className="inspector-meta-item">
                  <span>Context</span>
                  <strong>Outside active model context</strong>
                </div>
              ) : null}
            </div>
          </InspectorSection>
        </div>
      </div>
    );
  }

  if (selection.kind === "tool_batch") {
    return (
      <div className="event-inspector">
        <div className="event-inspector__header">
          <div className="event-inspector__title">Parallel tool batch</div>
          <div className="event-inspector__subtitle">
            {selection.batch.interactions.length} tool calls at {formatFullDate(selection.batch.timestamp)}
          </div>
        </div>
        <div className="event-inspector__body">
          <InspectorSection label="Tools">
            <div className="inspector-tool-list">
              {selection.batch.interactions.map((interaction) => {
                const info = getToolDisplayInfo(interaction.toolName);
                const exitCode = getToolExitCode(interaction);
                const duration = getToolDuration(interaction.callEvent, interaction.resultEvent);
                return (
                  <button
                    key={interaction.key}
                    type="button"
                    className="inspector-tool-list__item"
                    onClick={() => onSelectKey(`tool:${interaction.key}`)}
                  >
                    <div className="inspector-tool-list__item-header">
                      <span className="inspector-tool-list__icon" style={{ backgroundColor: info.color }}>
                        {info.icon}
                      </span>
                      <span className="inspector-tool-list__name">{info.displayName}</span>
                    </div>
                    <div className="inspector-tool-list__summary">
                      {getToolSummary(interaction) || "No input or output recorded"}
                    </div>
                    <div className="inspector-tool-list__meta">
                      {exitCode != null ? <span>exit {exitCode}</span> : null}
                      {duration ? <span>{duration}</span> : null}
                    </div>
                  </button>
                );
              })}
            </div>
          </InspectorSection>
        </div>
      </div>
    );
  }

  const info = getToolDisplayInfo(selection.interaction.toolName);
  const exitCode = getToolExitCode(selection.interaction);
  const duration = getToolDuration(selection.interaction.callEvent, selection.interaction.resultEvent);
  const outsideActiveContext =
    isOutsideActiveContext(selection.interaction.callEvent) ||
    isOutsideActiveContext(selection.interaction.resultEvent);
  const parsedOutput = selection.interaction.resultEvent?.tool_output_text
    ? parseLonghouseOutput(selection.interaction.resultEvent.tool_output_text)
    : null;
  const outputText = parsedOutput
    ? parsedOutput.output
    : selection.interaction.resultEvent?.tool_output_text || null;
  const hasInput =
    selection.interaction.callEvent?.tool_input_json != null &&
    Object.keys(selection.interaction.callEvent.tool_input_json).length > 0;

  return (
    <div className="event-inspector">
      <div className="event-inspector__header">
        <div className="event-inspector__title event-inspector__title--tool">
          <span className="event-inspector__tool-icon" style={{ backgroundColor: info.color }}>
            {info.icon}
          </span>
          <span>{info.displayName}</span>
        </div>
        <div className="event-inspector__subtitle">
          {formatFullDate(selection.interaction.callEvent?.timestamp ?? selection.interaction.timestamp)}
        </div>
      </div>
      <div className="event-inspector__body">
        <InspectorSection label="Summary">
          <div className="inspector-meta-list">
            <div className="inspector-meta-item">
              <span>Status</span>
              <strong>
                {selection.interaction.resultEvent
                  ? exitCode == null
                    ? "Completed"
                    : exitCode === 0
                      ? "Succeeded"
                      : `Failed (${exitCode})`
                  : selection.interaction.pairing === "orphan"
                    ? "Orphan result"
                    : "Pending"}
              </strong>
            </div>
            {duration ? (
              <div className="inspector-meta-item">
                <span>Duration</span>
                <strong>{duration}</strong>
              </div>
            ) : null}
            {outsideActiveContext ? (
              <div className="inspector-meta-item">
                <span>Context</span>
                <strong>Outside active model context</strong>
              </div>
            ) : null}
            {info.mcpNamespace ? (
              <div className="inspector-meta-item">
                <span>Namespace</span>
                <strong>{info.mcpNamespace}</strong>
              </div>
            ) : null}
          </div>
          {getToolSummary(selection.interaction) ? (
            <div className="inspector-summary-block">{getToolSummary(selection.interaction)}</div>
          ) : null}
        </InspectorSection>

        {hasInput ? (
          <InspectorSection label="Input">
            <pre className="inspector-code-block">
              {JSON.stringify(selection.interaction.callEvent?.tool_input_json, null, 2)}
            </pre>
          </InspectorSection>
        ) : null}

        <InspectorSection label="Output">
          {outputText ? (
            <>
              {parsedOutput?.wallTime || parsedOutput?.exitCode != null ? (
                <div className="inspector-output-meta">
                  {parsedOutput?.exitCode != null ? <span>exit {parsedOutput.exitCode}</span> : null}
                  {parsedOutput?.wallTime ? <span>{parsedOutput.wallTime}</span> : null}
                </div>
              ) : null}
              <pre className="inspector-code-block inspector-code-block--output">{outputText}</pre>
            </>
          ) : (
            <div className="inspector-empty-block">
              {selection.interaction.pairing === "pending"
                ? "Result not recorded yet."
                : "No output recorded."}
            </div>
          )}
        </InspectorSection>
      </div>
    </div>
  );
}
