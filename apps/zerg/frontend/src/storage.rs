use crate::models::{ApiAgent, WorkflowNode};
use crate::network::ui_updates::update_layout_status;
use crate::network::ApiClient;
use crate::state::AppState;
use gloo_timers::future::TimeoutFuture;
use serde_json::{from_str, to_string};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::spawn_local;
use crate::debug_log;
// Additional helpers and legacy extension traits – these will gradually be
// trimmed once the new storage pipeline stabilises.

// API URL configuration - removed hardcoded constant, use dynamic API client instead

// ---------------------------------------------------------------------------
// Debounced *layout save* helper – we track a *sequence number* that is
// incremented every time `save_state_to_api` is called.  A delayed task
// records the sequence number it saw and, after the timeout, only proceeds
// with the network request if no newer call has happened in the meantime.
// ---------------------------------------------------------------------------

static SAVE_SEQ: AtomicU64 = AtomicU64::new(0);

// Structure to store viewport data
#[derive(serde::Serialize, serde::Deserialize)]
struct ViewportData {
    x: f64,
    y: f64,
    zoom: f64,
}

// Store the active view (Dashboard, Canvas, or Chat)
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub enum ActiveView {
    Dashboard,
    Canvas,
    ChatView,
    /// User profile & preferences page
    Profile,
    /// Admin-only Ops dashboard
    AdminOps,
}

/// Save current app state to API
pub fn save_state(app_state: &AppState) -> Result<(), JsValue> {
    // Save changes to API
    save_state_to_api(app_state);

    // NOTE: Persisting node positions & viewport to localStorage has been
    // removed now that the `/api/graph/layout` endpoint is fully live.  Any
    // save error bubbles up via `update_layout_status()` so developers can
    // diagnose backend or network issues immediately instead of silently
    // falling back to stale client-side data.

    // TEMPORARILY DISABLED: localStorage persistence to avoid frontend/backend sync conflicts
    // The WebSocket-based backend persistence is much more reliable and eliminates race conditions
    /*
    let window = web_sys::window().expect("no global window exists");
    let local_storage = window.local_storage()?.expect("no local storage exists");

    // Save active view
    let active_view_str = to_string(&app_state.active_view).map_err(|e| JsValue::from_str(&e.to_string()))?;
    local_storage.set_item("active_view", &active_view_str)?;

    // Save workflows and WorkflowNodes
    save_workflows(app_state)?;
    */

    Ok(())
}

/// Load app state from API
pub fn load_state(app_state: &mut AppState) -> Result<bool, JsValue> {
    // Load data from API
    load_state_from_api(app_state);

    // Phase-B: attempt to hydrate canvas layout from backend *before* reading
    // the localStorage fallback.  This keeps the behaviour identical when the
    // remote call fails or returns 204 (no content).
    try_load_layout_from_api();

    // Also load workflows
    load_workflows(app_state)?;

    // -------------------------------------------------------------------
    // NOTE – LocalStorage fallback removed
    // -------------------------------------------------------------------
    // Historically the canvas restored node positions and viewport data from
    // `localStorage` if the remote `/api/graph/layout` endpoint returned an
    // error or had not been implemented yet.  This behaviour masked genuine
    // persistence issues – the UI appeared to work even though the changes
    // were *not* saved to the server.  To make such problems visible we now
    // rely exclusively on the backend-provided layout.  If that request fails
    // or returns 204 the canvas will start with an empty scene and default
    // viewport, prompting developers to investigate the underlying network or
    // backend problem instead of unknowingly working with stale local data.

    // Return true to indicate we started the loading process
    // Actual loading happens asynchronously
    Ok(true)
}

// ---------------------------------------------------------------------------
// Canvas layout – remote load helper (Phase-B)
// ---------------------------------------------------------------------------

