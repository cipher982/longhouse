//! Simple tab-bar widget that lists workflows and lets the user switch or
//! create new ones.  First iteration – no rename or delete yet.

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::Document;

use crate::dom_utils::mount_in_overlay;
use crate::messages::Message;
use crate::state::{dispatch_global_message, APP_STATE};

/// Call once on canvas view mount.  Renders the bar under the header.
pub fn init(document: &Document) -> Result<(), JsValue> {
    // Check if already initialised
    if document.get_element_by_id("workflow-bar").is_some() {
        return Ok(());
    }

    // Root bar element
    let bar_el = document.create_element("div")?;
    bar_el.set_attribute("id", "workflow-bar")?;
    bar_el.set_attribute("class", "workflow-bar")?;

    // Inject toolbar button CSS once
    if document.get_element_by_id("toolbar-btn-style").is_none() {
        let style_el = document.create_element("style")?;
        style_el.set_attribute("id", "toolbar-btn-style")?;
        style_el.set_text_content(Some(r#"
.run-btn.running{pointer-events:none;animation:spin 1s linear infinite;}
.run-btn.success{color:#6bff92;}
.run-btn.failed{color:#ff4e4e;}
@keyframes spin{from{transform:rotate(0deg);}to{transform:rotate(360deg);}}
.toolbar-btn.active{background-color:#3b82f6;color:white;box-shadow:0 0 10px rgba(59, 130, 246, 0.5);}
"#));
        document.body().unwrap().append_child(&style_el)?;
    }

    let list_el = document.create_element("ul")?;
    list_el.set_attribute("class", "workflow-tab-list")?;

    // Toolbar actions container (right side)
    let actions_el = document.create_element("div")?;
    actions_el.set_attribute("class", "toolbar-actions")?;

    // --------------------------------------------------------------------
    // Run (▶︎) button – trigger workflow execution
    // --------------------------------------------------------------------
    let run_btn = document.create_element("button")?;
    run_btn.set_attribute("type", "button")?;
    run_btn.set_inner_html("▶︎");
    run_btn.set_attribute("class", "toolbar-btn")?;
    run_btn.set_attribute("id", "run-workflow-btn")?;
    run_btn.set_attribute("title", "Run Workflow (⌘/Ctrl + R)")?;

    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            // Read current workflow id at click time (avoid stale capture)
            if let Some(current_id) = APP_STATE.with(|st| st.borrow().current_workflow_id) {
                dispatch_global_message(Message::StartWorkflowExecution {
                    workflow_id: current_id,
                });
            } else {
                web_sys::console::warn_1(&"No workflow selected – cannot run".into());
            }
        }));
        run_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    let _ = actions_el.append_child(&run_btn);

    // --------------------------------------------------------------------
    // Schedule (⏰) button – schedule workflow execution
    // --------------------------------------------------------------------
    let schedule_btn = document.create_element("button")?;
    schedule_btn.set_attribute("type", "button")?;
    schedule_btn.set_inner_html("⏰");
    schedule_btn.set_attribute("class", "toolbar-btn")?;
    schedule_btn.set_attribute("id", "schedule-workflow-btn")?;
    schedule_btn.set_attribute("title", "Schedule Workflow")?;

    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            // Read current workflow id at click time (avoid stale capture)
            if let Some(current_id) = APP_STATE.with(|st| st.borrow().current_workflow_id) {
                show_schedule_modal(current_id);
            } else {
                web_sys::console::warn_1(&"No workflow selected – cannot schedule".into());
            }
        }));
        schedule_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    let _ = actions_el.append_child(&schedule_btn);

    // Logs button 📜
    let logs_btn = document.create_element("button")?;
    logs_btn.set_attribute("type", "button")?;
    logs_btn.set_inner_html("📜");
    logs_btn.set_attribute("class", "toolbar-btn")?;
    logs_btn.set_attribute("title", "Toggle Logs")?;
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            crate::state::dispatch_global_message(crate::messages::Message::ToggleLogDrawer);
        }));
        logs_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    let _ = actions_el.append_child(&logs_btn);

    // Execution history button 🕒
    let hist_btn = document.create_element("button")?;
    hist_btn.set_attribute("type", "button")?;
    hist_btn.set_inner_html("🕒");
    hist_btn.set_attribute("class", "toolbar-btn")?;
    hist_btn.set_attribute("title", "Execution History")?;
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            crate::state::dispatch_global_message(crate::messages::Message::ToggleExecutionHistory);
            // If opening, also trigger load for current workflow
            crate::state::APP_STATE.with(|st| {
                if st.borrow().exec_history_open {
                    if let Some(wf_id) = st.borrow().current_workflow_id {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::LoadExecutionHistory { workflow_id: wf_id },
                        );
                    }
                }
            });
        }));
        hist_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    let _ = actions_el.append_child(&hist_btn);

    // Center view button
    let center_btn = document.create_element("button")?;
    center_btn.set_attribute("type", "button")?;
    center_btn.set_inner_html("⌖");
    center_btn.set_attribute("class", "toolbar-btn")?;
    center_btn.set_attribute("title", "Center View")?;
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            dispatch_global_message(Message::CenterView);
        }));
        center_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    actions_el.append_child(&center_btn)?;

    // Connection mode toggle button
    let connect_btn = document.create_element("button")?;
    connect_btn.set_attribute("type", "button")?;
    connect_btn.set_inner_html("🔗");
    connect_btn.set_attribute("class", "toolbar-btn")?;
    connect_btn.set_attribute("id", "connection-mode-btn")?;
    connect_btn.set_attribute("title", "Toggle Connection Mode")?;
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            dispatch_global_message(Message::ToggleConnectionMode);
        }));
        connect_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    actions_el.append_child(&connect_btn)?;

    // Template Gallery button 📋
    let gallery_btn = document.create_element("button")?;
    gallery_btn.set_attribute("type", "button")?;
    gallery_btn.set_inner_html("📋");
    gallery_btn.set_attribute("class", "toolbar-btn")?;
    gallery_btn.set_attribute("title", "Template Gallery")?;
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            let _ = crate::components::template_gallery::show_gallery();
        }));
        gallery_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    actions_el.append_child(&gallery_btn)?;

    // Dropdown menu (⋮)
    let dropdown_container = document.create_element("div")?;
    dropdown_container.set_attribute("class", "dropdown-container")?;

    let dropdown_toggle = document.create_element("button")?;
    dropdown_toggle.set_attribute("type", "button")?;
    dropdown_toggle.set_inner_html("⋮");
    dropdown_toggle.set_attribute("class", "dropdown-toggle")?;
    dropdown_toggle.set_attribute("title", "More Options")?;

    let dropdown_menu = document.create_element("div")?;
    dropdown_menu.set_attribute("class", "dropdown-menu")?;

    let clear_btn = document.create_element("button")?;
    clear_btn.set_attribute("type", "button")?;
    clear_btn.set_inner_html("Clear Canvas");
    clear_btn.set_attribute("class", "dropdown-item danger")?;
    {
        let dropdown_menu_clone = dropdown_menu.clone();
        let dropdown_toggle_clone = dropdown_toggle.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            let _ = dropdown_menu_clone.class_list().remove_1("show");
            let _ = dropdown_toggle_clone.class_list().remove_1("active");
            dispatch_global_message(Message::ClearCanvas);
        }));
        clear_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    dropdown_menu.append_child(&clear_btn)?;

    // --------------------------------------------------------------
    // Rename workflow action
    // --------------------------------------------------------------
    let rename_btn = document.create_element("button")?;
    rename_btn.set_attribute("type", "button")?;
    rename_btn.set_inner_html("Rename Workflow");
    rename_btn.set_attribute("class", "dropdown-item")?;
    {
        let dropdown_menu_clone = dropdown_menu.clone();
        let dropdown_toggle_clone = dropdown_toggle.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            // Close dropdown
            let _ = dropdown_menu_clone.class_list().remove_1("show");
            let _ = dropdown_toggle_clone.class_list().remove_1("active");

            // Ask for new name & description
            let win = web_sys::window().unwrap();
            let new_name_opt = win
                .prompt_with_message("New workflow name?")
                .unwrap_or(None);
            if let Some(new_name) = new_name_opt {
                let new_name = new_name.trim();
                if !new_name.is_empty() {
                    let desc_opt = win
                        .prompt_with_message("Description (optional)")
                        .unwrap_or(None);
                    let description = desc_opt.unwrap_or_default();
                    // Dispatch rename command
                    crate::state::APP_STATE.with(|st| {
                        if let Some(current_id) = st.borrow().current_workflow_id {
                            crate::state::dispatch_global_message(
                                crate::messages::Message::RenameWorkflow {
                                    workflow_id: current_id,
                                    name: new_name.to_string(),
                                    description: description.clone(),
                                },
                            );
                        } else {
                            web_sys::console::warn_1(&"No workflow selected to rename".into());
                        }
                    });
                }
            }
        }));
        rename_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    dropdown_menu.append_child(&rename_btn)?;

    // --------------------------------------------------------------
    // Delete workflow action
    // --------------------------------------------------------------
    let delete_btn = document.create_element("button")?;
    delete_btn.set_attribute("type", "button")?;
    delete_btn.set_inner_html("Delete Workflow");
    delete_btn.set_attribute("class", "dropdown-item danger")?;
    {
        let dropdown_menu_clone = dropdown_menu.clone();
        let dropdown_toggle_clone = dropdown_toggle.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            // Close dropdown
            let _ = dropdown_menu_clone.class_list().remove_1("show");
            let _ = dropdown_toggle_clone.class_list().remove_1("active");

            // Check if deletion is already in progress
            let is_deleting =
                crate::state::APP_STATE.with(|st| st.borrow().deleting_workflow.is_some());
            if is_deleting {
                return; // Prevent double-submission
            }

            let confirm = web_sys::window().unwrap().confirm_with_message("Are you sure you want to delete this workflow? This can be restored by support within 30 days.").unwrap_or(false);
            if !confirm {
                return;
            }

            crate::state::APP_STATE.with(|st| {
                if let Some(current_id) = st.borrow().current_workflow_id {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::DeleteWorkflow {
                            workflow_id: current_id,
                        },
                    );
                } else {
                    web_sys::console::warn_1(&"No workflow selected to delete".into());
                }
            });
        }));
        delete_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    dropdown_menu.append_child(&delete_btn)?;
    dropdown_container.append_child(&dropdown_toggle)?;
    dropdown_container.append_child(&dropdown_menu)?;
    actions_el.append_child(&dropdown_container)?;

    // Dropdown toggle click handler – opens the menu and positions it
    {
        let dropdown_menu_clone = dropdown_menu.clone();
        let dropdown_toggle_clone = dropdown_toggle.clone();

        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |e: web_sys::MouseEvent| {
            e.stop_propagation();

            let is_open = dropdown_menu_clone.class_list().contains("show");

            if is_open {
                let _ = dropdown_menu_clone.class_list().remove_1("show");
                let _ = dropdown_toggle_clone.class_list().remove_1("active");
            } else {
                // Mount dropdown in #overlay-root portal
                mount_in_overlay(&dropdown_menu_clone);

                // Position the menu right-aligned to the toggle button
                let bbox = dropdown_toggle_clone.get_bounding_client_rect();

                // Scroll offsets
                let win = web_sys::window().unwrap();
                let scroll_x = win.scroll_x().unwrap_or(0.0);
                let scroll_y = win.scroll_y().unwrap_or(0.0);

                let top_px = bbox.bottom() + scroll_y;
                let left_px = bbox.right() + scroll_x - dropdown_menu_clone.client_width() as f64;

                if let Some(html_el) = dropdown_menu_clone.dyn_ref::<web_sys::HtmlElement>() {
                    let _ = html_el
                        .style()
                        .set_property("top", &format!("{}px", top_px));
                    let _ = html_el
                        .style()
                        .set_property("left", &format!("{}px", left_px));
                }

                let _ = dropdown_menu_clone.class_list().add_1("show");
                let _ = dropdown_toggle_clone.class_list().add_1("active");
            }
        }));
        dropdown_toggle.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    // Document click handler to close dropdown when clicking outside
    {
        let dropdown_menu_clone = dropdown_menu.clone();
        let dropdown_toggle_clone = dropdown_toggle.clone();
        let dropdown_container_clone = dropdown_container.clone();
        let dropdown_menu_for_contains = dropdown_menu.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |e: web_sys::MouseEvent| {
            if let Some(target) = e.target() {
                if let Ok(element) = target.dyn_into::<web_sys::Element>() {
                    // Close only if the click happened outside both the toggle
                    // container *and* the menu itself (when it is appended to body)
                    if !dropdown_container_clone.contains(Some(&element))
                        && !dropdown_menu_for_contains.contains(Some(&element))
                    {
                        let _ = dropdown_menu_clone.class_list().remove_1("show");
                        let _ = dropdown_toggle_clone.class_list().remove_1("active");
                    }
                }
            }
        }));
        document.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    bar_el.append_child(&list_el)?;
    bar_el.append_child(&actions_el)?;

    // ------------------------------------------------------------------
    // Deterministic insertion: the canvas page **must** have already
    // created <div id="main-content-area">.  If it is missing we surface
    // an error instead of silently falling back to other containers.
    // ------------------------------------------------------------------

    debug_assert!(
        document.get_element_by_id("main-content-area").is_some(),
        "Canvas layout must initialise main-content-area before workflow_switcher::init() is called",
    );

    let main_content = document
        .get_element_by_id("main-content-area")
        .ok_or_else(|| {
            JsValue::from_str("main-content-area missing – canvas layout not initialised")
        })?;

    // Insert the bar at the very top of the main content column.
    main_content.insert_before(&bar_el, main_content.first_child().as_ref())?;

    refresh(document)?;

    Ok(())
}

