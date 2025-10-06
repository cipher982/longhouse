use crate::messages::Command;
use crate::models::NodeType;
use crate::state::dispatch_global_message;
use crate::state::{AppState, APP_STATE};
use serde_json::Value;
use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{AddEventListenerOptions, Document, DragEvent, HtmlCanvasElement, MouseEvent};

// New type to represent either a mutable or immutable reference to AppState
pub enum AppStateRef<'a> {
    #[allow(dead_code)]
    Mutable(&'a mut AppState),
    #[allow(dead_code)] // reserved for future readonly references when diff algorithm lands
    Immutable(&'a AppState),
    None,
}

pub fn setup_canvas(document: &Document) -> Result<(), JsValue> {
    let canvas = document
        .get_element_by_id("node-canvas")
        .unwrap()
        .dyn_into::<HtmlCanvasElement>()?;

    // Set canvas dimensions to match container
    resize_canvas(&canvas, AppStateRef::None)?;

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

    // Set up drag and drop events for the canvas
    setup_canvas_drag_drop(&canvas)?;

    // Set up resize handler
    setup_resize_handler(&canvas)?;

    // Setup animation loop for refreshing the canvas
    crate::ui::setup_animation_loop();

    // Initialize the node palette
    crate::components::node_palette::init_node_palette(document)?;

    Ok(())
}

pub fn resize_canvas(
    canvas: &HtmlCanvasElement,
    mut app_state_ref: AppStateRef,
) -> Result<(), JsValue> {
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
        canvas
            .style()
            .set_property("width", &format!("{}px", container_width))?;
        canvas
            .style()
            .set_property("height", &format!("{}px", container_height))?;

        // Update dimensions and check auto_fit setting based on the reference type
        let (auto_fit, has_nodes, context) = match app_state_ref {
            AppStateRef::Mutable(ref mut state) => {
                // Can update state
                state.canvas_width = container_width as f64;
                state.canvas_height = container_height as f64;
                (
                    state.auto_fit,
                    !state.workflow_nodes.is_empty(),
                    state.context.as_ref().cloned(),
                )
            }
            AppStateRef::Immutable(ref state) => {
                // Read only - these dimensions won't be saved in state
                // but that's okay for rendering purposes
                (
                    state.auto_fit,
                    !state.workflow_nodes.is_empty(),
                    state.context.as_ref().cloned(),
                )
            }
            AppStateRef::None => APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                state.canvas_width = container_width as f64;
                state.canvas_height = container_height as f64;
                (
                    state.auto_fit,
                    !state.workflow_nodes.is_empty(),
                    state.context.as_ref().cloned(),
                )
            }),
        };

        // Apply the canvas transformations if we have a context
        if let Some(context) = context {
            // Reset transform first to avoid compounding scales
            let _ = context.set_transform(1.0, 0.0, 0.0, 1.0, 0.0, 0.0);

            // Apply the device pixel ratio scaling
            let _ = context.scale(dpr, dpr);
        }

        // Handle auto-fit if needed based on the reference type
        if auto_fit && has_nodes {
            match app_state_ref {
                AppStateRef::Mutable(ref mut state) => {
                    state.fit_nodes_to_view();
                }
                AppStateRef::Immutable(_state) => {
                    // Read-only ref – obtain a fresh mutable borrow to flag
                    // the upcoming redraw without directly painting here.
                    crate::state::dispatch_global_message(
                        crate::messages::Message::MarkCanvasDirty,
                    );
                }
                AppStateRef::None => APP_STATE.with(|state| {
                    let mut state = state.borrow_mut();
                    state.fit_nodes_to_view();
                }),
            }
        } else {
            // Just redraw without auto-fit
            match app_state_ref {
                AppStateRef::Mutable(ref mut state) => {
                    let _ = state.dispatch(crate::messages::Message::MarkCanvasDirty);
                }
                AppStateRef::Immutable(_state) => {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::MarkCanvasDirty,
                    );
                }
                AppStateRef::None => APP_STATE.with(|state| {
                    let mut state = state.borrow_mut();
                    let _ = state.dispatch(crate::messages::Message::MarkCanvasDirty);
                }),
            }
        }
    }

    Ok(())
}

