use wasm_bindgen::prelude::*;
use web_sys::{
    Document,
    Event,
    KeyboardEvent,
    HtmlCanvasElement,
    HtmlTextAreaElement,
    HtmlSelectElement,
    MouseEvent,
};
use js_sys::{Math, Date};
use crate::models::{NodeType, Message};
use crate::state::{APP_STATE, AppState};
use std::rc::Rc;
use std::cell::RefCell;
use wasm_bindgen::closure::Closure;
use wasm_bindgen::JsValue;
use wasm_bindgen::JsCast;
use crate::network::fetch_available_models;
use wasm_bindgen_futures::spawn_local;

pub fn setup_ui(document: &Document) -> Result<(), JsValue> {
    // Find the app container
    let app_container = document
        .get_element_by_id("app-container")
        .ok_or(JsValue::from_str("Could not find app-container"))?;

    // Create "Create Agent" button instead of input field
    let create_agent_button = document.create_element("button")?;
    create_agent_button.set_inner_html("Create Agent");
    create_agent_button.set_attribute("id", "create-agent-button")?;

    // Create model selection dropdown
    let model_select = document.create_element("select")?;
    model_select.set_attribute("id", "model-select")?;

    // Initially populate with default models
    update_model_dropdown(document)?;

    // Create auto-fit toggle switch instead of button
    let auto_fit_container = document.create_element("div")?;
    auto_fit_container.set_attribute("id", "auto-fit-container")?;
    auto_fit_container.set_attribute("class", "toggle-container")?;

    let auto_fit_label = document.create_element("label")?;
    auto_fit_label.set_attribute("for", "auto-fit-toggle")?;
    auto_fit_label.set_attribute("class", "toggle-label")?;

    let auto_fit_input = document.create_element("input")?;
    auto_fit_input.set_attribute("type", "checkbox")?;
    auto_fit_input.set_attribute("id", "auto-fit-toggle")?;
    auto_fit_input.set_attribute("class", "toggle-checkbox")?;

    // Check if auto-fit is enabled in current state
    let auto_fit_enabled = APP_STATE.with(|state| {
        let state = state.borrow();
        state.auto_fit
    });

    // Set the initial checked state based on the app state
    if auto_fit_enabled {
        auto_fit_input.set_attribute("checked", "")?;
    }

    let toggle_slider = document.create_element("span")?;
    toggle_slider.set_attribute("class", "toggle-slider")?;

    auto_fit_label.append_child(&auto_fit_input)?;
    auto_fit_label.append_child(&toggle_slider)?;
    auto_fit_container.append_child(&auto_fit_label)?;

    // Create center view button
    let center_button = document.create_element("button")?;
    center_button.set_inner_html("Center View");
    center_button.set_attribute("id", "center-button")?;

    // Create clear all button
    let clear_button = document.create_element("button")?;
    clear_button.set_inner_html("Clear All");
    clear_button.set_attribute("id", "clear-button")?;

    // Create input panel (controls)
    let input_panel = document.create_element("div")?;
    input_panel.set_id("input-panel");
    input_panel.append_child(&create_agent_button)?;
    input_panel.append_child(&model_select)?;
    input_panel.append_child(&auto_fit_container)?;
    input_panel.append_child(&center_button)?;
    input_panel.append_child(&clear_button)?;

    // Create canvas container
    let canvas_container = document.create_element("div")?;
    canvas_container.set_id("canvas-container");

    // Create canvas
    let canvas = document.create_element("canvas")?;
    canvas.set_id("node-canvas");
    canvas_container.append_child(&canvas)?;

    // Create instruction text
    let instruction_text = document.create_element("div")?;
    instruction_text.set_class_name("instruction-text");
    // instruction_text.set_inner_html("Type text in the input box above and send it to the AI. The response will appear as a connected node on the canvas. You can drag nodes around to organize them.");

    // Add all components to app container
    app_container.append_child(&canvas_container)?;
    app_container.append_child(&input_panel)?;
    app_container.append_child(&instruction_text)?;

    // Set up event handlers
    setup_create_agent_button_handler(document)?;
    setup_auto_fit_button_handler(document)?;
    setup_center_view_handler(document)?;
    setup_model_select_handler(document)?;
    setup_clear_button_handler(document)?;

    // Fetch available models from the backend
    fetch_models_from_backend(document)?;

    // Create the agent input modal
    create_agent_input_modal(document)?;

    Ok(())
}

fn update_model_dropdown(document: &Document) -> Result<(), JsValue> {
    if let Some(select_el) = document.get_element_by_id("model-select") {
        // Clear existing options
        select_el.set_inner_html("");

        // Get available models from state
        let models = APP_STATE.with(|state| {
            let state = state.borrow();
            state.available_models.clone()
        });

        // Get selected model
        let selected_model = APP_STATE.with(|state| {
            let state = state.borrow();
            state.selected_model.clone()
        });

        // Add options to dropdown
        for (value, label) in models.iter() {
            let option = document.create_element("option")?;
            option.set_attribute("value", value)?;

            // Set selected if it matches current selection
            if value == &selected_model {
                option.set_attribute("selected", "selected")?;
            }

            option.set_inner_html(label);
            select_el.append_child(&option)?;
        }
    }

    Ok(())
}

fn fetch_models_from_backend(document: &Document) -> Result<(), JsValue> {
    let document_clone = document.clone();

    // Fetch models asynchronously
    spawn_local(async move {
        if let Ok(()) = fetch_available_models().await {
            // Update the dropdown with fetched models
            let _ = update_model_dropdown(&document_clone);
        }
    });

    Ok(())
}