/// Rebuild the tab list from current AppState
pub fn refresh(document: &Document) -> Result<(), JsValue> {
    // Only refresh workflow bar if we're in canvas view (main-content-area exists)
    if document.get_element_by_id("main-content-area").is_none() {
        // Not in canvas view, skip workflow bar refresh
        return Ok(());
    }

    // Make sure workflow bar is initialized first
    let workflow_bar = match document.get_element_by_id("workflow-bar") {
        Some(bar) => bar,
        None => {
            // Workflow bar not initialized yet, call init first
            init(document)?;
            document
                .get_element_by_id("workflow-bar")
                .ok_or_else(|| JsValue::from_str("workflow-bar element not found after init"))?
        }
    };

    let list_el = workflow_bar
        .first_child()
        .ok_or_else(|| {
            JsValue::from_str("workflow-bar has no children - initialization may have failed")
        })?
        .dyn_into::<web_sys::Element>()?;

    // Clear existing children
    while let Some(child) = list_el.first_child() {
        list_el.remove_child(&child)?;
    }

    // Borrow state once
    let (workflows_vec, current_id) = APP_STATE.with(|state| {
        let st = state.borrow();
        (
            st.workflows.values().cloned().collect::<Vec<_>>(),
            st.current_workflow_id,
        )
    });

    for wf in workflows_vec {
        let li = document.create_element("li")?;
        li.set_attribute(
            "class",
            if Some(wf.id) == current_id {
                "tab active"
            } else {
                "tab"
            },
        )?;
        li.set_attribute("data-id", &wf.id.to_string())?;
        li.set_text_content(Some(&wf.name));

        // Click handler – select workflow
        let id_clone = wf.id;
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            dispatch_global_message(Message::SelectWorkflow {
                workflow_id: id_clone,
            });
        }));
        li.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();

        list_el.append_child(&li)?;
    }

    // Plus-tab
    let plus_li = document.create_element("li")?;
    plus_li.set_attribute("class", "tab plus-tab")?;
    plus_li.set_text_content(Some("＋"));

    let cb_new = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
        // Check if workflow creation is already in progress
        let is_creating = APP_STATE.with(|st| st.borrow().creating_workflow);
        if is_creating {
            return; // Prevent double-submission
        }

        // Prompt for name
        let name = web_sys::window()
            .unwrap()
            .prompt_with_message("Workflow name?")
            .unwrap_or(None);
        if let Some(n) = name {
            if !n.trim().is_empty() {
                dispatch_global_message(Message::CreateWorkflow {
                    name: n.trim().to_string(),
                });
                // UI will refresh via below command from reducer – but we also manually refresh after short delay
            }
        }
    }));
    plus_li.add_event_listener_with_callback("click", cb_new.as_ref().unchecked_ref())?;
    cb_new.forget();

    list_el.append_child(&plus_li)?;

    // Update run button state if it exists
    update_run_button(document)?;

    // Update workflow buttons (plus button, delete buttons) loading states
    update_workflow_buttons(document)?;

    Ok(())
}

