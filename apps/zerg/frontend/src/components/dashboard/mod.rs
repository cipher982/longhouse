pub mod ws_manager;
#[cfg(test)]
mod ws_manager_test;

pub use ws_manager::{cleanup_dashboard_ws, init_dashboard_ws};

use crate::constants::{
    // DEFAULT_THREAD_TITLE (unused)
    DEFAULT_AGENT_NAME,
    DEFAULT_SYSTEM_INSTRUCTIONS,
    DEFAULT_TASK_INSTRUCTIONS,
    ATTR_DATA_TESTID,
};
use crate::state::APP_STATE;
use crate::toast;
use crate::ui_components::{create_button, ButtonConfig};
use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement};
use crate::debug_log;

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

/// Format an ISO‑8601 timestamp (`YYYY-MM-DDTHH:MM:SSZ`) into a short readable
/// form shown in the dashboard table: `YYYY‑MM‑DD HH:MM` (UTC).
fn format_datetime_short(iso: &str) -> String {
    // Very small helper – avoid pulling chrono in WASM build.
    // Expected formats: "2025-04-21T14:03:12Z" or with fractional seconds.
    let parts: Vec<&str> = iso.split('T').collect();
    if parts.len() != 2 {
        return iso.to_string();
    }
    let date = parts[0];
    let time = parts[1].trim_end_matches('Z');
    // keep only HH:MM
    let time_short = &time[..std::cmp::min(5, time.len())];
    format!("{} {}", date, time_short)
}

// Use generated AgentStatus from API contracts
use crate::generated::api_contracts::AgentStatus;

// Agent data structure for the dashboard
#[derive(Clone, Debug)]
pub struct Agent {
    pub id: u32,
    pub name: String,
    pub status: AgentStatus,
    pub last_run: Option<String>,
    pub next_run: Option<String>,
    pub success_rate: f64,
    pub run_count: u32,
    pub last_run_success: Option<bool>,

    // Stores the most recent error message (if any) returned by the backend
    pub last_error: Option<String>,

    // ---------------- Ownership (All agents scope) -------------------
    /// Owner id – present only when the dashboard is in *All agents* scope
    /// and the backend embeds the `owner` payload.
    pub owner_id: Option<u32>,

    /// Display name (or email if None) of the owner.
    pub owner_label: Option<String>,

    /// Optional avatar URL for the owner.
    pub owner_avatar_url: Option<String>,
}

impl Agent {
    #[allow(dead_code)]
    pub fn new(id: u32, name: String) -> Self {
        Self {
            id,
            name,
            status: AgentStatus::Idle,
            last_run: None,
            next_run: None,
            success_rate: 0.0,
            run_count: 0,
            last_run_success: None,

            last_error: None,

            // Ownership – not set in this helper
            owner_id: None,
            owner_label: None,
            owner_avatar_url: None,
        }
    }
}

// Main function to render the dashboard
pub fn render_dashboard(document: &Document) -> Result<(), JsValue> {
    let dashboard = document
        .get_element_by_id("dashboard")
        .ok_or(JsValue::from_str("Could not find dashboard element"))?;

    // Clear existing content
    dashboard.set_inner_html("");

    // Check loading state
    let is_loading = APP_STATE.with(|state| {
        let state = state.borrow();
        state.is_loading
    });

    if is_loading {
        // Show loading indicator with aria-live region
        let loading_div = document.create_element("div")?;
        loading_div.set_class_name("loading-indicator");
        loading_div.set_attribute("aria-live", "polite")?;
        loading_div.set_attribute("aria-label", "Loading status")?;
        loading_div.set_inner_html("Loading agents...");
        dashboard.append_child(&loading_div)?;
        return Ok(());
    }

    // Create header area with search and create button
    let header = create_dashboard_header(document)?;
    dashboard.append_child(&header)?;

    // Create the table
    let table = create_agents_table(document)?;
    dashboard.append_child(&table)?;

    // Populate the table with data
    populate_agents_table(document)?;

    Ok(())
}

// Function to refresh the dashboard based on the latest state
pub fn refresh_dashboard(document: &Document) -> Result<(), JsValue> {
    // Ensure both container and dashboard elements exist
    if document.get_element_by_id("dashboard-container").is_none() {
        // If container doesn't exist, set up the dashboard from scratch
        setup_dashboard(document)?;
    } else if document.get_element_by_id("dashboard").is_none() {
        // If container exists but dashboard doesn't, create dashboard
        let container = document
            .get_element_by_id("dashboard-container")
            .ok_or(JsValue::from_str("Dashboard container not found"))?;

        let dashboard = document.create_element("div")?;
        dashboard.set_id("dashboard");
        dashboard.set_class_name("dashboard");
        container.append_child(&dashboard)?;
    }

    // Now render the dashboard content
    render_dashboard(document)?;
    Ok(())
}