fn setup_create_agent_button_handler(document: &Document) -> Result<(), JsValue> {
    let create_agent_button = document.get_element_by_id("create-agent-button").unwrap();

    let click_callback = Closure::wrap(Box::new(move |_event: MouseEvent| {
        // Create a new agent node
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();

            // Get viewport center coordinates
            let viewport_width = if state.canvas_width > 0.0 { state.canvas_width } else { 800.0 };
            let viewport_height = if state.canvas_height > 0.0 { state.canvas_height } else { 600.0 };

            let x = state.viewport_x + (viewport_width / state.zoom_level) / 2.0 - 75.0; // Center - half node width
            let y = state.viewport_y + (viewport_height / state.zoom_level) / 2.0 - 50.0; // Center - half node height

            // Create a new agent node
            let _node_id = state.add_node(
                "New Agent".to_string(),
                x,
                y,
                NodeType::AgentIdentity
            );

            // Draw the nodes
            state.draw_nodes();

            // Save state after adding the node
            if let Err(e) = state.save_if_modified() {
                web_sys::console::error_1(&format!("Failed to save state: {:?}", e).into());
            }

            web_sys::console::log_1(&"Created new agent node".into());
        });
    }) as Box<dyn FnMut(_)>);

    create_agent_button.add_event_listener_with_callback("click", click_callback.as_ref().unchecked_ref())?;
    click_callback.forget();

    Ok(())
}

pub fn setup_canvas(document: &Document) -> Result<(), JsValue> {
    let canvas = document.get_element_by_id("node-canvas")
        .unwrap()
        .dyn_into::<HtmlCanvasElement>()?;

    // Set canvas dimensions to match container
    resize_canvas(&canvas)?;

    let context = canvas
        .get_context("2d")?
        .unwrap()
        .dyn_into::<web_sys::CanvasRenderingContext2d>()?;

    // Ensure initial context is properly scaled with device pixel ratio
    let window = web_sys::window().expect("no global window exists");
    let dpr = window.device_pixel_ratio();
    let _ = context.set_transform(1.0, 0.0, 0.0, 1.0, 0.0, 0.0);
    let _ = context.scale(dpr, dpr);

    APP_STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.canvas = Some(canvas.clone());
        state.context = Some(context);
    });

    // Set up mouse events for the canvas
    setup_canvas_mouse_events(&canvas)?;

    // Set up resize handler
    setup_resize_handler(&canvas)?;

    // Setup animation loop for refreshing the canvas
    internal_setup_animation_loop();

    Ok(())
}

fn resize_canvas(canvas: &HtmlCanvasElement) -> Result<(), JsValue> {
    // Get the parent container dimensions
    let window = web_sys::window().expect("no global window exists");
    let document = window.document().expect("no document exists");

    if let Some(container) = document.get_element_by_id("canvas-container") {
        let container_width = container.client_width();
        let container_height = container.client_height();

        // Get the device pixel ratio for high-DPI displays
        let dpr = window.device_pixel_ratio();

        // Set the canvas width and height attributes to the container size times the pixel ratio
        let scaled_width = (container_width as f64 * dpr) as u32;
        let scaled_height = (container_height as f64 * dpr) as u32;

        // Set the actual canvas bitmap size
        canvas.set_width(scaled_width);
        canvas.set_height(scaled_height);

        // Set CSS size to maintain visual dimensions
        canvas.style().set_property("width", &format!("{}px", container_width))?;
        canvas.style().set_property("height", &format!("{}px", container_height))?;

        // Update AppState with the new dimensions
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.canvas_width = container_width as f64;
            state.canvas_height = container_height as f64;
        });

        // Update viewport and redraw if necessary
        APP_STATE.with(|state| {
            let state = state.borrow();

            if let Some(context) = &state.context {
                // Reset transform first to avoid compounding scales
                let _ = context.set_transform(1.0, 0.0, 0.0, 1.0, 0.0, 0.0);

                // Apply the device pixel ratio scaling
                let _ = context.scale(dpr, dpr);

                // If auto-fit is enabled, refit the nodes
                if state.auto_fit && !state.nodes.is_empty() {
                    // We need to drop the immutable borrow to get a mutable one
                    drop(state);

                    APP_STATE.with(|state| {
                        let mut state = state.borrow_mut();
                        state.fit_nodes_to_view();
                    });
                } else {
                    state.draw_nodes();
                }
            }
        });
    }

    Ok(())
}

fn setup_resize_handler(canvas: &HtmlCanvasElement) -> Result<(), JsValue> {
    let canvas_clone = canvas.clone();
    let resize_callback = Closure::wrap(Box::new(move || {
        let _ = resize_canvas(&canvas_clone);
    }) as Box<dyn FnMut()>);

    // Add window resize event listener
    web_sys::window()
        .expect("no global window exists")
        .add_event_listener_with_callback(
            "resize",
            resize_callback.as_ref().unchecked_ref(),
        )?;

    // Leak the closure to keep it alive for the lifetime of the application
    resize_callback.forget();

    Ok(())
}

