//! A vertical "Agent Shelf" component that lists all agents and allows users to quickly
//! place an agent onto the canvas.  For the first milestone we implement it as a simple
//! clickable list (no true drag-and-drop yet). Clicking an agent pill dispatches a
//! `Message::AddCanvasNode` with x = 0, y = 0 so the node is centred by existing logic.

use crate::state::APP_STATE;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, DragEvent, Element, HtmlElement};

/// Public helper that (re-)creates the Agent Shelf DOM element and appends it to the
/// `<div id="app-container">` root if it isn't present yet.
pub fn refresh_agent_shelf(document: &Document) -> Result<(), JsValue> {
    if let Some(existing) = document.get_element_by_id("agent-shelf") {
        // Shelf already exists – just repopulate the list.
        populate_agent_shelf(document, &existing)?;
    } else {
        // First render – create the root element and append to the main container.
        let shelf_el = create_root_element(document)?;
        populate_agent_shelf(document, &shelf_el)?;

        // Try to append next to the canvas inside `app-container` for now.
        // If the container is missing (unlikely) fall back to <body>.
        if let Some(parent) = document.get_element_by_id("app-container") {
            // Insert as the first child so it sits on the left side in flex layouts.
            let _ = parent.insert_before(&shelf_el, parent.first_child().as_ref());
        } else if let Some(body) = document.body() {
            body.append_child(&shelf_el)?;
        }
    }

    Ok(())
}

/// Internal – create the outer `<div id="agent-shelf">` container.
fn create_root_element(document: &Document) -> Result<Element, JsValue> {
    let el = document.create_element("div")?;
    el.set_id("agent-shelf");
    // Expose stable hook for E2E tests
    let _ = el.set_attribute("data-testid", "agent-shelf");
    // Make focusable for accessibility / focus management
    let _ = el.set_attribute("tabindex", "-1");
    // No inline styles needed - using CSS now
    Ok(el)
}