/// Update the ▶︎ Run button CSS classes based on AppState.current_execution.
/// Adds one of: `running`, `success`, `failed` or removes all for idle.
pub fn update_run_button(document: &Document) -> Result<(), JsValue> {
    use crate::models::NodeExecStatus;
    use crate::state::{ExecPhase, APP_STATE};

    if let Some(btn) = document.get_element_by_id("run-workflow-btn") {
        let class_list = btn.class_list();
        let _ = class_list.remove_1("running");
        let _ = class_list.remove_1("success");
        let _ = class_list.remove_1("failed");

        let (phase_opt, progress_info) = APP_STATE.with(|st| {
            let state = st.borrow();
            let phase = state.current_execution.clone().map(|e| e.status);

            // Count node progress for enhanced feedback
            let total_nodes = state.workflow_nodes.len();
            let running_nodes = state
                .workflow_nodes
                .iter()
                .filter(|(id, _)| {
                    state
                        .ui_state
                        .get(*id)
                        .map_or(false, |ui| ui.exec_status == Some(NodeExecStatus::Running))
                })
                .count();
            let completed_nodes = state
                .workflow_nodes
                .iter()
                .filter(|(id, _)| {
                    state.ui_state.get(*id).map_or(false, |ui| {
                        matches!(
                            ui.exec_status,
                            Some(NodeExecStatus::Completed) | Some(NodeExecStatus::Failed)
                        )
                    })
                })
                .count();

            (phase, (total_nodes, running_nodes, completed_nodes))
        });

        let (total_nodes, running_nodes, completed_nodes) = progress_info;

        if let Some(phase) = phase_opt {
            match phase {
                ExecPhase::Running | ExecPhase::Starting => {
                    let _ = class_list.add_1("running");

                    // Update button text with progress during execution only
                    if total_nodes > 0 {
                        let progress_text = if running_nodes > 0 {
                            format!("▶︎ Running... ({}/{})", completed_nodes, total_nodes)
                        } else {
                            "▶︎ Starting...".to_string()
                        };
                        btn.set_inner_html(&progress_text);
                    } else {
                        btn.set_inner_html("▶︎ Running...");
                    }
                }
                ExecPhase::Success | ExecPhase::Failed => {
                    // Button returns to normal state after completion
                    // Status is communicated via toast notifications and results panel
                    btn.set_inner_html("▶︎ Run");
                }
            }
        } else {
            // Default state
            btn.set_inner_html("▶︎ Run");
        }
    }

    Ok(())
}