fn setup_canvas_mouse_events(canvas: &HtmlCanvasElement) -> Result<(), JsValue> {
    // Get device pixel ratio once for all handlers
    let window = web_sys::window().expect("no global window exists");
    let _document = window.document().expect("should have a document");
    let _dpr = window.device_pixel_ratio();

    // Mouse down event
    let mousedown_handler = Closure::wrap(Box::new(move |event: MouseEvent| {
        // Get raw coordinates
        let x = event.offset_x() as f64;
        let y = event.offset_y() as f64;

        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();

            // Find which node was clicked on, accounting for viewport transformation
            if let Some((id, offset_x, offset_y)) = state.find_node_at_position(x, y) {
                // Store the selected node ID
                state.selected_node_id = Some(id.clone());

                // Check if it's a right click (context menu)
                if event.button() == 2 {
                    // Right click handling (future: show context menu)
                    return;
                }

                // Check if this node is an agent type
                let is_agent = if let Some(node) = state.nodes.get(&id) {
                    matches!(node.node_type, NodeType::AgentIdentity)
                } else {
                    false
                };

                if is_agent {
                    // Open modal for agent interaction
                    let window = web_sys::window().expect("no global window exists");
                    let document = window.document().expect("should have a document");

                    // Update modal title and load agent data
                    if let Some(node) = state.nodes.get(&id) {
                        // Set modal title
                        let modal_title = document.get_element_by_id("modal-title").unwrap();
                        modal_title.set_inner_html(&format!("Agent: {}", node.text));

                        // Load system instructions if available
                        if let Some(system_instructions) = &node.system_instructions {
                            let system_elem = document.get_element_by_id("system-instructions").unwrap();
                            let system_textarea = system_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
                            system_textarea.set_value(system_instructions);
                        }

                        // Load conversation history if available
                        if let Some(history) = &node.history {
                            let history_container = document.get_element_by_id("history-container").unwrap();

                            if history.is_empty() {
                                history_container.set_inner_html("<p>No history available.</p>");
                            } else {
                                // Clear existing history
                                history_container.set_inner_html("");

                                // Add each message to the history container
                                for message in history {
                                    let message_elem = document.create_element("div").unwrap();
                                    message_elem.set_class_name(&format!("history-item {}", message.role));

                                    let content = document.create_element("p").unwrap();
                                    content.set_inner_html(&message.content);

                                    message_elem.append_child(&content).unwrap();
                                    history_container.append_child(&message_elem).unwrap();
                                }
                            }
                        }
                    }

                    // Show the modal
                    let modal = document.get_element_by_id("agent-modal").unwrap();
                    modal.set_attribute("style", "display: block;").unwrap();
                } else {
                    // Regular node dragging (existing behavior)
                    state.dragging = Some(id);
                    state.drag_offset_x = offset_x;
                    state.drag_offset_y = offset_y;
                    state.canvas_dragging = false; // Ensure canvas dragging is off
                }
            } else {
                // Canvas dragging - clicked on empty area
                // If in Auto Layout Mode, automatically switch to Manual Layout Mode
                if state.auto_fit {
                    state.auto_fit = false;

                    // Also update the toggle in the UI
                    let window = web_sys::window().expect("no global window exists");
                    let document = window.document().expect("should have a document");
                    if let Some(toggle) = document.get_element_by_id("auto-fit-toggle") {
                        let checkbox = toggle.dyn_ref::<web_sys::HtmlInputElement>().unwrap();
                        checkbox.set_checked(false);
                    }
                }

                state.dragging = None;
                state.canvas_dragging = true;
                state.drag_start_x = x;
                state.drag_start_y = y;
                state.drag_last_x = x;
                state.drag_last_y = y;
            }
        });
    }) as Box<dyn FnMut(_)>);

    canvas.add_event_listener_with_callback(
        "mousedown",
        mousedown_handler.as_ref().unchecked_ref(),
    )?;
    mousedown_handler.forget();

    // Mouse move event
    let canvas_mousemove = canvas.clone();
    let mousemove_handler = Closure::wrap(Box::new(move |event: MouseEvent| {
        let x = event.offset_x() as f64;
        let y = event.offset_y() as f64;

        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();

            if let Some(id) = &state.dragging {
                // Node dragging (existing behavior)
                let id = id.clone();
                let drag_offset_x = state.drag_offset_x;
                let drag_offset_y = state.drag_offset_y;

                // Apply viewport transformation to the mouse coordinates
                let world_x = x / state.zoom_level + state.viewport_x;
                let world_y = y / state.zoom_level + state.viewport_y;

                state.update_node_position(&id, world_x - drag_offset_x, world_y - drag_offset_y);

                // After updating the node position, save state
                let _ = state.save_if_modified();
            } else if state.canvas_dragging {
                // Canvas dragging (new behavior)
                // Calculate how far the mouse has moved since starting the drag
                let dx = (state.drag_start_x - x) / state.zoom_level;
                let dy = (state.drag_start_y - y) / state.zoom_level;

                // Update the viewport (moving it in the direction of the drag)
                state.viewport_x += dx;
                state.viewport_y += dy;

                // Enforce boundaries to prevent panning too far
                state.enforce_viewport_boundaries();

                // Update the drag start position for next movement
                state.drag_start_x = x;
                state.drag_start_y = y;

                // Redraw with the new viewport
                state.draw_nodes();
            }
        });
    }) as Box<dyn FnMut(_)>);

    canvas_mousemove.add_event_listener_with_callback(
        "mousemove",
        mousemove_handler.as_ref().unchecked_ref(),
    )?;
    mousemove_handler.forget();

    // Mouse up event
    let canvas_mouseup = canvas.clone();
    let mouseup_handler = Closure::wrap(Box::new(move |_event: MouseEvent| {
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.dragging = None;
            state.canvas_dragging = false; // Clear canvas dragging state

            // Save state after completing the drag
            let _ = state.save_if_modified();
        });
    }) as Box<dyn FnMut(_)>);

    canvas_mouseup.add_event_listener_with_callback(
        "mouseup",
        mouseup_handler.as_ref().unchecked_ref(),
    )?;
    mouseup_handler.forget();

    // Add mouse wheel event for manual zooming when auto-fit is disabled
    let canvas_wheel = canvas.clone();
    // Create an additional clone for use inside the closure
    let canvas_wheel_inside = canvas_wheel.clone();

    let wheel_handler = Closure::wrap(Box::new(move |event: web_sys::WheelEvent| {
        // We won't prevent default since we want natural scrolling when not zooming
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();

            // Only allow manual zooming when auto-fit is disabled
            if !state.auto_fit {
                // Get canvas dimensions to use center as zoom point
                let canvas_width = canvas_wheel_inside.width() as f64;
                let canvas_height = canvas_wheel_inside.height() as f64;
                let window = web_sys::window().expect("no global window exists");
                let dpr = window.device_pixel_ratio();

                // Get center of canvas in screen coordinates
                let x = canvas_width / (2.0 * dpr);
                let y = canvas_height / (2.0 * dpr);

                // Convert to world coordinates
                let world_x = x / state.zoom_level + state.viewport_x;
                let world_y = y / state.zoom_level + state.viewport_y;

                // Get wheel delta
                let delta_y = event.delta_y();

                // Adjust zoom based on wheel direction
                let zoom_delta = if delta_y > 0.0 { 0.9 } else { 1.1 };
                state.zoom_level *= zoom_delta;

                // Limit zoom to reasonable values
                state.zoom_level = f64::max(0.1, f64::min(state.zoom_level, 5.0));

                // Adjust viewport to zoom toward/away from center
                state.viewport_x = world_x - x / state.zoom_level;
                state.viewport_y = world_y - y / state.zoom_level;

                state.draw_nodes();
            }
        });
    }) as Box<dyn FnMut(_)>);

    // Add wheel event listener with standard method
    canvas_wheel.add_event_listener_with_callback(
        "wheel",
        wheel_handler.as_ref().unchecked_ref(),
    )?;
    wheel_handler.forget();

    Ok(())
}