fn setup_resize_handler(canvas: &HtmlCanvasElement) -> Result<(), JsValue> {
    let _window = web_sys::window().expect("no global window exists");

    // Set up resize handler
    let canvas_clone = canvas.clone();
    let resize_callback = Closure::wrap(Box::new(move || {
        let _ = resize_canvas(&canvas_clone, AppStateRef::None);
    }) as Box<dyn FnMut()>);

    // Add window resize event listener
    web_sys::window()
        .expect("no global window exists")
        .add_event_listener_with_callback("resize", resize_callback.as_ref().unchecked_ref())?;

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

        // First check if clicking on a connection handle
        let handle_clicked = {
            APP_STATE.with(|state| {
                let state = state.borrow();
                // Check all nodes for handle hits
                for (node_id, _) in &state.workflow_nodes {
                    if let Some(handle_position) = state.get_handle_at_point(node_id, x, y) {
                        return Some((node_id.clone(), handle_position));
                    }
                }
                None
            })
        };

        if let Some((node_id, handle_position)) = handle_clicked {
            // Start connection drag from this handle
            dispatch_global_message(crate::messages::Message::StartConnectionDrag {
                node_id,
                handle_position,
                start_x: x,
                start_y: y,
            });
            return;
        }

        // If not a handle click, check for regular node clicks
        let clicked_result = {
            APP_STATE.with(|state| {
                let state = state.borrow();
                state.find_node_at_position(x, y)
            })
        };

        // Based on the result, dispatch the appropriate message
        if let Some((node_id, offset_x, offset_y)) = clicked_result {
            APP_STATE.with(|state| {
                state.borrow_mut().clicked_node_id = Some(node_id.clone());
            });

            // Check if this node is an agent type
            let is_agent = APP_STATE.with(|state| {
                let state = state.borrow();
                if let Some(node) = state.workflow_nodes.get(&node_id) {
                    matches!(node.get_semantic_type(), NodeType::AgentIdentity)
                } else {
                    false
                }
            });

            // Check if right click
            if event.button() == 2 {
                // Right click handling (future: show context menu)
                return;
            }

            // Check if auto-fit is enabled
            let auto_fit_enabled = APP_STATE.with(|state| {
                let state = state.borrow();
                state.auto_fit
            });

            // If auto-fit is enabled, toggle it off
            if auto_fit_enabled {
                let commands = APP_STATE.with(|state| {
                    let mut state = state.borrow_mut();
                    state.dispatch(crate::messages::Message::ToggleAutoFit)
                });

                // Execute commands
                for cmd in commands {
                    match cmd {
                        Command::SendMessage(msg) => dispatch_global_message(msg),
                        Command::NoOp => {}
                        _ => {} // Ignore other commands in this context
                    }
                }

                // Update the auto-fit toggle button
                let auto_fit_toggle = window
                    .document()
                    .expect("should have a document")
                    .get_element_by_id("auto-fit-toggle");

                if let Some(toggle) = auto_fit_toggle {
                    // Cast to HTMLInputElement to set checked property directly
                    if let Some(toggle_input) = toggle.dyn_ref::<web_sys::HtmlInputElement>() {
                        toggle_input.set_checked(false);
                    }
                }
            }

            // Dispatch StartDragging message
            let commands = APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                state.dispatch(crate::messages::Message::StartDragging {
                    node_id: node_id.clone(),
                    offset_x,
                    offset_y,
                    start_x: x,
                    start_y: y,
                    is_agent,
                })
            });

            // Execute commands
            for cmd in commands {
                match cmd {
                    Command::SendMessage(msg) => dispatch_global_message(msg),
                    Command::NoOp => {}
                    _ => {} // Ignore other commands in this context
                }
            }
        } else {
            // Nothing was clicked - no canvas dragging (disabled for simpler interactions)
        }
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

        // Update mouse position for hover effects
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            state.update_mouse_position(x, y);
        });

        // Check if we're dragging a node or connection (canvas dragging disabled)
        let dragging_type = APP_STATE.with(|state| {
            let state = state.borrow();
            if state.connection_drag_active {
                "connection"
            } else if state.dragging.is_some() {
                "node"
            } else {
                "none"
            }
        });

        match dragging_type {
            "connection" => {
                // Update connection drag position
                dispatch_global_message(crate::messages::Message::UpdateConnectionDrag {
                    current_x: x,
                    current_y: y,
                });
            }
            "node" => {
                // Get the node id and offset from state (simplified - no viewport transformation needed)
                let (node_id, drag_offset_x, drag_offset_y) = APP_STATE.with(|state| {
                    let state = state.borrow();
                    let node_id = state.dragging.clone().unwrap();
                    (node_id, state.drag_offset_x, state.drag_offset_y)
                });

                // No viewport transformation needed since viewport is fixed at (0,0) and zoom is 1.0

                // Dispatch UpdateNodePosition message
                let commands = APP_STATE.with(|state| {
                    let mut state = state.borrow_mut();
                    state.dispatch(crate::messages::Message::UpdateNodePosition {
                        node_id,
                        x: x - drag_offset_x,
                        y: y - drag_offset_y,
                    })
                });

                // Execute commands
                for cmd in commands {
                    match cmd {
                        Command::SendMessage(msg) => dispatch_global_message(msg),
                        Command::NoOp => {}
                        _ => {} // Ignore other commands in this context
                    }
                }
            }
            "canvas" => {
                // Canvas dragging disabled for simpler interactions
            }
            _ => {
                // Not dragging anything
            }
        }
    }) as Box<dyn FnMut(_)>);

    canvas_mousemove.add_event_listener_with_callback(
        "mousemove",
        mousemove_handler.as_ref().unchecked_ref(),
    )?;
    mousemove_handler.forget();

    // Mouse up event
    let canvas_mouseup = canvas.clone();
    let mouseup_handler = Closure::wrap(Box::new(move |event: MouseEvent| {
        let x = event.offset_x() as f64;
        let y = event.offset_y() as f64;

        let (clicked_node_id, drag_start_x, drag_start_y) = APP_STATE.with(|state| {
            let state = state.borrow();
            (
                state.clicked_node_id.clone(),
                state.drag_start_x,
                state.drag_start_y,
            )
        });

        if let Some(node_id) = clicked_node_id {
            let dx = x - drag_start_x;
            let dy = y - drag_start_y;
            let distance_squared = dx * dx + dy * dy;
            if distance_squared < 25.0 {
                // It's a click
                let node_type = APP_STATE.with(|state| {
                    state
                        .borrow()
                        .workflow_nodes
                        .get(&node_id)
                        .map(|n| n.node_type.clone())
                });

                if let Some(_node_type) = node_type {
                    // Get the semantic type from the node
                    let semantic_type = APP_STATE.with(|state| {
                        let state = state.borrow();
                        state
                            .workflow_nodes
                            .get(&node_id)
                            .map(|n| n.get_semantic_type())
                    });

                    if let Some(semantic_type) = semantic_type {
                        match semantic_type {
                            NodeType::AgentIdentity => {
                                dispatch_global_message(
                                    crate::messages::Message::CanvasNodeClicked { node_id },
                                );
                            }
                            NodeType::Tool { .. } => {
                                dispatch_global_message(
                                    crate::messages::Message::ShowToolConfigModal { node_id },
                                );
                            }
                            NodeType::Trigger { .. } => {
                                dispatch_global_message(
                                    crate::messages::Message::ShowTriggerConfigModal { node_id },
                                );
                            }
                            _ => {}
                        }
                    }
                }
            }
        }

        // Stop any dragging operation
        if APP_STATE.with(|state| state.borrow().connection_drag_active) {
            dispatch_global_message(crate::messages::Message::EndConnectionDrag {
                end_x: x,
                end_y: y,
            });
        } else if APP_STATE.with(|state| state.borrow().dragging.is_some()) {
            dispatch_global_message(crate::messages::Message::StopDragging);
        } else if APP_STATE.with(|state| state.borrow().canvas_dragging) {
            // Canvas dragging disabled - just clear the state
            dispatch_global_message(crate::messages::Message::StopCanvasDrag);
        }

        // Reset clicked_node_id
        APP_STATE.with(|state| {
            state.borrow_mut().clicked_node_id = None;
        });

        // Refresh the dashboard
        let _ = refresh_dashboard_after_change();
    }) as Box<dyn FnMut(_)>);

    canvas_mouseup
        .add_event_listener_with_callback("mouseup", mouseup_handler.as_ref().unchecked_ref())?;
    mouseup_handler.forget();

    // Add mouse wheel event for manual zooming when auto-fit is disabled
    let canvas_wheel = canvas.clone();

    let wheel_handler = Closure::wrap(Box::new(move |event: web_sys::WheelEvent| {
        // ------------------------------------------------------------------
        // Interactive zoom is disabled.  Prevent the browser’s default page
        // zoom (two-finger pinch on trackpads) so the canvas stays put.
        // ------------------------------------------------------------------

        // Only cancel the event if the gesture looks like a pinch-zoom:
        // on macOS this arrives as a wheel event with `ctrlKey` true.
        if event.ctrl_key() {
            event.prevent_default();
        }

        // Early-exit: we no longer perform any canvas zoom.
        return;

        // (Code below kept for future re-enable.)
        /*
        let auto_fit_enabled = APP_STATE.with(|state| {
            let state = state.borrow();
            state.auto_fit
        });
        if !auto_fit_enabled {
            // Rest of the zoom handling code
            let (canvas_width, canvas_height, zoom_level, viewport_x, viewport_y) = APP_STATE.with(|state| {
                let state = state.borrow();
                (
                    canvas_wheel_inside.width() as f64,
                    canvas_wheel_inside.height() as f64,
                    state.zoom_level,
                    state.viewport_x,
                    state.viewport_y
                )
            });

            let window = web_sys::window().expect("no global window exists");
            let dpr = window.device_pixel_ratio();

            // Get center of canvas in screen coordinates
            let x = canvas_width / (2.0 * dpr);
            let y = canvas_height / (2.0 * dpr);

            // Convert to world coordinates
            let world_x = x / zoom_level + viewport_x;
            let world_y = y / zoom_level + viewport_y;

            // Get wheel delta
            let delta_y = event.delta_y();

            // Calculate new zoom level
            let zoom_delta = if delta_y > 0.0 { 0.9 } else { 1.1 };
            let new_zoom = zoom_level * zoom_delta;

            // Clamp zoom to ±50 % around default
            use crate::state::{MIN_ZOOM, MAX_ZOOM};
            let new_zoom = new_zoom.clamp(MIN_ZOOM, MAX_ZOOM);

            // Calculate new viewport based on the zoom
            let new_viewport_x = world_x - x / new_zoom;
            let new_viewport_y = world_y - y / new_zoom;

            // Dispatch ZoomCanvas message
            let commands = APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                state.dispatch(crate::messages::Message::ZoomCanvas {
                    new_zoom,
                    viewport_x: new_viewport_x,
                    viewport_y: new_viewport_y,
                })
            });

            // Execute commands
            for cmd in commands {
                match cmd {
                    Command::SendMessage(msg) => dispatch_global_message(msg),
                    Command::NoOp => {},
                    _ => {},  // Ignore other commands in this context
                }
            }
        }
        */
    }) as Box<dyn FnMut(_)>);

    // Create options with passive: true
    let options = AddEventListenerOptions::new();
    options.set_passive(false); // Allow preventDefault inside wheel handler

    // Add wheel event listener with passive option
    canvas_wheel.add_event_listener_with_callback_and_add_event_listener_options(
        "wheel",
        wheel_handler.as_ref().unchecked_ref(),
        &options,
    )?;
    wheel_handler.forget();

    Ok(())
}