// Function to set up the dashboard in the application
pub fn setup_dashboard(document: &Document) -> Result<(), JsValue> {
    // Initialize WebSocket manager first
    init_dashboard_ws()?;

    // Check if there's already a dashboard container
    if document.get_element_by_id("dashboard-container").is_none() {
        // Create a container for the dashboard
        let dashboard_container = document.create_element("div")?;
        dashboard_container.set_id("dashboard-container");
        dashboard_container.set_class_name("dashboard-container");

        // Create the dashboard element itself
        let dashboard = document.create_element("div")?;
        dashboard.set_id("dashboard");
        dashboard.set_class_name("dashboard");
        dashboard_container.append_child(&dashboard)?;

        // Get the body element or another appropriate parent
        let app_container = document
            .get_element_by_id("app-container")
            .ok_or(JsValue::from_str("Could not find app-container"))?;

        // Append the dashboard container
        app_container.append_child(&dashboard_container)?;
    } else {
        // If container exists but dashboard doesn't, create dashboard
        let container = document
            .get_element_by_id("dashboard-container")
            .ok_or(JsValue::from_str("Dashboard container not found"))?;

        if document.get_element_by_id("dashboard").is_none() {
            let dashboard = document.create_element("div")?;
            dashboard.set_id("dashboard");
            dashboard.set_class_name("dashboard");
            container.append_child(&dashboard)?;
        }
    }

    // Now render the dashboard content
    render_dashboard(document)?;

    Ok(())
}

// Function to clean up the dashboard
#[allow(dead_code)]
pub fn cleanup_dashboard() -> Result<(), JsValue> {
    // Clean up WebSocket manager
    cleanup_dashboard_ws()?;

    // Clean up DOM elements if needed
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            if let Some(container) = document.get_element_by_id("dashboard-container") {
                container.set_inner_html("");
            }
        }
    }

    Ok(())
}

// Create the dashboard header with search and create button
fn create_dashboard_header(document: &Document) -> Result<Element, JsValue> {
    let header = document.create_element("div")?;
    header.set_class_name("dashboard-header");

    // -------------------------------------------------------------------
    // Scope selector – fancy slider toggle (My ▸ All)
    // -------------------------------------------------------------------
    use crate::state::DashboardScope;

    // <label class="scope-toggle"> <input type=checkbox …> <span class="slider"></span> </label>
    let toggle_label = document.create_element("label")?;
    toggle_label.set_class_name("scope-toggle");

    let toggle_input = document.create_element("input")?;
    toggle_input.set_attribute("type", "checkbox")?;
    toggle_input.set_attribute("id", "dashboard-scope-toggle")?;
    toggle_input.set_attribute(ATTR_DATA_TESTID, "dashboard-scope-toggle")?;

    // Checked means "All agents"; unchecked means "My agents"
    let initial_scope = APP_STATE.with(|st| st.borrow().dashboard_scope);
    if matches!(initial_scope, DashboardScope::AllAgents) {
        toggle_input.set_attribute("checked", "checked")?;
    }

    // Slider visual span
    let slider_span = document.create_element("span")?;
    slider_span.set_class_name("slider");

    // ---------------- Text label above toggle ----------------------
    let text_label = document.create_element("span")?;
    text_label.set_class_name("scope-text-label");
    text_label.set_id("scope-text");
    let initial_label = if matches!(initial_scope, DashboardScope::AllAgents) {
        "All agents"
    } else {
        "My agents"
    };
    text_label.set_inner_html(initial_label);

    // assemble
    toggle_label.append_child(&toggle_input)?;
    toggle_label.append_child(&slider_span)?;

    // wrapper to stack text above toggle (column)
    let wrapper = document.create_element("div")?;
    wrapper.set_class_name("scope-wrapper");
    wrapper.append_child(&text_label)?;
    wrapper.append_child(&toggle_label)?;

    // Change handler
    let change_callback = Closure::wrap(Box::new(move |event: web_sys::Event| {
        if let Some(target) = event.target() {
            if let Ok(input_el) = target.dyn_into::<web_sys::HtmlInputElement>() {
                let checked = input_el.checked();
                let scope = if checked {
                    DashboardScope::AllAgents
                } else {
                    DashboardScope::MyAgents
                };

                // update text label
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    if let Some(lbl) = doc.get_element_by_id("scope-text") {
                        let lbl_el = lbl.dyn_ref::<web_sys::HtmlElement>().unwrap();
                        lbl_el.set_inner_html(if checked { "All agents" } else { "My agents" });
                    }
                }

                crate::state::dispatch_global_message(
                    crate::messages::Message::ToggleDashboardScope(scope),
                );
            }
        }
    }) as Box<dyn FnMut(_)>);

    toggle_input
        .dyn_ref::<web_sys::HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("change", change_callback.as_ref().unchecked_ref())?;
    change_callback.forget();

    // -------------------------------------------------------------------
    // Finally, attach the scope switch (temporarily disabled – UX TBD)
    // -------------------------------------------------------------------
    #[allow(unused_variables)]
    {
        let _unused = &wrapper; // Keep tree for future use, but don’t render.
    }

    // -------------------------------------------------------------------
    // Button container
    let button_container = document.create_element("div")?;
    button_container.set_class_name("button-container");

    // Create Agent button using ui_components helper
    let create_button = create_button(
        document,
        ButtonConfig::new("Create Agent")
            .with_id("create-agent-button")
            .with_class("create-agent-button")
            .with_testid("create-agent-btn"),
    )?;

    // Add click event handler for Create Agent button
    let create_callback = Closure::wrap(Box::new(move || {
        debug_log!("Create new agent from dashboard");

        // Generate a random agent name
        let agent_name = format!(
            "{} {}",
            DEFAULT_AGENT_NAME,
            (js_sys::Math::random() * 100.0).round()
        );

        // Dispatch a message to request agent creation (do not access state directly)
        crate::state::dispatch_global_message(crate::messages::Message::RequestCreateAgent {
            name: agent_name,
            system_instructions: DEFAULT_SYSTEM_INSTRUCTIONS.to_string(),
            task_instructions: DEFAULT_TASK_INSTRUCTIONS.to_string(),
        });
    }) as Box<dyn FnMut()>);

    let create_btn = create_button
        .dyn_ref::<web_sys::HtmlElement>()
        .ok_or(JsValue::from_str("Could not cast to HtmlElement"))?;
    create_btn.set_onclick(Some(create_callback.as_ref().unchecked_ref()));
    create_callback.forget();

    button_container.append_child(&create_button)?;

    // Reset DB control moved to Admin Ops page (super-user panel)

    // Attach button container last so it aligns to the right via flexbox CSS.
    header.append_child(&button_container)?;

    Ok(header)
}