// Add this new function to handle auto-fit toggle
fn setup_auto_fit_button_handler(document: &Document) -> Result<(), JsValue> {
    let auto_fit_toggle = document.get_element_by_id("auto-fit-toggle").unwrap();

    let change_handler = Closure::wrap(Box::new(move |_event: Event| {
        // Use the toggle_auto_fit method to toggle the auto-fit state
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.toggle_auto_fit();
        });
    }) as Box<dyn FnMut(_)>);

    auto_fit_toggle.add_event_listener_with_callback(
        "change",
        change_handler.as_ref().unchecked_ref(),
    )?;

    // Keep the closure alive
    change_handler.forget();

    Ok(())
}

fn setup_center_view_handler(document: &Document) -> Result<(), JsValue> {
    let center_button = document.get_element_by_id("center-button").unwrap();

    let click_callback = Closure::wrap(Box::new(move |_event: Event| {
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.center_view();
        });
    }) as Box<dyn FnMut(_)>);

    center_button.add_event_listener_with_callback(
        "click",
        click_callback.as_ref().unchecked_ref(),
    )?;
    click_callback.forget();

    Ok(())
}

fn setup_model_select_handler(document: &Document) -> Result<(), JsValue> {
    let select_el = document.get_element_by_id("model-select").unwrap();

    let change_handler = Closure::wrap(Box::new(move |event: Event| {
        let select = event.target().unwrap().dyn_into::<web_sys::HtmlSelectElement>().unwrap();
        let model = select.value();

        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.selected_model = model;
            state.state_modified = true; // Mark as modified
            let _ = state.save_if_modified();
        });
    }) as Box<dyn FnMut(_)>);

    select_el.add_event_listener_with_callback(
        "change",
        change_handler.as_ref().unchecked_ref(),
    )?;
    change_handler.forget();

    Ok(())
}

// Add function for clear button handler
fn setup_clear_button_handler(document: &Document) -> Result<(), JsValue> {
    let clear_button = document.get_element_by_id("clear-button")
        .ok_or_else(|| JsValue::from_str("Clear button not found"))?;

    let click_callback = Closure::wrap(Box::new(move |_e: Event| {
        // Show confirmation dialog
        let window = web_sys::window().expect("no global window exists");
        let confirm = window.confirm_with_message("Are you sure you want to clear all nodes? This cannot be undone.")
            .unwrap_or(false);

        if confirm {
            // Clear state and storage
            APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                state.nodes.clear();
                state.latest_user_input_id = None;
                state.message_id_to_node_id.clear();
                state.viewport_x = 0.0;
                state.viewport_y = 0.0;
                state.zoom_level = 1.0;
                state.auto_fit = true;
                state.state_modified = true;

                state.draw_nodes();

                // Clear storage
                if let Err(e) = crate::storage::clear_storage() {
                    web_sys::console::error_1(&format!("Failed to clear storage: {:?}", e).into());
                }

                web_sys::console::log_1(&"Canvas cleared and storage reset".into());
            });
        }
    }) as Box<dyn FnMut(_)>);

    clear_button.add_event_listener_with_callback("click", click_callback.as_ref().unchecked_ref())?;
    click_callback.forget();

    Ok(())
}

