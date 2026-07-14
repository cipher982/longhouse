export type {
  EventFilter,
  ManagedLaunchSuggestion,
  NoiseGroup,
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
  formatExplorationSummary,
  getPreferredSelectionKey,
  getToolDisplayInfo,
  getToolDuration,
  getToolExitCode,
  getToolSummary,
  getToolTier,
  isAgentToolInteraction,
  isExplorationEligible,
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
