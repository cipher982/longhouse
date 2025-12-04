export const TOOL_GROUPS: Record<string, string[]> = {
  github: [
    "github_create_issue",
    "github_list_issues",
    "github_get_issue",
    "github_add_comment",
    "github_list_pull_requests",
    "github_get_pull_request",
  ],
  jira: [
    "jira_create_issue",
    "jira_list_issues",
    "jira_get_issue",
    "jira_add_comment",
    "jira_transition_issue",
    "jira_update_issue",
  ],
  slack: ["send_slack_webhook"],
  discord: ["send_discord_webhook"],
  email: ["send_email"],
  sms: ["send_sms"],
  linear: [
    "linear_create_issue",
    "linear_list_issues",
    "linear_get_issue",
    "linear_update_issue",
    "linear_add_comment",
    "linear_list_teams",
  ],
  notion: [
    "notion_create_page",
    "notion_get_page",
    "notion_update_page",
    "notion_search",
    "notion_query_database",
    "notion_append_blocks",
  ],
  imessage: ["send_imessage", "list_imessage_messages"],
};

export const UTILITY_TOOLS = [
  "get_current_time",
  "datetime_diff",
  "math_eval",
  "http_request",
  "http_post",
  "generate_uuid",
  "container_exec",
];