pub fn create_base_ui(document: &Document) -> Result<(), JsValue> {
    // Create header
    let header = document.create_element("div")?;
    header.set_class_name("header");

    let title = document.create_element("h1")?;
    title.set_inner_html("AI Agent Platform");
    header.append_child(&title)?;

    // Create status bar
    let status_bar = document.create_element("div")?;
    status_bar.set_class_name("status-bar");

    let status = document.create_element("div")?;
    status.set_id("status");
    status.set_class_name("yellow"); // Initial state
    status.set_inner_html("Status: Connecting");

    let api_status = document.create_element("div")?;
    api_status.set_id("api-status");
    api_status.set_inner_html("API: Ready");

    status_bar.append_child(&status)?;
    status_bar.append_child(&api_status)?;

    // Get body element
    let body = document.body().ok_or(JsValue::from_str("No body found"))?;

    // Append header and status bar
    body.append_child(&header)?;
    body.append_child(&status_bar)?;

    Ok(())
}

// Create a modal dialog for agent interaction
fn create_agent_input_modal(document: &Document) -> Result<(), JsValue> {
    // Check if modal already exists (avoid duplicates)
    if document.get_element_by_id("agent-modal").is_some() {
        return Ok(());
    }

    // Create modal container
    let modal = document.create_element("div")?;
    modal.set_id("agent-modal");
    modal.set_class_name("modal");
    modal.set_attribute("style", "display: none;")?;

    // Create modal content
    let modal_content = document.create_element("div")?;
    modal_content.set_class_name("modal-content");

    // Create modal header
    let modal_header = document.create_element("div")?;
    modal_header.set_class_name("modal-header");

    let modal_title = document.create_element("h2")?;
    modal_title.set_id("modal-title");
    modal_title.set_inner_html("Agent Configuration");

    let close_button = document.create_element("span")?;
    close_button.set_class_name("close");
    close_button.set_inner_html("&times;");
    close_button.set_id("modal-close");

    modal_header.append_child(&modal_title)?;
    modal_header.append_child(&close_button)?;

    // Create tabs
    let tab_container = document.create_element("div")?;
    tab_container.set_class_name("tab-container");

    let system_tab = document.create_element("button")?;
    system_tab.set_class_name("tab-button active");
    system_tab.set_id("system-tab");
    system_tab.set_inner_html("System");

    let task_tab = document.create_element("button")?;
    task_tab.set_class_name("tab-button");
    task_tab.set_id("task-tab");
    task_tab.set_inner_html("Task");

    let history_tab = document.create_element("button")?;
    history_tab.set_class_name("tab-button");
    history_tab.set_id("history-tab");
    history_tab.set_inner_html("History");

    tab_container.append_child(&system_tab)?;
    tab_container.append_child(&task_tab)?;
    tab_container.append_child(&history_tab)?;

    // Create system instructions section
    let system_content = document.create_element("div")?;
    system_content.set_class_name("tab-content");
    system_content.set_id("system-content");

    let system_label = document.create_element("label")?;
    system_label.set_inner_html("System Instructions:");
    system_label.set_attribute("for", "system-instructions")?;

    let system_textarea = document.create_element("textarea")?;
    system_textarea.set_id("system-instructions");
    system_textarea.set_attribute("rows", "8")?;
    system_textarea.set_attribute("placeholder", "Enter system-level instructions for this agent...")?;

    system_content.append_child(&system_label)?;
    system_content.append_child(&system_textarea)?;

    // Create task input section
    let task_content = document.create_element("div")?;
    task_content.set_class_name("tab-content");
    task_content.set_id("task-content");
    task_content.set_attribute("style", "display: none;")?;

    let task_label = document.create_element("label")?;
    task_label.set_inner_html("Task Input:");
    task_label.set_attribute("for", "task-input")?;

    let task_textarea = document.create_element("textarea")?;
    task_textarea.set_id("task-input");
    task_textarea.set_attribute("rows", "6")?;
    task_textarea.set_attribute("placeholder", "Enter specific task or question for this agent...")?;

    task_content.append_child(&task_label)?;
    task_content.append_child(&task_textarea)?;

    // Create history section
    let history_content = document.create_element("div")?;
    history_content.set_class_name("tab-content");
    history_content.set_id("history-content");
    history_content.set_attribute("style", "display: none;")?;

    let history_container = document.create_element("div")?;
    history_container.set_id("history-container");
    history_container.set_inner_html("<p>No history available.</p>");

    history_content.append_child(&history_container)?;

    // Create buttons
    let button_container = document.create_element("div")?;
    button_container.set_class_name("modal-buttons");

    let save_button = document.create_element("button")?;
    save_button.set_id("save-agent");
    save_button.set_inner_html("Save");

    let send_button = document.create_element("button")?;
    send_button.set_id("send-to-agent");
    send_button.set_inner_html("Send");

    button_container.append_child(&save_button)?;
    button_container.append_child(&send_button)?;

    // Assemble modal
    modal_content.append_child(&modal_header)?;
    modal_content.append_child(&tab_container)?;
    modal_content.append_child(&system_content)?;
    modal_content.append_child(&task_content)?;
    modal_content.append_child(&history_content)?;
    modal_content.append_child(&button_container)?;

    modal.append_child(&modal_content)?;

    // Add to document
    let body = document.body().expect("document should have a body");
    body.append_child(&modal)?;

    // Set up event handlers for the modal
    setup_modal_handlers(document)?;

    Ok(())
}