// Create the agents table structure
fn create_agents_table(document: &Document) -> Result<Element, JsValue> {
    let table = document.create_element("table")?;
    table.set_id("agents-table");
    table.set_class_name("agents-table");

    // Create table header
    let thead = document.create_element("thead")?;
    let header_row = document.create_element("tr")?;

    use crate::state::DashboardScope;
    let include_owner =
        APP_STATE.with(|st| st.borrow().dashboard_scope == DashboardScope::AllAgents);

    // Define column headers dynamically
    let mut columns = vec!["Name".to_string()];
    if include_owner {
        columns.push("Owner".to_string());
    }
    columns.extend(vec![
        "Status".to_string(),
        "Last Run".to_string(),
        "Next Run".to_string(),
        "Success Rate".to_string(),
        "Actions".to_string(),
    ]);

    for column in columns.iter() {
        let th = document.create_element("th")?;
        th.set_inner_html(column);

        // Add sort functionality
        let column_id = column.to_lowercase().replace(" ", "_");
        th.set_attribute("data-column", &column_id)?;

        // Add actions-header class to Actions column
        if column == "Actions" {
            th.set_class_name("actions-header");
        }

        // Add click handler for sorting
        let key_for_msg = match column.as_str() {
            "Name" => crate::state::DashboardSortKey::Name,
            "Status" => crate::state::DashboardSortKey::Status,
            "Last Run" => crate::state::DashboardSortKey::LastRun,
            "Next Run" => crate::state::DashboardSortKey::NextRun,
            "Success Rate" => crate::state::DashboardSortKey::SuccessRate,
            _ => crate::state::DashboardSortKey::Name,
        };

        let sort_callback = Closure::wrap(Box::new(move |_event: web_sys::MouseEvent| {
            crate::state::dispatch_global_message(crate::messages::Message::UpdateDashboardSort(
                key_for_msg,
            ));
        }) as Box<dyn FnMut(_)>);

        th.dyn_ref::<HtmlElement>()
            .unwrap()
            .add_event_listener_with_callback("click", sort_callback.as_ref().unchecked_ref())?;

        // We need to forget the closure to avoid it being cleaned up when leaving scope
        sort_callback.forget();

        // Add sort indicator
        APP_STATE.with(|st| {
            let st = st.borrow();
            let sort = &st.dashboard_sort;
            let is_this = sort.key == key_for_msg;
            if is_this {
                let arrow = if sort.ascending { "▲" } else { "▼" };
                let indicator_span = document.create_element("span").unwrap();
                indicator_span.set_class_name("sort-indicator");
                indicator_span.set_inner_html(arrow);
                let _ = th.append_child(&indicator_span);
            }
        });

        header_row.append_child(&th)?;
    }

    thead.append_child(&header_row)?;
    table.append_child(&thead)?;

    // Create table body
    let tbody = document.create_element("tbody")?;
    tbody.set_id("agents-table-body");
    table.append_child(&tbody)?;

    Ok(table)
}

fn get_agents_from_app_state() -> Vec<Agent> {
    APP_STATE.with(|state| {
        let state = state.borrow();

        let mut list: Vec<_> = state
            .agents
            .values()
            .filter_map(|api_agent| {
                // Only include agents that have a valid u32 ID
                api_agent.id.map(|id| {
                    // Map ApiAgent.status (Option<String>) to AgentStatus enum
                    let status = match api_agent.status.as_deref() {
                        Some("running") => AgentStatus::Running,
                        Some("processing") => AgentStatus::Processing,
                        Some("error") => AgentStatus::Error,
                        Some("idle") | _ => AgentStatus::Idle, // Default to Idle
                    };

                    // Determine scheduling metadata strings (truncate seconds for compact display)
                    let last_run_fmt = api_agent
                        .last_run_at
                        .as_ref()
                        .map(|s| format_datetime_short(s));
                    let next_run_fmt = api_agent
                        .next_run_at
                        .as_ref()
                        .map(|s| format_datetime_short(s));

                    Agent {
                        id, // Use the u32 ID directly
                        name: api_agent.name.clone(),
                        status,
                        last_run: last_run_fmt,
                        next_run: next_run_fmt,
                        success_rate: 0.0, // Placeholder – needs backend metric
                        run_count: 0,      // Placeholder – needs backend metric
                        last_run_success: None, // Placeholder
                        last_error: api_agent.last_error.clone(),

                        owner_id: api_agent.owner_id,
                        owner_label: api_agent.owner.as_ref().map(|o| {
                            o.display_name
                                .clone()
                                .filter(|s| !s.is_empty())
                                .unwrap_or_else(|| o.email.clone())
                        }),
                        owner_avatar_url: api_agent
                            .owner
                            .as_ref()
                            .and_then(|o| o.avatar_url.clone()),
                    }
                })
            })
            .collect();

        // Sorting ------------------------------------------------------
        let sort_cfg = state.dashboard_sort;
        list.sort_by(|a, b| {
            use crate::state::DashboardSortKey::*;
            let ord = match sort_cfg.key {
                Name => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
                Status => {
                    let ord_a = match a.status {
                        AgentStatus::Running => 0,
                        AgentStatus::Processing => 1,
                        AgentStatus::Idle => 2,
                        AgentStatus::Error => 3,
                    };
                    let ord_b = match b.status {
                        AgentStatus::Running => 0,
                        AgentStatus::Processing => 1,
                        AgentStatus::Idle => 2,
                        AgentStatus::Error => 3,
                    };
                    ord_a.cmp(&ord_b)
                }
                LastRun => a.last_run.cmp(&b.last_run),
                NextRun => a.next_run.cmp(&b.next_run),
                SuccessRate => {
                    use std::cmp::Ordering;
                    a.success_rate
                        .partial_cmp(&b.success_rate)
                        .unwrap_or(Ordering::Equal)
                }
            };
            if sort_cfg.ascending {
                ord
            } else {
                ord.reverse()
            }
        });

        list
    })
}