fn try_load_layout_from_api() {
    // Spawn future that attempts to GET /api/graph/layout.  We deliberately
    // *do not* block the caller because the initial render should not wait
    // on network I/O.

    use crate::constants::{DEFAULT_NODE_HEIGHT, DEFAULT_NODE_WIDTH, NODE_COLOR_GENERIC};
    use crate::models::WorkflowNode;
    use serde::Deserialize;

    spawn_local(async {
        // Determine current workflow id once – avoid borrowing after await.
        let wf_id = crate::state::APP_STATE.with(|s| s.borrow().current_workflow_id);

        match crate::network::ApiClient::get_layout(wf_id.map(|v| v as u32)).await {
            Ok(body) if !body.is_empty() => {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&body) {
                    // ------------------------------------------------------------------
                    // 1. Apply viewport first (if present)
                    // ------------------------------------------------------------------
                    if let Some(vp_val) = val.get("viewport") {
                        #[derive(Deserialize)]
                        struct Vp {
                            x: f64,
                            y: f64,
                            zoom: f64,
                        }

                        if let Ok(vp) = serde_json::from_value::<Vp>(vp_val.clone()) {
                            crate::state::dispatch_global_message(crate::messages::Message::UpdateViewport {
                                x: vp.x,
                                y: vp.y,
                                zoom: vp.zoom,
                            });
                        }
                    }

                    // ------------------------------------------------------------------
                    // 2. Merge/insert node positions
                    // ------------------------------------------------------------------
                    if let Some(nodes_val) = val.get("nodes") {
                        #[derive(Deserialize)]
                        struct Pos {
                            x: f64,
                            y: f64,
                        }

                        if let Ok(pos_map) = serde_json::from_value::<
                            std::collections::HashMap<String, Pos>,
                        >(nodes_val.clone())
                        {
                            crate::state::APP_STATE.with(|s| {
                                let mut st = s.borrow_mut();

                                for (id, pos) in pos_map {
                                    match st.workflow_nodes.get_mut(&id) {
                                        Some(node) => {
                                            node.set_x(pos.x);
                                            node.set_y(pos.y);
                                        }
                                        None => {
                                            // Insert a *stub* node so at least the layout is respected.
                                            let mut node = WorkflowNode::new_with_type(
                                                id.clone(),
                                                &crate::models::NodeType::GenericNode,
                                            );
                                            node.apply_visual(
                                                pos.x,
                                                pos.y,
                                                DEFAULT_NODE_WIDTH,
                                                DEFAULT_NODE_HEIGHT,
                                                NODE_COLOR_GENERIC,
                                                &id,
                                            );
                                            st.workflow_nodes.insert(id.clone(), node);
                                        }
                                    }
                                }
                            });
                        }
                    }

                    // Trigger UI update
                    if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
                        web_sys::console::error_1(
                            &format!("Failed to refresh UI after layout load: {:?}", e).into(),
                        );
                    }

                    // Log number of nodes & viewport after remote hydration
                    crate::state::APP_STATE.with(|s| {
                        let st = s.borrow();
                        debug_log!(
                            "[layout] remote load success – nodes={} viewport=({}, {}, {})",
                            st.workflow_nodes.len(),
                            st.viewport_x,
                            st.viewport_y,
                            st.zoom_level
                        );
                    });

                    // ------------------------------------------------------------------
                    // 3. Reconcile any *stub* nodes that were inserted because the
                    //    corresponding agent data was not yet available when the
                    //    layout payload was processed.  Now that the async
                    //    agents fetch may have completed we can upgrade their
                    //    labels & node_type.
                    // ------------------------------------------------------------------
                    fix_stub_nodes();
                }
            }
            Err(e) => {
                // Visible alert so missing persistence is *immediately* obvious
                update_layout_status("Layout: load failed – using empty scene", "red");

                // Log full error to console for debugging.
                web_sys::console::error_1(&format!("Remote layout fetch failed: {:?}", e).into());

                // No localStorage fallback – surface error to developer instead
            }
            // 204 No Content – no layout for this workflow yet.  **Do not**
            // load the stale localStorage copy (if any) because the user
            // explicitly reset the database or created a new workflow.
            _ => {
                // Got 204 – backend responded with "no content".  Inform the
                // user so they realise a first save is still required.
                update_layout_status("Layout: no saved layout yet", "yellow");

                debug_log!("No remote canvas layout found (204)");
            }
        }
    });
}

// ---------------------------------------------------------------------------
// LocalStorage helpers
// ---------------------------------------------------------------------------