/// Update the plus button visual state based on workflow loading states
pub fn update_workflow_buttons(document: &Document) -> Result<(), JsValue> {
    use crate::state::APP_STATE;

    // Update plus button for creation state
    if let Some(plus_btn) = document.query_selector(".plus-tab").ok().flatten() {
        let (is_creating, _deleting_id, _updating_id) = APP_STATE.with(|st| {
            let state = st.borrow();
            (
                state.creating_workflow,
                state.deleting_workflow,
                state.updating_workflow,
            )
        });

        if is_creating {
            plus_btn.set_text_content(Some("⟳")); // Spinner character
            let _ = plus_btn.set_attribute("style", "pointer-events: none; opacity: 0.6;");
        } else {
            plus_btn.set_text_content(Some("＋"));
            let _ = plus_btn.set_attribute("style", "");
        }
    }

    // Update delete buttons in dropdown (if any are showing delete state)
    if let Some(delete_btn) = document
        .query_selector(".dropdown-item.danger")
        .ok()
        .flatten()
    {
        let is_deleting = APP_STATE.with(|st| st.borrow().deleting_workflow.is_some());
        if is_deleting {
            delete_btn.set_text_content(Some("Deleting..."));
            let _ = delete_btn.set_attribute("style", "pointer-events: none; opacity: 0.6;");
        } else {
            delete_btn.set_text_content(Some("Delete Workflow"));
            let _ = delete_btn.set_attribute("style", "");
        }
    }

    Ok(())
}