// Populate the table with agent data
fn populate_agents_table(document: &Document) -> Result<(), JsValue> {
    let tbody = document
        .get_element_by_id("agents-table-body")
        .ok_or_else(|| JsValue::from_str("Could not find agents-table-body"))?;

    // Clear existing rows
    tbody.set_inner_html("");

    // Get agents from the app state
    let agents = get_agents_from_app_state();

    if agents.is_empty() {
        // Display empty or no-results message depending on search
        let empty_row = document.create_element("tr")?;
        let empty_cell = document.create_element("td")?;
        let include_owner = APP_STATE
            .with(|st| st.borrow().dashboard_scope == crate::state::DashboardScope::AllAgents);
        let colspan = if include_owner { 7 } else { 6 };
        empty_cell.set_attribute("colspan", &colspan.to_string())?;
        let no_results_msg = "No agents found. Click 'Create Agent' to get started.".to_string();
        // Build empty-state container with illustration and CTA button.
        let wrapper = document.create_element("div")?;
        wrapper.set_class_name("empty-state");

        // Simple robot illustration (emoji fallback for now)
        let illustration = document.create_element("div")?;
        illustration.set_class_name("empty-state-illustration");
        illustration.set_inner_html("🤖");
        wrapper.append_child(&illustration)?;

        // Message text
        let msg_el = document.create_element("p")?;
        msg_el.set_class_name("empty-state-text");
        msg_el.set_inner_html(&no_results_msg);
        wrapper.append_child(&msg_el)?;

        // No secondary create-agent CTA – header already provides this action.

        empty_cell.append_child(&wrapper)?;

        empty_row.append_child(&empty_cell)?;
        tbody.append_child(&empty_row)?;
    } else {
        // Create a row for each agent
        for agent in agents {
            // Create main row
            let row = create_agent_row(document, &agent)?;
            tbody.append_child(&row)?;

            // If this agent is currently expanded, render an additional row
            let expanded = APP_STATE.with(|s| s.borrow().expanded_agent_rows.contains(&agent.id));

            if expanded {
                let detail_row = create_agent_detail_row(document, &agent)?;
                tbody.append_child(&detail_row)?;
            }
        }
    }

    Ok(())
}