/// Attempt to hydrate nodes + viewport from localStorage.  Does **not**
/// overwrite existing nodes if any are already present.
pub fn try_load_from_localstorage() {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            // Bail if we already have nodes loaded
            let has_nodes = crate::state::APP_STATE.with(|s| s.borrow().workflow_nodes.len() > 0);
            if has_nodes {
                return;
            }

            if let Ok(Some(nodes_str)) = storage.get_item("nodes") {
                if let Ok(nodes_map) = serde_json::from_str::<
                    std::collections::HashMap<String, crate::models::WorkflowNode>,
                >(&nodes_str)
                {
                    crate::state::dispatch_global_message(crate::messages::Message::UpdateWorkflowNodes(nodes_map));
                }
            }

            if let Ok(Some(vp_str)) = storage.get_item("viewport") {
                #[derive(serde::Deserialize)]
                struct Vp {
                    x: f64,
                    y: f64,
                    zoom: f64,
                }
                if let Ok(vp) = serde_json::from_str::<Vp>(&vp_str) {
                    crate::state::dispatch_global_message(crate::messages::Message::UpdateViewport {
                        x: vp.x,
                        y: vp.y,
                        zoom: vp.zoom,
                    });
                }
            }

            debug_log!("Layout loaded from localStorage fallback");
            update_layout_status("Layout: local offline copy", "orange");
        }
    }
}

/// Clear all stored data
pub fn clear_storage() -> Result<(), JsValue> {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            let _ = storage.remove_item("nodes");
            let _ = storage.remove_item("viewport");
        }
    }
    Ok(())
}

/// Save app state to API (transitional approach)
pub fn save_state_to_api(app_state: &AppState) {
    // If a drag is still in progress we don’t schedule a save – StopDragging
    // will call us again.  This avoids enqueuing dozens of timers while the
    // user moves the mouse.
    // Skip persistence if the user is actively dragging **either** any
    // node *or* the entire canvas viewport.  A fresh call will be issued by
    // the `StopDragging` / `StopCanvasDrag` handlers once the pointer is
    // released so no data is lost.
    if app_state.dragging.is_some() || app_state.canvas_dragging {
        return;
    }

    // The canvas layout is now persisted **exclusively** to the backend.
    // We intentionally removed the legacy localStorage fallback so that any
    // save failures become immediately visible during development instead of
    // being masked by stale client-side data.

    // ------------------------------------------------------------------
    // Save canvas structure data to workflow - this is critical for LangGraph
    // ------------------------------------------------------------------
    save_canvas_data_to_api(app_state);

    // ------------------------------------------------------------------
    // Debounce network save – PATCH /api/graph/layout
    // ------------------------------------------------------------------

    use serde_json::json;

    let my_seq = SAVE_SEQ.fetch_add(1, Ordering::SeqCst) + 1;
    update_layout_status("Layout: saving…", "yellow");

    spawn_local(async move {
        TimeoutFuture::new(300).await;

        if SAVE_SEQ.load(Ordering::SeqCst) != my_seq {
            // A newer save displaced us.
            return;
        }

        // Serialise *latest* state after debounce period.
        use serde_json::json;
        crate::state::APP_STATE.with(|state_ref| {
            let st = state_ref.borrow();

            // ------------------------------------------------------------------
            // Validate state – bail out loudly on any invalid value instead of
            // silently coercing.  This surfaces bugs early.
            // ------------------------------------------------------------------

            if !st.viewport_x.is_finite()
                || !st.viewport_y.is_finite()
                || !st.zoom_level.is_finite()
            {
                web_sys::console::error_1(
                    &format!(
                        "Refusing to save: non-finite viewport detected (x={}, y={}, zoom={})",
                        st.viewport_x, st.viewport_y, st.zoom_level
                    )
                    .into(),
                );
                return; // Abort save – developer must investigate
            }

            let mut layout_nodes = std::collections::HashMap::new();
            for (id, node) in &st.workflow_nodes {
                if node.get_x().is_finite() && node.get_y().is_finite() {
                    layout_nodes
                        .insert(id.clone(), json!({ "x": node.get_x(), "y": node.get_y() }));
                } else {
                    web_sys::console::error_1(
                        &format!(
                            "Refusing to save: node '{}' has invalid position (x={}, y={})",
                            id,
                            node.get_x(),
                            node.get_y()
                        )
                        .into(),
                    );
                    // Do NOT include the node; we still continue so that other
                    // valid nodes/viewport can be persisted.
                }
            }

            let payload = json!({
                "nodes": layout_nodes,
                "viewport": {
                    "x": st.viewport_x,
                    "y": st.viewport_y,
                    "zoom": st.zoom_level,
                }
            });

            let payload_str = payload.to_string();

            spawn_local(async move {
                let wf_id = crate::state::APP_STATE.with(|s| s.borrow().current_workflow_id);
                match crate::network::ApiClient::patch_layout(&payload_str, wf_id.map(|v| v as u32))
                    .await
                {
                    Ok(_) => update_layout_status("Layout: saved", "green"),
                    Err(e) => {
                        update_layout_status("Layout: save failed", "red");
                        web_sys::console::error_1(&format!("layout save failed: {:?}", e).into());
                    }
                }
            });
        });
    });
}