/// Show a modal dialog for scheduling a workflow with cron expression input
fn show_schedule_modal(workflow_id: u32) {
    let window = match web_sys::window() {
        Some(w) => w,
        None => return,
    };

    let document = match window.document() {
        Some(d) => d,
        None => return,
    };

    // Create modal overlay
    let overlay = match document.create_element("div") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = overlay.set_attribute("class", "modal-overlay");
    let _ = overlay.set_attribute("id", "schedule-modal-overlay");

    // Create modal content
    let modal = match document.create_element("div") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = modal.set_attribute("class", "modal schedule-modal");

    // Modal header
    let header = match document.create_element("div") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = header.set_attribute("class", "modal-header");
    header.set_inner_html("<h3>Schedule Workflow</h3>");

    // Modal body
    let body = match document.create_element("div") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = body.set_attribute("class", "modal-body");

    // Cron expression input
    let input_html = r#"
        <div class="form-group">
            <label for="cron-expression">Cron Expression:</label>
            <input type="text" id="cron-expression" class="input w-full"
                   placeholder="0 9 * * 1-5"
                   title="Examples: '0 9 * * 1-5' (weekdays at 9 AM), '0 0 * * 0' (Sundays at midnight)">
            <div class="help-text">
                <p>Examples:</p>
                <ul>
                    <li><code>0 9 * * 1-5</code> - Weekdays at 9:00 AM</li>
                    <li><code>0 0 * * 0</code> - Sundays at midnight</li>
                    <li><code>*/15 * * * *</code> - Every 15 minutes</li>
                    <li><code>0 2 1 * *</code> - First day of every month at 2:00 AM</li>
                </ul>
            </div>
        </div>
    "#;
    body.set_inner_html(input_html);

    // Modal footer
    let footer = match document.create_element("div") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = footer.set_attribute("class", "modal-footer");

    let cancel_btn = match document.create_element("button") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = cancel_btn.set_attribute("type", "button");
    let _ = cancel_btn.set_attribute("class", "btn btn-secondary");
    cancel_btn.set_text_content(Some("Cancel"));

    let schedule_btn = match document.create_element("button") {
        Ok(el) => el,
        Err(_) => return,
    };
    let _ = schedule_btn.set_attribute("type", "button");
    let _ = schedule_btn.set_attribute("class", "btn btn-primary");
    let _ = schedule_btn.set_attribute("id", "confirm-schedule-btn");
    schedule_btn.set_text_content(Some("Schedule"));

    let _ = footer.append_child(&cancel_btn);
    let _ = footer.append_child(&schedule_btn);

    // Assemble modal
    let _ = modal.append_child(&header);
    let _ = modal.append_child(&body);
    let _ = modal.append_child(&footer);
    let _ = overlay.append_child(&modal);

    // Add event handlers
    {
        let overlay_clone = overlay.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            let _ = overlay_clone.remove();
        }));
        let _ = cancel_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
        cb.forget();
    }

    // Schedule button handler
    {
        let overlay_clone = overlay.clone();
        let document_clone = document.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            if let Some(input) = document_clone.get_element_by_id("cron-expression") {
                if let Some(html_input) = input.dyn_ref::<web_sys::HtmlInputElement>() {
                    let cron_expr = html_input.value().trim().to_string();
                    if !cron_expr.is_empty() {
                        // Dispatch schedule message
                        dispatch_global_message(Message::ScheduleWorkflow {
                            workflow_id,
                            cron_expression: cron_expr,
                        });
                        let _ = overlay_clone.remove();
                    } else {
                        // Show error for empty cron expression
                        if let Some(window) = web_sys::window() {
                            let _ =
                                window.alert_with_message("Please enter a valid cron expression");
                        }
                    }
                }
            }
        }));
        let _ = schedule_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
        cb.forget();
    }

    // Close on overlay click (outside modal)
    {
        let overlay_clone = overlay.clone();
        let modal_clone = modal.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |e: web_sys::MouseEvent| {
            if let Some(target) = e.target() {
                if let Ok(element) = target.dyn_into::<web_sys::Element>() {
                    if element == overlay_clone && !modal_clone.contains(Some(&element)) {
                        let _ = overlay_clone.remove();
                    }
                }
            }
        }));
        let _ = overlay.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
        cb.forget();
    }

    // Add to body and focus input
    let _ = document.body().unwrap().append_child(&overlay);

    // Focus the cron expression input
    if let Some(input) = document.get_element_by_id("cron-expression") {
        if let Some(html_input) = input.dyn_ref::<web_sys::HtmlInputElement>() {
            let _ = html_input.focus();
        }
    }
}

/// Update connection mode button visual state
pub fn update_connection_button(document: &Document) -> Result<(), JsValue> {
    if let Some(btn) = document.get_element_by_id("connection-mode-btn") {
        let is_active = APP_STATE.with(|state| state.borrow().connection_mode);

        if is_active {
            btn.set_attribute("class", "toolbar-btn active")?;
            btn.set_attribute(
                "title",
                "Exit Connection Mode (Click nodes to connect them)",
            )?;
        } else {
            btn.set_attribute("class", "toolbar-btn")?;
            btn.set_attribute("title", "Toggle Connection Mode")?;
        }
    }
    Ok(())
}