// Create a table row for an agent
fn create_agent_row(document: &Document, agent: &Agent) -> Result<Element, JsValue> {
    let row = document.create_element("tr")?;
    row.set_attribute("data-agent-id", &agent.id.to_string())?;

    // Check if this agent row is currently expanded for aria-expanded attribute
    let is_expanded = APP_STATE.with(|s| s.borrow().expanded_agent_rows.contains(&agent.id));
    row.set_attribute("aria-expanded", if is_expanded { "true" } else { "false" })?;

    // Highlight the entire row if the agent is currently in an error state so
    // users can spot it quickly.
    if agent.status == AgentStatus::Error {
        // Add a CSS class (requires style definition in stylesheet)
        row.set_class_name("error-row");
    }

    // Name cell
    let name_cell = document.create_element("td")?;
    name_cell.set_attribute("data-label", "Name")?;
    name_cell.set_inner_html(&agent.name);
    row.append_child(&name_cell)?;

    // Make row focusable for keyboard nav
    row.set_attribute("tabindex", "0")?;

    // Owner cell (only in AllAgents scope)
    use crate::state::DashboardScope;
    let include_owner =
        APP_STATE.with(|st| st.borrow().dashboard_scope == DashboardScope::AllAgents);

    if include_owner {
        let owner_cell = document.create_element("td")?;
        owner_cell.set_attribute("data-label", "Owner")?;
        owner_cell.set_class_name("owner-cell");

        if let Some(label) = &agent.owner_label {
            // Try to render avatar badge if we have at least email/id.
            if let Some(owner_id) = agent.owner_id {
                use crate::models::CurrentUser;
                let owner_profile = CurrentUser {
                    id: owner_id,
                    email: label.clone(),
                    display_name: Some(label.clone()),
                    avatar_url: agent.owner_avatar_url.clone(),
                    prefs: None,
                    gmail_connected: false,
                };

                // Create a wrapper div for inline display
                let owner_wrapper = document.create_element("div")?;
                owner_wrapper.set_class_name("owner-wrapper");

                if let Ok(avatar_el) =
                    crate::components::avatar_badge::render(document, &owner_profile)
                {
                    // Add the small class to make avatar smaller
                    avatar_el.set_class_name("avatar-badge small");
                    // Set title attribute to show full name on hover
                    avatar_el.set_attribute("title", &label)?;
                    owner_wrapper.append_child(&avatar_el)?;
                }

                owner_cell.append_child(&owner_wrapper)?;
            } else {
                // If no owner_id, just show the text
                owner_cell.set_inner_html(&label);
            }
        } else {
            owner_cell.set_inner_html("-");
        }

        row.append_child(&owner_cell)?;
    }

    // Status cell
    let status_cell = document.create_element("td")?;
    status_cell.set_attribute("data-label", "Status")?;
    let status_indicator = document.create_element("span")?;
    status_indicator
        .set_class_name(&format!("status-indicator status-{:?}", agent.status).to_lowercase());

    let status_text = match agent.status {
        AgentStatus::Running => "● Running",
        AgentStatus::Processing => "⏳ Processing",
        AgentStatus::Idle => "○ Idle",
        AgentStatus::Error => "⚠ Error",
    };

    status_indicator.set_inner_html(status_text);
    status_cell.append_child(&status_indicator)?;

    // If we have an error message, append a small info icon with a tooltip so
    // that hovering shows the error string. This avoids adding a whole new
    // column while still keeping the information discoverable.
    if let Some(err_msg) = &agent.last_error {
        let info_icon = document.create_element("span")?;
        info_icon.set_class_name("info-icon");
        info_icon.set_inner_html("ℹ");
        // Use the "title" attribute for a simple browser tooltip.
        info_icon.set_attribute("title", err_msg)?;
        status_cell.append_child(&info_icon)?;
    }

    // Add last run success/failure indicator if available
    if let Some(success) = agent.last_run_success {
        let last_run_indicator = document.create_element("span")?;
        last_run_indicator.set_class_name(if success {
            "last-run-indicator last-run-success"
        } else {
            "last-run-indicator last-run-failure"
        });

        last_run_indicator.set_inner_html(if success {
            " (Last: ✓)"
        } else {
            " (Last: ✗)"
        });

        status_cell.append_child(&last_run_indicator)?;
    }

    row.append_child(&status_cell)?;

    // (Toggle column removed – implementation postponed)

    // Last Run cell
    let last_run_cell = document.create_element("td")?;
    last_run_cell.set_attribute("data-label", "Last Run")?;
    last_run_cell.set_inner_html(&agent.last_run.as_deref().unwrap_or("-"));
    row.append_child(&last_run_cell)?;

    // Next Run cell
    let next_run_cell = document.create_element("td")?;
    next_run_cell.set_attribute("data-label", "Next Run")?;
    next_run_cell.set_inner_html(&agent.next_run.as_deref().unwrap_or("-"));
    row.append_child(&next_run_cell)?;

    // Success Rate cell
    let success_cell = document.create_element("td")?;
    success_cell.set_attribute("data-label", "Success Rate")?;
    success_cell.set_inner_html(&format!("{:.1}% ({})", agent.success_rate, agent.run_count));
    row.append_child(&success_cell)?;

    // Actions cell
    let actions_cell = document.create_element("td")?;
    actions_cell.set_attribute("data-label", "Actions")?;
    actions_cell.set_class_name("actions-cell");

    // Create inner container for buttons
    let actions_inner = document.create_element("div")?;
    actions_inner.set_class_name("actions-cell-inner");

    // Run button
    let run_btn = document.create_element("button")?;
    run_btn.set_attribute("type", "button")?;

    // Check if agent is currently running
    let is_running = matches!(agent.status, AgentStatus::Running);

    if is_running {
        run_btn.set_class_name("action-btn run-btn disabled");
        run_btn.set_attribute("disabled", "true")?;
        run_btn.set_attribute("title", "Agent is already running")?;
        run_btn.set_attribute("aria-label", "Agent is already running")?;
    } else {
        run_btn.set_class_name("action-btn run-btn");
        run_btn.set_attribute("title", "Run Agent")?;
        run_btn.set_attribute("aria-label", "Run Agent")?;
    }

    run_btn.set_inner_html("<i data-feather=\"play\"></i>");
    run_btn.set_attribute(ATTR_DATA_TESTID, &format!("run-agent-{}", agent.id))?;

    // Run button click handler
    let agent_id = agent.id;
    let run_btn_html: web_sys::HtmlElement = run_btn.clone().dyn_into().unwrap();
    let run_btn_rc = std::rc::Rc::new(run_btn_html);

    let run_callback = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
        event.stop_propagation();

        // Check if button is disabled
        if let Some(target) = event.target() {
            if let Ok(element) = target.dyn_into::<web_sys::HtmlElement>() {
                if element.get_attribute("disabled").is_some() {
                    crate::toast::info("Agent is already running. Please wait for it to finish.");
                    return;
                }
            }
        }

        debug_log!("Run agent: {}", agent_id);

        // Immediate user feedback
        crate::toast::info("Run started");
        // Optimistically mark the agent as running so the UI updates instantly.
        crate::state::APP_STATE.with(|state_ref| {
            let mut state = state_ref.borrow_mut();
            if let Some(agent) = state.agents.get_mut(&agent_id) {
                agent.status = Some("running".to_string());
            }
        });

        // Refresh dashboard immediately to reflect optimistic change.
        if let Some(window) = web_sys::window() {
            if let Some(document) = window.document() {
                let _ = crate::components::dashboard::refresh_dashboard(&document);
            }
        }

        use crate::ui_components::set_button_loading;
        let btn_clone = run_btn_rc.clone();
        set_button_loading(&btn_clone, true);

        wasm_bindgen_futures::spawn_local(async move {
            match crate::network::api_client::ApiClient::run_agent(agent_id).await {
                Ok(response) => {
                    debug_log!("Agent {} run triggered: {}", agent_id, response);

                    // After the task completes we refresh the specific agent to
                    // pick up the final status and timestamps.
                    crate::network::api_client::reload_agent(agent_id);
                    set_button_loading(&btn_clone, false);
                }
                Err(e) => {
                    web_sys::console::error_1(
                        &format!("Run error for agent {}: {:?}", agent_id, e).into(),
                    );

                    // If it's a 409 conflict, revert optimistic status change
                    let error_str = format!("{:?}", e);
                    if error_str.contains("already running") {
                        // Reload the agent to get the actual status
                        crate::network::api_client::reload_agent(agent_id);
                    } else {
                        crate::toast::error(&format!("Failed to run agent: {:?}", e));
                    }

                    set_button_loading(&btn_clone, false);
                }
            }
        });
    }) as Box<dyn FnMut(_)>);

    run_btn
        .dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("click", run_callback.as_ref().unchecked_ref())?;
    run_callback.forget();

    actions_inner.append_child(&run_btn)?;

    // Edit button
    let edit_btn = document.create_element("button")?;
    edit_btn.set_attribute("type", "button")?;
    edit_btn.set_class_name("action-btn edit-btn");
    edit_btn.set_inner_html("<i data-feather=\"edit-3\"></i>");
    edit_btn.set_attribute("title", "Edit Agent")?;
    edit_btn.set_attribute("aria-label", "Edit Agent")?;
    edit_btn.set_attribute(ATTR_DATA_TESTID, &format!("edit-agent-{}", agent.id))?;

    // Edit button click handler
    let agent_id = agent.id;
    let edit_callback = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
        event.stop_propagation();
        debug_log!("Edit agent: {}", agent_id);

        // Dispatch EditAgent message with the u32 ID
        // NOTE: Message::EditAgent needs to be updated to accept u32
        crate::state::dispatch_global_message(crate::messages::Message::EditAgent(agent_id));
    }) as Box<dyn FnMut(_)>);

    edit_btn
        .dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("click", edit_callback.as_ref().unchecked_ref())?;
    edit_callback.forget();

    actions_inner.append_child(&edit_btn)?;

    // Chat button
    let chat_btn = document.create_element("button")?;
    chat_btn.set_attribute("type", "button")?;
    chat_btn.set_class_name("action-btn chat-btn");
    chat_btn.set_inner_html("<i data-feather=\"message-circle\"></i>");
    chat_btn.set_attribute("title", "Chat with Agent")?;
    chat_btn.set_attribute("aria-label", "Chat with Agent")?;
    chat_btn.set_attribute(ATTR_DATA_TESTID, &format!("chat-agent-{}", agent.id))?;

    // Chat button click handler
    let agent_id = agent.id;
    let chat_callback = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
        event.stop_propagation();
        debug_log!("Chat with agent: {}", agent_id);

        // Dispatch NavigateToAgentChat message with the u32 ID
        crate::state::dispatch_global_message(crate::messages::Message::NavigateToAgentChat(
            agent_id,
        ));

        // (legacy id parsing logic removed – agent_id is already a u32)
    }) as Box<dyn FnMut(_)>);

    chat_btn
        .dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("click", chat_callback.as_ref().unchecked_ref())?;
    chat_callback.forget();

    // Debug (info) button – new 🐞 icon
    let debug_btn = document.create_element("button")?;
    debug_btn.set_attribute("type", "button")?;
    debug_btn.set_class_name("action-btn debug-btn");
    debug_btn.set_inner_html("<i data-feather=\"settings\"></i>");
    debug_btn.set_attribute("title", "Debug / Info")?;
    debug_btn.set_attribute("aria-label", "Debug / Info")?;
    debug_btn.set_attribute(ATTR_DATA_TESTID, &format!("debug-agent-{}", agent.id))?;

    let agent_id = agent.id;
    let debug_cb = Closure::wrap(Box::new(move |_event: web_sys::MouseEvent| {
        crate::state::dispatch_global_message(crate::messages::Message::ShowAgentDebugModal {
            agent_id,
        });
    }) as Box<dyn FnMut(_)>);
    debug_btn
        .dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("click", debug_cb.as_ref().unchecked_ref())?;
    debug_cb.forget();

    actions_inner.append_child(&debug_btn)?;
    actions_inner.append_child(&chat_btn)?;

    // Delete agent button
    let delete_btn = document.create_element("button")?;
    delete_btn.set_attribute("type", "button")?;
    delete_btn.set_class_name("action-btn delete-btn");
    delete_btn.set_inner_html("<i data-feather=\"trash-2\"></i>");
    delete_btn.set_attribute("title", "Delete Agent")?;
    delete_btn.set_attribute("aria-label", "Delete Agent")?;
    delete_btn.set_attribute(ATTR_DATA_TESTID, &format!("delete-agent-{}", agent.id))?;

    // Delete button click handler
    let agent_id_del = agent.id;
    let delete_callback = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
        event.stop_propagation();

        let confirmed = web_sys::window()
            .and_then(|w| {
                w.confirm_with_message(&format!("Delete agent {}?", agent_id_del))
                    .ok()
            })
            .unwrap_or(false);
        if !confirmed {
            return;
        }

        use crate::ui_components::set_button_loading;

        if let Some(btn) = event
            .current_target()
            .and_then(|t| t.dyn_into::<web_sys::HtmlElement>().ok())
        {
            set_button_loading(&btn, true);

            wasm_bindgen_futures::spawn_local(async move {
                match crate::network::api_client::ApiClient::delete_agent(agent_id_del).await {
                    Ok(_) => {
                        crate::toast::success("Agent deleted");
                        crate::state::dispatch_global_message(
                            crate::messages::Message::RefreshAgentsFromAPI,
                        );
                    }
                    Err(e) => {
                        crate::toast::error(&format!("Delete failed: {:?}", e));
                    }
                }
                set_button_loading(&btn, false);
            });
        }
    }) as Box<dyn FnMut(_)>);

    delete_btn
        .dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("click", delete_callback.as_ref().unchecked_ref())?;
    delete_callback.forget();

    actions_inner.append_child(&delete_btn)?;

    // Append the inner container to the cell
    actions_cell.append_child(&actions_inner)?;
    row.append_child(&actions_cell)?;

    // Keyboard navigation: Up/Down moves between sibling rows, Enter toggles expand
    let row_toggle_id = agent.id;
    let key_cb = Closure::wrap(Box::new(move |event: web_sys::KeyboardEvent| {
        let key = event.key();
        match key.as_str() {
            "ArrowDown" | "ArrowUp" => {
                event.prevent_default();
                if let Some(current) = event
                    .target()
                    .and_then(|t| t.dyn_into::<web_sys::HtmlElement>().ok())
                {
                    if let Some(parent) = current.parent_element() {
                        let rows = parent.children();
                        let len = rows.length();
                        let mut idx = 0;
                        for i in 0..len {
                            if let Some(r) = rows.item(i) {
                                if r.is_same_node(Some(&current)) {
                                    idx = i;
                                    break;
                                }
                            }
                        }
                        let new_idx = if key == "ArrowDown" {
                            std::cmp::min(len - 1, idx + 1)
                        } else if idx == 0 {
                            0
                        } else {
                            idx - 1
                        };
                        if let Some(next_row) = rows.item(new_idx) {
                            let _ = next_row.dyn_ref::<web_sys::HtmlElement>().unwrap().focus();
                        }
                    }
                }
            }
            "Enter" => {
                // simulate row click to toggle expand
                event.prevent_default();
                crate::state::APP_STATE.with(|state_ref| {
                    let mut s = state_ref.borrow_mut();
                    if s.expanded_agent_rows.contains(&row_toggle_id) {
                        s.expanded_agent_rows.remove(&row_toggle_id);
                    } else {
                        s.expanded_agent_rows.clear();
                        s.expanded_agent_rows.insert(row_toggle_id);
                    }
                });
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::dashboard::refresh_dashboard(&doc);
                }
            }
            _ => {}
        }
    }) as Box<dyn FnMut(_)>);

    row.dyn_ref::<HtmlElement>()
        .unwrap()
        .add_event_listener_with_callback("keydown", key_cb.as_ref().unchecked_ref())?;
    key_cb.forget();

    // ---------------- Toggle detail expansion on row click ----------------
    let toggle_id = agent.id;
    let toggle_cb = Closure::wrap(Box::new(move |_e: web_sys::MouseEvent| {
        crate::state::APP_STATE.with(|state_ref| {
            let mut s = state_ref.borrow_mut();
            if s.expanded_agent_rows.contains(&toggle_id) {
                // Collapse current row
                s.expanded_agent_rows.remove(&toggle_id);
            } else {
                // Collapse any other expanded rows to enforce single-open behaviour
                s.expanded_agent_rows.clear();
                s.expanded_agent_rows.insert(toggle_id);
            }
        });

        if let Some(win) = web_sys::window() {
            if let Some(doc) = win.document() {
                let _ = crate::components::dashboard::refresh_dashboard(&doc);
            }
        }
    }) as Box<dyn FnMut(_)>);

    row.add_event_listener_with_callback("click", toggle_cb.as_ref().unchecked_ref())?;
    toggle_cb.forget();

    Ok(row)
}