/// Load app state from API
pub fn load_state_from_api(app_state: &mut AppState) {
    // Set loading state flags
    app_state.is_loading = true;
    app_state.data_loaded = false;
    app_state.api_load_attempted = true;

    // Spawn an async task to load agents from API
    spawn_local(async move {
        // Try to fetch all agents from the API
        // Determine current dashboard scope from global state (default "my")
        let scope_str = crate::state::APP_STATE.with(|state_ref| {
            let state = state_ref.borrow();
            state.dashboard_scope.as_str().to_string()
        });

        match ApiClient::get_agents_scoped(&scope_str).await {
            Ok(agents_json) => {
                match from_str::<Vec<ApiAgent>>(&agents_json) {
                    Ok(agents) => {
                        debug_log!("Loaded {} agents from API", agents.len());

                        // Update the agents in the global APP_STATE using message dispatch
                        crate::state::dispatch_global_message(crate::messages::Message::ClearAgents);
                        crate::state::dispatch_global_message(crate::messages::Message::AddAgents(agents.clone()));
                        // Update loading state directly in storage operations
                        crate::state::APP_STATE.with(|state_ref| {
                            let mut state = state_ref.borrow_mut();
                            state.is_loading = false;
                            state.data_loaded = true;
                        });

                        // --- IMPORTANT --------------------------------------------------
                        // We just mutated `state` above and now want to run
                        // `fix_stub_nodes()` which requires a *fresh* mutable
                        // borrow.  Calling it inside the previous closure
                        // would trigger a `BorrowMutError`.  Therefore we run
                        // it **after** the closure scope ends (borrow dropped
                        // here).
                        // ----------------------------------------------------------------

                        fix_stub_nodes();
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Error parsing agents from API: {}", e).into(),
                        );
                        // Update loading state flags even in case of error
                        crate::state::APP_STATE.with(|state_ref| {
                            let mut state = state_ref.borrow_mut();
                            state.is_loading = false;
                        });
                    }
                }
            }
            Err(e) => {
                web_sys::console::error_1(
                    &format!("Error fetching agents from API: {:?}", e).into(),
                );
                // Update loading state flags even in case of error
                crate::state::APP_STATE.with(|state_ref| {
                    let mut state = state_ref.borrow_mut();
                    state.is_loading = false;
                });
            }
        }

        // Schedule a UI refresh
        crate::state::AppState::refresh_ui_after_state_change().expect("Failed to refresh UI");

        // Call a function to signal that loading is complete
        mark_loading_complete();
    });
}

// Helper function to mark loading as complete and refresh UI
fn mark_loading_complete() {
    // Update loading state directly in storage operations
    crate::state::APP_STATE.with(|state_ref| {
        let mut state = state_ref.borrow_mut();
        state.is_loading = false;
        state.data_loaded = true;
    });

    // Schedule UI refresh
    let window = web_sys::window().expect("no global window exists");
    let closure = Closure::once(|| {
        if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
            web_sys::console::warn_1(
                &format!("Failed to refresh UI after state load: {:?}", e).into(),
            );
        }
    });

    window
        .set_timeout_with_callback_and_timeout_and_arguments_0(closure.as_ref().unchecked_ref(), 0)
        .expect("Failed to set timeout");

    closure.forget();
}

/// Save agent messages to the API
pub fn save_agent_messages_to_api(node_id: &str, messages: &[crate::models::Message]) {
    // Resolve the `agent_id` using the global mapping.  This avoids any
    // string-based assumptions about the node_id.
    use crate::state::APP_STATE;

    if let Some(agent_id) = APP_STATE.with(|s| {
        let st = s.borrow();
        st.workflow_nodes
            .get(node_id)
            .and_then(|n| n.get_agent_id())
    }) {
        // Clone the messages for the async block
        let messages_clone = messages.to_vec();

        // Spawn an async task to save messages
        spawn_local(async move {
            for message in messages_clone {
                // Convert our internal Message type to ApiMessageCreate
                let api_message = crate::models::ApiMessageCreate {
                    role: message.role,
                    content: message.content,
                };

                // Convert to JSON
                let message_json = match to_string(&api_message) {
                    Ok(json) => json,
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Error serializing message: {}", e).into(),
                        );
                        continue;
                    }
                };

                // Send to API
                if let Err(e) = ApiClient::create_agent_message(agent_id, &message_json).await {
                    web_sys::console::error_1(
                        &format!("Error saving message to API: {:?}", e).into(),
                    );
                } else {
                    debug_log!("Saved message to agent {} in API", agent_id);
                }
            }
        });
    }
}

