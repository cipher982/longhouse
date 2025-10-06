//! Constants for the frontend application
//!
//! This module centralizes commonly used string literals to prevent typos
//! and enable safe refactoring across the codebase.

// CSS Class Names
pub const CSS_TAB_BUTTON: &str = "tab-button";
pub const CSS_TAB_BUTTON_ACTIVE: &str = "tab-button active";
pub const CSS_MODAL: &str = "modal";
pub const CSS_HIDDEN: &str = "hidden";
pub const CSS_VISIBLE: &str = "visible";
pub const CSS_MESSAGE_USER: &str = "message user-message";
pub const CSS_MESSAGE_ASSISTANT: &str = "message assistant-message";

// Utility CSS Classes (extracted from inline styles)
pub const CSS_FORM_ROW: &str = "form-row";
pub const CSS_FORM_ROW_SM: &str = "form-row-sm";
pub const CSS_FORM_ROW_XS: &str = "form-row-xs";
pub const CSS_CARD: &str = "card";
pub const CSS_ACTIONS_ROW: &str = "actions-row";
pub const CSS_SUCCESS_TEXT: &str = "success-text";
pub const CSS_SCHEDULE_SUMMARY: &str = "schedule-summary";
pub const CSS_TRIGGERS_LIST: &str = "triggers-list";
pub const CSS_EMPTY_STATE: &str = "empty-state";
pub const CSS_PRESERVE_WHITESPACE: &str = "preserve-whitespace";

// Message Roles
pub const ROLE_USER: &str = "user";
pub const ROLE_ASSISTANT: &str = "assistant";
pub const ROLE_TOOL: &str = "tool";
pub const ROLE_SYSTEM: &str = "system";

// Agent/Run Status Values
pub const STATUS_RUNNING: &str = "running";
pub const STATUS_IDLE: &str = "idle";
pub const STATUS_ERROR: &str = "error";
pub const STATUS_SCHEDULED: &str = "scheduled";
pub const STATUS_PROCESSING: &str = "processing";
pub const STATUS_COMPLETE: &str = "complete";
pub const STATUS_SUCCESS: &str = "success";

// Element IDs (commonly referenced)
pub const ID_GLOBAL_CANVAS_TAB: &str = "global-canvas-tab";
pub const ID_GLOBAL_DASHBOARD_TAB: &str = "global-dashboard-tab";
pub const ID_GLOBAL_LOGIN_OVERLAY: &str = "global-login-overlay";
pub const ID_AGENT_TRIGGERS_TAB: &str = "agent-triggers-tab";

// WebSocket/Network Message Types
pub const WS_TYPE_ERROR: &str = "error";
pub const WS_TYPE_PONG: &str = "pong";
pub const WS_TYPE_DISCONNECT: &str = "disconnect";
pub const WS_TYPE_PING: &str = "ping";

// Event Types
pub const EVENT_SYSTEM_STATUS: &str = "system_status";
pub const EVENT_THREAD_MESSAGE_CREATED: &str = "thread_message_created";

// Default Values
pub const DEFAULT_TOOL_NAME: &str = "tool";
pub const DEFAULT_AGENT_COLOR: &str = "#ffecb3"; // Light amber
pub const DEFAULT_AGENT_NODE_COLOR: &str = "#2ecc71"; // Green for agent identity nodes
pub const DEFAULT_AGENT_NAME: &str = "New Agent";
pub const DEFAULT_SYSTEM_INSTRUCTIONS: &str = "You are a helpful AI assistant.";
pub const DEFAULT_TASK_INSTRUCTIONS: &str = "Complete the given task.";
pub const DEFAULT_THREAD_TITLE: &str = "New Thread";

// Node dimensions
pub const DEFAULT_NODE_WIDTH: f64 = 200.0;
pub const DEFAULT_NODE_HEIGHT: f64 = 100.0;

// Status Colors (for canvas nodes)
pub const COLOR_IDLE: &str = "#ffecb3"; // Light amber
pub const COLOR_PROCESSING: &str = "#b3e5fc"; // Light blue
pub const COLOR_COMPLETE: &str = "#c8e6c9"; // Light green
pub const COLOR_ERROR: &str = "#ffcdd2"; // Light red

// Canvas colors
pub const CANVAS_BACKGROUND_COLOR: &str = "#33334a";
pub const NODE_COLOR_GENERIC: &str = "#ffffff";
pub const SHADOW_COLOR: &str = "rgba(0, 0, 0, 0.15)";
pub const NODE_TEXT_COLOR: &str = "#2c3e50";
pub const CONNECTION_LINE_COLOR: &str = "#95a5a6";

// Node border colors
pub const NODE_BORDER_DEFAULT: &str = "#e0e0e0";
pub const NODE_BORDER_SELECTED: &str = "#3498db";
pub const NODE_BORDER_AGENT_IDLE: &str = "#95a5a6";
pub const NODE_BORDER_AGENT_PROCESSING_BASE: &str = "52, 152, 219"; // RGB values for animation
pub const NODE_BORDER_AGENT_ERROR: &str = "#e74c3c";
pub const NODE_BORDER_AGENT_SCHEDULED: &str = "#9b59b6";
pub const NODE_BORDER_AGENT_PAUSED: &str = "#f39c12";

// Node fill colors
pub const NODE_FILL_AGENT_IDENTITY: &str = "rgba(255, 255, 255, 0.98)";
pub const NODE_FILL_USER_INPUT: &str = "rgba(52, 152, 219, 0.1)";
pub const NODE_FILL_RESPONSE: &str = "rgba(46, 204, 113, 0.1)";

// Modern gradient colors for agent nodes
pub const AGENT_GRADIENT_START: &str = "#ffffff";
pub const AGENT_GRADIENT_END: &str = "#f8fafc";
pub const AGENT_ACCENT_COLOR: &str = "#6366f1";
pub const AGENT_TEXT_PRIMARY: &str = "#1f2937";
pub const AGENT_TEXT_SECONDARY: &str = "#6b7280";
pub const AGENT_BORDER_SUBTLE: &str = "#e5e7eb";

// Status specific colors with better contrast
pub const STATUS_IDLE_COLOR: &str = "#10b981";
pub const STATUS_PROCESSING_COLOR: &str = "#3b82f6";
pub const STATUS_ERROR_COLOR: &str = "#ef4444";
pub const STATUS_SUCCESS_COLOR: &str = "#22c55e";
pub const STATUS_SCHEDULED_COLOR: &str = "#8b5cf6";
pub const STATUS_PAUSED_COLOR: &str = "#f59e0b";

// Status background colors (lighter versions)
pub const STATUS_IDLE_BG: &str = "rgba(16, 185, 129, 0.1)";
pub const STATUS_PROCESSING_BG: &str = "rgba(59, 130, 246, 0.1)";
pub const STATUS_ERROR_BG: &str = "rgba(239, 68, 68, 0.1)";
pub const STATUS_SUCCESS_BG: &str = "rgba(34, 197, 94, 0.1)";
pub const STATUS_SCHEDULED_BG: &str = "rgba(139, 92, 246, 0.1)";
pub const STATUS_PAUSED_BG: &str = "rgba(245, 158, 11, 0.1)";

// Button Types
pub const BUTTON_TYPE_BUTTON: &str = "button";
pub const BUTTON_TYPE_SUBMIT: &str = "submit";

// HTML Attributes
pub const ATTR_TYPE: &str = "type";
pub const ATTR_DATA_TOOL_CALL_ID: &str = "data-tool-call-id";
pub const ATTR_DATA_TESTID: &str = "data-testid";