// -------------------------------------------------------------------------
// Detail row with error info + action buttons
// -------------------------------------------------------------------------
fn create_agent_detail_row(document: &Document, agent: &Agent) -> Result<Element, JsValue> {
    let tr = document.create_element("tr")?;
    tr.set_class_name("agent-detail-row");

    let td = document.create_element("td")?;
    // Adjust colspan based on whether Owner column is shown
    let include_owner =
        APP_STATE.with(|st| st.borrow().dashboard_scope == crate::state::DashboardScope::AllAgents);
    let colspan = if include_owner { 7 } else { 6 };
    td.set_attribute("colspan", &colspan.to_string())?;

    let container = document.create_element("div")?;
    container.set_class_name("agent-detail-container");

    // (legacy error display removed)

    // -----------------------------------------------------------------
    // Run History table (Phase 1 stub – minimal yet functional)
    // -----------------------------------------------------------------
    // Attempt to fetch runs from global state. If not present trigger load.
    let runs_opt = APP_STATE.with(|state| {
        let state = state.borrow();
        state.agent_runs.get(&agent.id).cloned()
    });

    if let Some(runs) = runs_opt {
        // Determine whether we are in compact (<=5) or expanded mode
        let expanded = APP_STATE.with(|s| s.borrow().run_history_expanded.contains(&agent.id));

        let rows_to_show = if expanded {
            runs.len()
        } else {
            runs.len().min(5)
        };

        // Build a minimal table with id + status + started_at
        let table = document.create_element("table")?;
        table.set_class_name("run-history-table");

        // Header row – extended columns
        let thead = document.create_element("thead")?;
        let header_row = document.create_element("tr")?;
        for h in [
            "Status", "Started", "Duration", "Trigger", "Tokens", "Cost", "",
        ] {
            let th = document.create_element("th")?;
            th.set_inner_html(h);
            header_row.append_child(&th)?;
        }
        thead.append_child(&header_row)?;
        table.append_child(&thead)?;

        let tbody = document.create_element("tbody")?;
        for run in runs.iter().take(rows_to_show) {
            let row = document.create_element("tr")?;

            // Status icon
            let status_td = document.create_element("td")?;
            let icon = match run.status.as_str() {
                "running" => "▶",
                "success" => "✔",
                "failed" => "✖",
                _ => "●",
            };
            status_td.set_inner_html(icon);
            row.append_child(&status_td)?;

            // Started at (short)
            let started_td = document.create_element("td")?;
            started_td.set_inner_html(
                run.started_at
                    .as_ref()
                    .map(|s| format_datetime_short(s))
                    .unwrap_or_else(|| "-".to_string())
                    .as_str(),
            );
            row.append_child(&started_td)?;

            // Duration (pretty)
            let dur_td = document.create_element("td")?;
            let dur_str = run
                .duration_ms
                .map(|d| crate::utils::format_duration_ms(d))
                .unwrap_or_else(|| "-".to_string());
            dur_td.set_inner_html(&dur_str);
            row.append_child(&dur_td)?;

            // Trigger
            let trig_td = document.create_element("td")?;
            let trig_str = crate::utils::capitalise_first(run.trigger.as_str());
            trig_td.set_inner_html(&trig_str);
            row.append_child(&trig_td)?;

            // Tokens
            let tokens_td = document.create_element("td")?;
            let tok_str = run
                .total_tokens
                .map(|t| t.to_string())
                .unwrap_or_else(|| "—".to_string());
            tokens_td.set_inner_html(&tok_str);
            row.append_child(&tokens_td)?;

            // Cost
            let cost_td = document.create_element("td")?;
            let cost_str = run
                .total_cost_usd
                .map(|c| crate::utils::format_cost_usd(c))
                .unwrap_or_else(|| "—".to_string());
            cost_td.set_inner_html(&cost_str);
            row.append_child(&cost_td)?;

            // Kebab-menu placeholder (⋮)
            let action_td = document.create_element("td")?;
            action_td.set_class_name("run-kebab-cell");
            let kebab = document.create_element("span")?;
            kebab.set_class_name("kebab-menu-btn");
            kebab.set_inner_html("⋮");

            // Placeholder click handler – will be replaced in Phase-2 PR-3
            let run_id_for_cb = run.id;
            let cb = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
                event.stop_propagation();
                crate::toast::info(&format!(
                    "Actions menu for run #{} – not yet implemented",
                    run_id_for_cb
                ));
            }) as Box<dyn FnMut(_)>);
            kebab.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();

            action_td.append_child(&kebab)?;
            row.append_child(&action_td)?;

            tbody.append_child(&row)?;
        }
        table.append_child(&tbody)?;
        container.append_child(&table)?;

        // Show toggle link if more than 5 runs exist
        if runs.len() > 5 {
            let toggle_link = document.create_element("a")?;
            toggle_link.set_class_name("run-toggle-link");
            toggle_link.set_attribute("href", "#")?;
            toggle_link.set_attribute("aria-expanded", if expanded { "true" } else { "false" })?;
            let link_text = if expanded {
                "Show less".to_string()
            } else {
                format!("Show all ({})", runs.len())
            };
            toggle_link.set_inner_html(&link_text);

            let aid = agent.id;
            let toggle_cb = Closure::wrap(Box::new(move |event: web_sys::MouseEvent| {
                event.prevent_default();
                crate::state::dispatch_global_message(crate::messages::Message::ToggleRunHistory {
                    agent_id: aid,
                });
            }) as Box<dyn FnMut(_)>);

            toggle_link
                .add_event_listener_with_callback("click", toggle_cb.as_ref().unchecked_ref())?;
            toggle_cb.forget();

            container.append_child(&toggle_link)?;
        }
    } else {
        // Not yet loaded – show placeholder and dispatch load message
        let span = document.create_element("span")?;
        span.set_inner_html("Loading run history...");
        container.append_child(&span)?;

        // Dispatch load
        crate::state::dispatch_global_message(crate::messages::Message::LoadAgentRuns(agent.id));
    }

    // (legacy retry / dismiss buttons removed)
    td.append_child(&container)?;
    tr.append_child(&td)?;

    Ok(tr)
}