/// Internal – clears and repopulates the shelf with the current set of agents.
fn populate_agent_shelf(document: &Document, shelf_el: &Element) -> Result<(), JsValue> {
    // Clear current children.
    shelf_el.set_inner_html("");

    // Add header
    let header = document.create_element("div")?;
    header.set_class_name("agent-shelf-header");
    header.set_inner_html("Available Agents");
    shelf_el.append_child(&header)?;

    // Borrow state to iterate agents.
    APP_STATE.with(|state| {
        let state = state.borrow();

        // If agents haven't been loaded yet, show a loading state
        if !state.agents_loaded {
            let loading_msg = document.create_element("div").unwrap();
            loading_msg.set_class_name("agent-shelf-loading");
            loading_msg.set_inner_html("Loading agents...");
            shelf_el.append_child(&loading_msg).unwrap();
            return;
        }

        // If no agents, show appropriate message
        if state.agents.is_empty() {
            let empty_msg = document.create_element("div").unwrap();
            empty_msg.set_class_name("agent-shelf-empty");
            empty_msg.set_inner_html("No agents available");
            shelf_el.append_child(&empty_msg).unwrap();
            return;
        }
        // Sort agents by name to ensure consistent ordering (HashMap iteration is non-deterministic)
        let mut sorted_agents: Vec<_> = state.agents.values().collect();
        sorted_agents.sort_by(|a, b| a.name.cmp(&b.name));

        for agent in sorted_agents {
            if let Some(agent_id) = agent.id {
                // Create a simple pill element.
                let pill: HtmlElement = document.create_element("div").unwrap().dyn_into().unwrap();
                pill.set_class_name("agent-pill");
                pill.set_inner_text(&agent.name);
                pill.set_attribute("data-agent-id", &agent_id.to_string()).unwrap();

                // Check if agent is already on canvas
                let is_on_canvas = state.agents_on_canvas.contains(&agent_id);

                if is_on_canvas {
                    pill.class_list().add_1("disabled").unwrap();
                } else {
                    // Only make draggable if not already on canvas
                    pill.set_attribute("draggable", "true").unwrap();
                    // Make focusable for keyboard users
                    pill.set_tab_index(0);

                    // Clone data for the closures
                    let agent_id_for_drag = agent_id.to_string();
                    let agent_name_for_drag = agent.name.clone();

                    let dragstart_closure = Closure::<dyn FnMut(_)>::new(move |event: DragEvent| {
                        let dt = event.data_transfer().unwrap();

                        // Set the drag data - include both id and name
                        let data = format!("{{\"agent_id\":{},\"name\":\"{}\"}}", agent_id_for_drag, agent_name_for_drag);
                        dt.set_data("text/plain", &data).unwrap();

                        // Set visual drag effect
                        dt.set_effect_allowed("copy");

                        // Create a clean drag image instead of showing the full element
                        if let Some(target) = event.current_target() {
                            if let Some(element) = target.dyn_ref::<HtmlElement>() {
                                // Add dragging class for visual feedback
                                element.class_list().add_1("dragging").unwrap();

                                // Create a simple drag image - just a small rounded rectangle
                                if let Ok(document) = web_sys::window().unwrap().document().ok_or("No document") {
                                    let drag_image = document.create_element("div").unwrap();
                                    drag_image.set_attribute("style",
                                        "position: absolute; top: -1000px; left: -1000px; \
                                         width: 80px; height: 30px; \
                                         background: #3b82f6; color: white; \
                                         border-radius: 15px; \
                                         display: flex; align-items: center; justify-content: center; \
                                         font-size: 12px; font-weight: 500; \
                                         box-shadow: 0 2px 8px rgba(0,0,0,0.2);"
                                    ).unwrap();
                                    drag_image.set_inner_html("Agent");

                                    if let Some(body) = document.body() {
                                        body.append_child(&drag_image).unwrap();

                                        // Set the custom drag image
                                        dt.set_drag_image(&drag_image, 40, 15);

                                        // Clean up the temporary element after a short delay
                                        let cleanup_drag_image = drag_image.clone();
                                        let cleanup_closure = Closure::once(Box::new(move || {
                                            if let Some(parent) = cleanup_drag_image.parent_node() {
                                                let _ = parent.remove_child(&cleanup_drag_image);
                                            }
                                        }));

                                        web_sys::window().unwrap()
                                            .set_timeout_with_callback_and_timeout_and_arguments_0(
                                                cleanup_closure.as_ref().unchecked_ref(),
                                                100
                                            ).unwrap();
                                        cleanup_closure.forget();
                                    }
                                }
                            }
                        }

                    });
                    pill.add_event_listener_with_callback("dragstart", dragstart_closure.as_ref().unchecked_ref()).unwrap();
                    dragstart_closure.forget();

                    // Add dragend event to clean up
                    let dragend_closure = Closure::<dyn FnMut(_)>::new(move |event: DragEvent| {
                        // Get the target element
                        if let Some(target) = event.current_target() {
                            if let Some(element) = target.dyn_ref::<HtmlElement>() {
                                // Remove dragging class
                                element.class_list().remove_1("dragging").unwrap();
                            }
                        }
                    });
                    pill.add_event_listener_with_callback("dragend", dragend_closure.as_ref().unchecked_ref()).unwrap();
                    dragend_closure.forget();
                }

                // Append to shelf.
                shelf_el.append_child(&pill).unwrap();
            }
        }
    });

    // Add node palette section header
    let node_palette_header = document.create_element("div")?;
    node_palette_header.set_class_name("agent-shelf-header");
    node_palette_header.set_inner_html("Tools & Triggers");
    shelf_el.append_child(&node_palette_header)?;

    // Add node palette container
    let node_palette_container = document.create_element("div")?;
    node_palette_container.set_id("node-palette-shelf");
    node_palette_container.set_class_name("node-palette-shelf");
    shelf_el.append_child(&node_palette_container)?;

    // Render the node palette into the shelf
    let palette = crate::components::node_palette::NodePalette::new();
    let _ = palette.render_into(document, &node_palette_container);
    // Apply persisted open state (mobile drawer)
    apply_persisted_open_state(document);
    Ok(())
}

fn apply_persisted_open_state(document: &Document) {
    if let Some(win) = web_sys::window() {
        if let Ok(Some(storage)) = win.local_storage() {
            if let Ok(Some(val)) = storage.get_item("agent_shelf_open") {
                if val == "1" {
                    if let Some(shelf) = document.get_element_by_id("agent-shelf") {
                        let _ = shelf.class_list().add_1("open");
                    }
                    if let Some(body) = document.body() {
                        let _ = body.class_list().add_1("shelf-open");
                    }
                    if let Some(btn) = document.get_element_by_id("shelf-toggle-btn") {
                        let _ = btn.set_attribute("aria-expanded", "true");
                        let _ = btn.set_attribute("aria-label", "Close agent panel");
                        // Swap icon to 'x' via feather placeholder
                        if let Ok(icon) = document.create_element("i") {
                            let _ = icon.set_attribute("data-feather", "x");
                            while let Some(child) = btn.first_child() {
                                let _ = btn.remove_child(&child);
                            }
                            let _ = btn.append_child(&icon);
                        }
                    }
                }
            }
        }
    }
}