// Add event handlers to the modal
fn setup_modal_handlers(document: &Document) -> Result<(), JsValue> {
    // Close button
    let close_button = document.get_element_by_id("modal-close").unwrap();
    let close_handler = Closure::wrap(Box::new(move |_event: Event| {
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");
        let modal = document.get_element_by_id("agent-modal").unwrap();
        modal.set_attribute("style", "display: none;").unwrap();

        // Auto-save any pending changes before closing
        let system_elem = document.get_element_by_id("system-instructions").unwrap();
        let system_textarea = system_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
        let system_instructions = system_textarea.value();

        save_system_instructions(system_instructions);
    }) as Box<dyn FnMut(_)>);

    close_button.add_event_listener_with_callback(
        "click",
        close_handler.as_ref().unchecked_ref(),
    )?;
    close_handler.forget();

    // Add input listener to system instructions to auto-save after a delay
    let system_textarea = document.get_element_by_id("system-instructions").unwrap();

    // Create a new timeout ID reference
    let timeout_id_ref = Rc::new(RefCell::new(None::<i32>));

    let input_timeout_id = timeout_id_ref.clone();
    let input_handler = Closure::wrap(Box::new(move |_event: Event| {
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        // Get the current value from the textarea
        let system_elem = document.get_element_by_id("system-instructions").unwrap();
        let system_textarea = system_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
        let system_instructions = system_textarea.value();

        // Clone values for the timeout closure
        let system_instructions_clone = system_instructions.clone();

        // Clear previous timeout if it exists
        if let Some(id) = *input_timeout_id.borrow() {
            window.clear_timeout_with_handle(id);
        }

        // Set new timeout (500ms debounce)
        let timeout_closure = Closure::once_into_js(move || {
            save_system_instructions(system_instructions_clone);
        });

        let new_timeout_id = window
            .set_timeout_with_callback_and_timeout_and_arguments(
                timeout_closure.as_ref().unchecked_ref(),
                500,  // 500ms debounce
                &js_sys::Array::new(),
            )
            .expect("Failed to set timeout");

        *input_timeout_id.borrow_mut() = Some(new_timeout_id);
    }) as Box<dyn FnMut(_)>);

    system_textarea.add_event_listener_with_callback(
        "input",
        input_handler.as_ref().unchecked_ref(),
    )?;
    input_handler.forget();

    // Tab switching
    let tab_handler = Closure::wrap(Box::new(move |event: Event| {
        let target = event.target().unwrap();
        let button = target.dyn_ref::<web_sys::HtmlElement>().unwrap();
        let tab_id = button.id();

        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        // Hide all tab contents
        let contents = vec!["system-content", "task-content", "history-content"];
        for content_id in contents.iter() {
            let content = document.get_element_by_id(content_id).unwrap();
            content.set_attribute("style", "display: none;").unwrap();
        }

        // Deactivate all tabs
        let tabs = vec!["system-tab", "task-tab", "history-tab"];
        for tab in tabs.iter() {
            let tab_el = document.get_element_by_id(tab).unwrap();
            tab_el.set_class_name("tab-button");
        }

        // Activate selected tab
        button.set_class_name("tab-button active");

        // Show selected content
        let content_id = match tab_id.as_str() {
            "system-tab" => "system-content",
            "task-tab" => "task-content",
            "history-tab" => "history-content",
            _ => "system-content"
        };

        let content = document.get_element_by_id(content_id).unwrap();
        content.set_attribute("style", "display: block;").unwrap();
    }) as Box<dyn FnMut(_)>);

    let tabs = vec!["system-tab", "task-tab", "history-tab"];
    for tab_id in tabs.iter() {
        let tab = document.get_element_by_id(tab_id).unwrap();
        tab.add_event_listener_with_callback(
            "click",
            tab_handler.as_ref().unchecked_ref(),
        )?;
    }

    tab_handler.forget();

    // Save button handler
    let save_button = document.get_element_by_id("save-agent").unwrap();
    let save_handler = Closure::wrap(Box::new(move |_event: Event| {
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        // Get values from inputs
        let system_elem = document.get_element_by_id("system-instructions").unwrap();
        let system_textarea = system_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
        let system_instructions = system_textarea.value();

        // Save system instructions
        save_system_instructions(system_instructions);

        // Close modal
        let modal = document.get_element_by_id("agent-modal").unwrap();
        modal.set_attribute("style", "display: none;").unwrap();
    }) as Box<dyn FnMut(_)>);

    save_button.add_event_listener_with_callback(
        "click",
        save_handler.as_ref().unchecked_ref(),
    )?;
    save_handler.forget();

    // Send button handler
    let send_button = document.get_element_by_id("send-to-agent").unwrap();
    let send_handler = Closure::wrap(Box::new(move |_event: Event| {
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        // Get values from inputs
        let task_elem = document.get_element_by_id("task-input").unwrap();
        let task_textarea = task_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
        let task_input = task_textarea.value();

        // Get current system instructions and save them before sending
        let system_elem = document.get_element_by_id("system-instructions").unwrap();
        let system_textarea = system_elem.dyn_ref::<web_sys::HtmlTextAreaElement>().unwrap();
        let system_instructions = system_textarea.value();

        // Save system instructions first
        save_system_instructions(system_instructions);

        if !task_input.is_empty() {
            // Send to agent
            send_task_to_agent(task_input);

            // Clear task input
            task_textarea.set_value("");

            // Close modal after send
            let modal = document.get_element_by_id("agent-modal").unwrap();
            modal.set_attribute("style", "display: none;").unwrap();
        }
    }) as Box<dyn FnMut(_)>);

    send_button.add_event_listener_with_callback(
        "click",
        send_handler.as_ref().unchecked_ref(),
    )?;
    send_handler.forget();

    Ok(())
}