/// Load agent messages from the API
#[allow(dead_code)]
pub fn load_agent_messages_from_api(node_id: &String, _agent_id: u32) {
    use crate::state::APP_STATE;

    if let Some(agent_id) = APP_STATE.with(|s| {
        let st = s.borrow();
        st.workflow_nodes
            .get(node_id)
            .and_then(|n| n.get_agent_id())
    }) {
        // Reference to the node ID for the closure
        let _node_id = node_id.to_string();

        // Spawn an async task to load messages
        spawn_local(async move {
            match ApiClient::get_agent_messages(agent_id).await {
                Ok(messages_json) => {
                    match from_str::<Vec<crate::models::ApiMessage>>(&messages_json) {
                        Ok(api_messages) => {
                            debug_log!(
                                "Loaded {} messages for agent {}",
                                api_messages.len(),
                                agent_id
                            );

                            // Convert API messages to our internal Message type
                            let _messages: Vec<crate::models::Message> = api_messages
                                .into_iter()
                                .map(|api_msg| crate::models::Message {
                                    role: api_msg.role,
                                    content: api_msg.content,
                                    timestamp: 0, // We'll use current timestamp since created_at is a string
                                })
                                .collect();

                            // TODO: store historical messages with thread model once
                            // the frontend chat rewrite lands.  Until then we ignore.
                        }
                        Err(e) => {
                            web_sys::console::error_1(
                                &format!("Error parsing messages: {}", e).into(),
                            );
                        }
                    }
                }
                Err(e) => {
                    web_sys::console::error_1(&format!("Error loading messages: {:?}", e).into());
                }
            }
        });
    }
}

