export type {
  EventFilter,
  ManagedLaunchSuggestion,
  ActivityGroup,
  SessionInteractionCapabilities,
  SessionInteractionMode,
  TimelineAction,
  TimelineItem,
  TimelineModel,
  TimelineSeam,
  TimelineSelection,
  ToolInteraction,
} from "./types";

export type { ToolTier } from "./toolTiers.generated";

export {
  formatContinuationStamp,
  formatDuration,
  formatFullDate,
  formatProviderLabel,
  formatTime,
  getProviderColor,
  getSessionOriginLabel,
  getTimelineMessagePreview,
  normalizeSessionOriginLabel,
  truncatePath,
} from "./formatters";

export {
  buildTimelineModel,
  EXPLORATION_OVERFLOW_VISIBLE,
  formatActivitySummary,
  formatToolInput,
  getPreferredSelectionKey,
  getInteractionDisplayInfo,
  getToolDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolInputRecord,
  getToolSummary,
  getToolTier,
  isAgentToolInteraction,
  isActivityEligible,
  isOutsideActiveContext,
  isToolInteractionDropped,
  isToolInteractionRunning,
  parseLonghouseOutput,
  projectionItemsWithTranscriptPreview,
  shouldRenderTranscriptPreview,
  splitExplorationOverflow,
  timelineItemContainsSelection,
} from "./timelineModel";

export { getSessionInteractionCapabilities } from "./interaction";