// Function to send a task to an agent and handle the response
fn send_task_to_agent(task: String) {
    web_sys::console::log_1(&JsValue::from_str("Starting send_task_to_agent"));

    // Generate a message ID
    let message_id = Math::random().to_string();
    web_sys::console::log_1(&format!("Generated message_id: {}", message_id).into());

    // Get current timestamp
    let timestamp = Date::now() as u64;

    // Log before accessing app state
    web_sys::console::log_1(&JsValue::from_str("About to access APP_STATE"));

    // Add the task to agent's history and create a response node
    APP_STATE.with(|state| {
        let mut state = state.borrow_mut();
        web_sys::console::log_1(&JsValue::from_str("Got state borrow_mut"));

        if let Some(agent_id) = state.selected_node_id.clone() {
            web_sys::console::log_1(&format!("Found selected_node_id: {}", agent_id).into());

            // Log all nodes in state for debugging
            web_sys::console::log_1(&JsValue::from_str("Current nodes in state:"));
            for (id, node) in &state.nodes {
                web_sys::console::log_1(&format!("Node {}: type={:?}, text='{}', status={:?}",
                    id, node.node_type, node.text, node.status).into());
            }

            if let Some(agent) = state.nodes.get_mut(&agent_id) {
                web_sys::console::log_1(&format!("Found agent node with text: '{}', type: {:?}, status: {:?}",
                    agent.text, agent.node_type, agent.status).into());

                // Verify this is actually an agent node
                if !matches!(agent.node_type, NodeType::AgentIdentity) {
                    web_sys::console::error_1(&format!("Selected node is not an agent! Type: {:?}", agent.node_type).into());
                    return;
                }

                // Create a new user message
                let user_message = Message {
                    role: "user".to_string(),
                    content: task.clone(),
                    timestamp,
                };

                // Log before updating agent history
                web_sys::console::log_1(&JsValue::from_str("About to update agent history"));

                // Update the agent's history
                if let Some(history) = &mut agent.history {
                    web_sys::console::log_1(&format!("Current history length: {}", history.len()).into());
                    history.push(user_message);
                    web_sys::console::log_1(&format!("New history length: {}", history.len()).into());
                } else {
                    web_sys::console::error_1(&JsValue::from_str("History is None! Initializing new history."));
                    agent.history = Some(vec![user_message]);
                }

                // Set agent status to "processing"
                agent.status = Some("processing".to_string());
                web_sys::console::log_1(&JsValue::from_str("Set agent status to 'processing'"));

                // Log before creating response node
                web_sys::console::log_1(&JsValue::from_str("About to create response node"));

                // Store agent_id before releasing the borrow
                let agent_id_clone = agent_id.clone();

                // End the mutable borrow of agent
                let _ = agent;

                // Add a response node connected to the agent
                let response_node_id = add_response_node_for_agent(&mut state, &agent_id_clone, "...".to_string());

                // Check if response node creation failed
                if response_node_id.is_empty() {
                    web_sys::console::error_1(&JsValue::from_str("Failed to create response node"));
                    // Reset agent status back to idle since we failed
                    if let Some(agent) = state.nodes.get_mut(&agent_id) {
                        agent.status = Some("idle".to_string());
                    }
                    state.draw_nodes();
                    return;
                }

                web_sys::console::log_1(&format!("Created response node with ID: {}", response_node_id).into());

                // Save the message_id -> response_node mapping
                state.message_id_to_node_id.insert(message_id.clone(), response_node_id);
                web_sys::console::log_1(&JsValue::from_str("Saved message_id -> response_node mapping"));

                // Update the display
                web_sys::console::log_1(&JsValue::from_str("About to draw nodes"));
                state.draw_nodes();
                web_sys::console::log_1(&JsValue::from_str("Drew nodes"));

                // Save state
                web_sys::console::log_1(&JsValue::from_str("About to save state"));
                if let Err(e) = state.save_if_modified() {
                    web_sys::console::error_1(&format!("Failed to save state: {:?}", e).into());
                } else {
                    web_sys::console::log_1(&JsValue::from_str("State saved successfully"));
                }
            } else {
                web_sys::console::error_1(&JsValue::from_str("Could not find agent node!"));
            }
        } else {
            web_sys::console::error_1(&JsValue::from_str("No selected_node_id!"));
        }
    });

    // Log before preparing request parameters
    web_sys::console::log_1(&JsValue::from_str("About to prepare request parameters"));

    // Prepare request parameters
    let system_instructions = APP_STATE.with(|state| {
        let state = state.borrow();
        if let Some(agent_id) = &state.selected_node_id {
            if let Some(agent) = state.nodes.get(agent_id) {
                let instructions = agent.system_instructions.clone().unwrap_or_default();
                web_sys::console::log_1(&format!("Got system instructions: {}", instructions).into());
                instructions
            } else {
                web_sys::console::error_1(&JsValue::from_str("Could not find agent node for system instructions!"));
                String::new()
            }
        } else {
            web_sys::console::error_1(&JsValue::from_str("No selected_node_id for system instructions!"));
            String::new()
        }
    });

    let selected_model = APP_STATE.with(|state| {
        let state = state.borrow();
        let model = state.selected_model.clone();
        web_sys::console::log_1(&format!("Selected model: {}", model).into());
        model
    });

    // Log before sending to backend
    web_sys::console::log_1(&JsValue::from_str("About to send to backend"));

    // Send to backend
    send_to_backend(&task, &system_instructions, &selected_model, message_id);
    web_sys::console::log_1(&JsValue::from_str("Sent to backend"));
}