// Helper function to refresh dashboard after node modifications
fn refresh_dashboard_after_change() -> Result<(), JsValue> {
    // First save the state in its own borrow scope
    {
        APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            let _ = state.save_if_modified();
        });
    }

    // Quick-win: avoid the heavyweight unmount/mount cycle that
    // refresh_ui_after_state_change() performs.  A simple save is
    // enough after drag-end; the RAF render loop (or the direct
    // draw_nodes call in the drag handler) already repaints.

    Ok(())
}

// Add a new function to set up drag and drop events
fn setup_canvas_drag_drop(canvas: &HtmlCanvasElement) -> Result<(), JsValue> {
    let canvas_dragover = canvas.clone();
    let dragover_handler = Closure::wrap(Box::new(move |event: DragEvent| {
        // Prevent default to allow drop
        event.prevent_default();

        // Set the drop effect to "copy" for visual feedback
        if let Some(dt) = event.data_transfer() {
            dt.set_drop_effect("copy");
        }

        // Add a class to the canvas container for visual feedback
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        if let Some(container) = document.get_element_by_id("canvas-container") {
            if let Some(element) = container.dyn_ref::<web_sys::HtmlElement>() {
                element
                    .class_list()
                    .add_1("canvas-drop-target")
                    .unwrap_or_default();
            }
        }
    }) as Box<dyn FnMut(_)>);

    canvas_dragover
        .add_event_listener_with_callback("dragover", dragover_handler.as_ref().unchecked_ref())?;
    dragover_handler.forget();

    let canvas_dragleave = canvas.clone();
    let dragleave_handler = Closure::wrap(Box::new(move |_event: DragEvent| {
        // Remove the highlight class when drag leaves
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        if let Some(container) = document.get_element_by_id("canvas-container") {
            if let Some(element) = container.dyn_ref::<web_sys::HtmlElement>() {
                element
                    .class_list()
                    .remove_1("canvas-drop-target")
                    .unwrap_or_default();
            }
        }
    }) as Box<dyn FnMut(_)>);

    canvas_dragleave.add_event_listener_with_callback(
        "dragleave",
        dragleave_handler.as_ref().unchecked_ref(),
    )?;
    dragleave_handler.forget();

    let canvas_drop = canvas.clone();
    let drop_handler = Closure::wrap(Box::new(move |event: DragEvent| {
        // Prevent default browser behavior
        event.prevent_default();

        // Get mouse position relative to canvas
        let x = event.offset_x() as f64;
        let y = event.offset_y() as f64;

        // Log drop event for debugging
        crate::debug_log!("Drop event at position: x={}, y={}", x, y);

        // Get the data transfer object
        if let Some(dt) = event.data_transfer() {
            // Try to get tool/node palette data first
            if let Ok(node_json) = dt.get_data("application/json") {
                if let Ok(palette_node) =
                    serde_json::from_str::<crate::components::node_palette::PaletteNode>(&node_json)
                {
                    // No viewport transformation needed since viewport is fixed at (0,0) and zoom is 1.0
                    APP_STATE.with(|state| {
                        let mut state = state.borrow_mut();
                        crate::components::node_palette::create_node_from_palette(
                            &mut state,
                            &palette_node,
                            x,
                            y,
                        );
                    });
                    return;
                }
            }
            // Fallback: Get the agent data that was set during dragstart
            if let Ok(data_str) = dt.get_data("text/plain") {
                crate::debug_log!("Dropped agent data: {}", data_str);

                // Parse the JSON data
                if let Ok(data) = serde_json::from_str::<Value>(&data_str) {
                    // Extract agent_id and name
                    let agent_id = data["agent_id"].as_u64().map(|id| id as u32);
                    let name = data["name"].as_str().unwrap_or("Unknown Agent").to_string();

                    crate::debug_log!(
                        "Parsed agent_id: {:?}, name: {}",
                        agent_id, name
                    );

                    // No viewport transformation needed since viewport is fixed at (0,0) and zoom is 1.0

                    // Add agent to canvas tracking and dispatch message
                    if let Some(agent_id) = agent_id {
                        APP_STATE.with(|state| {
                            let mut state = state.borrow_mut();
                            state.agents_on_canvas.insert(agent_id);
                        });
                    }

                    // Dispatch message to add node at drop position
                    dispatch_global_message(crate::messages::Message::AddCanvasNode {
                        agent_id,
                        x,
                        y,
                        node_type: NodeType::AgentIdentity,
                        text: name,
                    });

                    // Force UI refresh so the node appears immediately and agent shelf updates
                    let _ = crate::state::AppState::refresh_ui_after_state_change();
                } else {
                    web_sys::console::error_1(&"Failed to parse dropped agent data".into());
                }
            }
        }

        // Remove highlight class
        let window = web_sys::window().expect("no global window exists");
        let document = window.document().expect("should have a document");

        if let Some(container) = document.get_element_by_id("canvas-container") {
            if let Some(element) = container.dyn_ref::<web_sys::HtmlElement>() {
                element
                    .class_list()
                    .remove_1("canvas-drop-target")
                    .unwrap_or_default();
            }
        }
    }) as Box<dyn FnMut(_)>);

    canvas_drop.add_event_listener_with_callback("drop", drop_handler.as_ref().unchecked_ref())?;
    drop_handler.forget();

    Ok(())
}