/// Helper function to save just the nodes to the API
#[allow(dead_code)]
fn save_nodes_to_api(nodes: &HashMap<String, WorkflowNode>) -> Result<(), JsValue> {
    // Implementation to save nodes to API
    debug_log!("Saving {} nodes to API", nodes.len());

    for (node_id, node) in nodes {
        if matches!(
            node.get_semantic_type(),
            crate::models::NodeType::AgentIdentity
        ) {
            // Create or update agent in API
            debug_log!("Would save agent {}: {}", node_id, node.get_text());
            // Here you would call your API client to save the agent
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Helper – replace placeholder *stub* nodes with real AgentIdentity nodes once
// the agent list has been loaded.  A stub node is identified by:
//   • `node.text` starts with "node_" (legacy naming convention), and
//   • `node.agent_id == None`
// ---------------------------------------------------------------------------

pub(crate) fn fix_stub_nodes() {
    use crate::state::APP_STATE;

    APP_STATE.with(|state_ref| {
        // Panic on borrow error – should not occur in normal operation and
        // indicates a programming mistake when it does.
        let mut st = state_ref.borrow_mut();

        // Work with *copies* to avoid Rust's strict aliasing rules.

        let mut agent_pool: Vec<(u32, String)> = st
            .agents
            .iter()
            .filter(|(aid, _)| !st.agent_id_to_node_id.contains_key(aid))
            .map(|(&aid, a)| (aid, a.name.clone()))
            .collect();

        if agent_pool.is_empty() {
            return; // Nothing to reconcile
        }

        // Collect candidate placeholder node ids first.
        // Only convert nodes that are actually stub nodes (text starts with "node_")
        let candidate_ids: Vec<String> = st
            .workflow_nodes
            .iter()
            .filter_map(|(id, n)| {
                if n.get_agent_id().is_none() && n.get_text().starts_with("node_") {
                    Some(id.clone())
                } else {
                    None
                }
            })
            .collect();

        let mut upgraded_any = false;

        for node_id in candidate_ids {
            if agent_pool.is_empty() {
                break;
            }

            if let Some(node) = st.workflow_nodes.get_mut(&node_id) {
                if let Some((aid, name)) = agent_pool.pop() {
                    node.set_text(name.clone());
                    node.set_semantic_type(&crate::models::NodeType::AgentIdentity);
                    node.set_agent_id(Some(aid));
                    st.agent_id_to_node_id.insert(aid, node_id.clone());
                    upgraded_any = true;
                }
            }
        }

        if upgraded_any {
            st.mark_dirty();
        }
    });
}

// Save workflows to localStorage
pub fn save_workflows(_app_state: &AppState) -> Result<(), JsValue> {
    // TEMPORARILY DISABLED: localStorage workflow persistence to avoid sync conflicts
    // WebSocket-based backend persistence handles this more reliably
    /*
    let window = web_sys::window().expect("no global window exists");
    let local_storage = window.local_storage()?.expect("no local storage exists");

    // Convert the workflows to a JSON string
    let workflows_str = to_string(&app_state.workflows).map_err(|e| JsValue::from_str(&e.to_string()))?;

    // Save to localStorage
    local_storage.set_item("workflows", &workflows_str)?;

    // Also save the current workflow ID if it exists
    if let Some(workflow_id) = app_state.current_workflow_id {
        local_storage.set_item("current_workflow_id", &workflow_id.to_string())?;
    } else {
        local_storage.remove_item("current_workflow_id")?;
    }
    */

    Ok(())
}

// Load workflows from localStorage
pub fn load_workflows(_app_state: &mut AppState) -> Result<(), JsValue> {
    // TEMPORARILY DISABLED: localStorage workflow loading to avoid sync conflicts
    // Backend API calls handle workflow loading more reliably
    /*
    let window = web_sys::window().expect("no global window exists");
    let local_storage = window.local_storage()?.expect("no local storage exists");

    // Load workflows
    if let Some(workflows_str) = local_storage.get_item("workflows")? {
        match from_str::<HashMap<u32, Workflow>>(&workflows_str) {
            Ok(workflows) => {
                app_state.workflows = workflows;
            },
            Err(e) => {
                web_sys::console::error_1(&format!("Failed to parse workflows: {}", e).into());
                // Initialize with empty workflows
                app_state.workflows = HashMap::new();
            }
        }
    }

    // Load current workflow ID
    if let Some(workflow_id_str) = local_storage.get_item("current_workflow_id")? {
        if let Ok(workflow_id) = workflow_id_str.parse::<u32>() {
            app_state.current_workflow_id = Some(workflow_id);

            // Load the nodes from the current workflow into nodes
            if let Some(workflow) = app_state.workflows.get(&workflow_id) {
                app_state.workflow_nodes.clear();
                for node in &workflow.get_nodes() {
                    app_state.workflow_nodes.insert(node.node_id.clone(), node.clone());
                }
            }
        }
    }
    */

    debug_log!(
        "Workflows will be loaded from backend API instead of localStorage"
    );
    Ok(())
}

/// Save canvas structure data (nodes and edges) to current workflow
pub fn save_canvas_data_to_api(app_state: &AppState) {
    // Skip if dragging to avoid too many API calls
    if app_state.dragging.is_some() || app_state.canvas_dragging {
        return;
    }

    // Transform current workflow data to backend format
    let canvas_data = if let Some(workflow_id) = app_state.current_workflow_id {
        if let Some(workflow) = app_state.workflows.get(&workflow_id) {
            // Use contract-compliant serialization directly (no manual transformation)
            serde_json::to_value(&serde_json::json!({
                "nodes": workflow.get_nodes(),
                "edges": workflow.get_edges()
            }))
            .unwrap_or_else(|_| {
                // Fallback to empty structure if serialization fails
                serde_json::json!({
                    "nodes": Vec::<serde_json::Value>::new(),
                    "edges": Vec::<serde_json::Value>::new()
                })
            })
        } else {
            // If no workflow exists but we have a current_workflow_id,
            // we should still send the current state
            serde_json::json!({
                "nodes": Vec::<serde_json::Value>::new(),
                "edges": Vec::<serde_json::Value>::new()
            })
        }
    } else {
        // No current workflow, create empty structure
        serde_json::json!({
            "nodes": Vec::<serde_json::Value>::new(),
            "edges": Vec::<serde_json::Value>::new()
        })
    };

    // Only save if we have actual content to save
    let has_nodes = canvas_data
        .get("nodes")
        .and_then(|n| n.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false);

    let has_edges = canvas_data
        .get("edges")
        .and_then(|e| e.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false);

    if !has_nodes && !has_edges {
        // Don't spam API with empty canvas data unless it's the first save
        return;
    }

    debug_log!("🚀 Sending canvas data via type-safe generated client");

    // Use type-safe generated client instead of manual API calls
    crate::network::generated_client::save_canvas_data_typed(canvas_data);
}