// Helper function to create a response node attached to an agent
// Takes AppState as a parameter instead of borrowing it internally to prevent double borrow
fn add_response_node_for_agent(state: &mut AppState, agent_id: &str, initial_text: String) -> String {
    web_sys::console::log_1(&format!("Starting add_response_node_for_agent for agent: {}", agent_id).into());

    // Find the agent node's position
    let (x, y, height) = if let Some(agent) = state.nodes.get(agent_id) {
        // Verify this is actually an agent node
        if !matches!(agent.node_type, NodeType::AgentIdentity) {
            web_sys::console::error_1(&format!("Node {} is not an agent! Type: {:?}", agent_id, agent.node_type).into());
            state.draw_nodes(); // Ensure we redraw in error state
            return String::new(); // Return empty string to signal error
        }

        web_sys::console::log_1(&format!("Found agent node at position: ({}, {}), height: {}", agent.x, agent.y, agent.height).into());
        (agent.x, agent.y, agent.height)
    } else {
        web_sys::console::error_1(&format!("Agent node not found: {}", agent_id).into());
        state.draw_nodes(); // Ensure we redraw in error state
        return String::new(); // Return empty string to signal error
    };

    // Calculate position for response node (below the agent)
    // Add some padding between the agent and response node
    let response_y = y + height + 50.0;
    let response_x = x;

    // Create the response node
    let response_node_id = state.add_node(
        initial_text,
        response_x,
        response_y,
        NodeType::AgentResponse
    );

    // Set the parent_id of the response node to link it to the agent
    if let Some(response_node) = state.nodes.get_mut(&response_node_id) {
        response_node.parent_id = Some(agent_id.to_string());
    }

    // Redraw with the new node
    state.draw_nodes();

    response_node_id
}

// Function to send the task to the backend
fn send_to_backend(task: &str, _system_instructions: &str, _model: &str, message_id: String) {
    // Use the network module's implementation to send the request
    crate::network::send_text_to_backend(task, message_id);

    // Note: system_instructions and model are already set in the APP_STATE and
    // handled by the network module's implementation
}

// Update response node with AI's response
fn update_response_node(message_id: &str, response_text: &str) {
    APP_STATE.with(|state| {
        let mut state = state.borrow_mut();

        // Find the response node ID
        if let Some(node_id) = state.message_id_to_node_id.get(message_id).cloned() {
            // Update the response node text
            if let Some(node) = state.nodes.get_mut(&node_id) {
                node.text = response_text.to_string();

                // Resize the node to fit the content
                state.resize_node_for_content(&node_id);
            }

            // Also update the parent agent's history and status
            // First get the parent ID
            let parent_id = if let Some(response_node) = state.nodes.get(&node_id) {
                response_node.parent_id.clone()
            } else {
                None
            };

            // Then update the parent if we have a valid ID
            if let Some(parent_id) = parent_id {
                if let Some(agent) = state.nodes.get_mut(&parent_id) {
                    // Create an assistant message
                    let assistant_message = Message {
                        role: "assistant".to_string(),
                        content: response_text.to_string(),
                        timestamp: Date::now() as u64,
                    };

                    // Add to history
                    if let Some(history) = &mut agent.history {
                        history.push(assistant_message);
                    }

                    // Set status back to idle
                    agent.status = Some("idle".to_string());
                }
            }

            // Redraw and save
            state.draw_nodes();

            if let Err(e) = state.save_if_modified() {
                web_sys::console::error_1(&format!("Failed to save state: {:?}", e).into());
            }
        }
    });
}

// Rename setup_animation_loop to internal_setup_animation_loop
pub fn internal_setup_animation_loop() {
    // Animation disabled to prevent RefCell borrow issues
    // The only thing this powered was the processing status visual effect
}

// Helper function to save system instructions
fn save_system_instructions(system_instructions: String) {
    APP_STATE.with(|state| {
        let mut state = state.borrow_mut();
        if let Some(node_id) = state.selected_node_id.clone() {
            if let Some(node) = state.nodes.get_mut(&node_id) {
                // Update node text based on system instructions
                node.text = if system_instructions.is_empty() {
                    "Agent".to_string()
                } else {
                    // Use first line or first few characters as name
                    let name = system_instructions.lines().next()
                        .unwrap_or("Agent")
                        .chars()
                        .take(20)
                        .collect::<String>();
                    if name.len() >= 20 { name + "..." } else { name }
                };

                // Store system instructions in node metadata
                node.system_instructions = Some(system_instructions);

                state.draw_nodes();
                if let Err(e) = state.save_if_modified() {
                    web_sys::console::error_1(&format!("Failed to save state: {:?}", e).into());
                }
            }
        }
    });
}
