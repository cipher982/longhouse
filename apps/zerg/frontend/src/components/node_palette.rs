use crate::generated::{ServerName, ToolName};
use crate::models::{NodeType, ToolConfig, ToolVisibility, TriggerConfig, TriggerType};
use crate::state::{AppState, APP_STATE};
use std::collections::HashMap;
use wasm_bindgen::{JsCast, JsValue};
use web_sys::{Document, DragEvent, Element, MouseEvent};

/// Node palette component for dragging tools and triggers onto the canvas
pub struct NodePalette {
    pub is_open: bool,
    pub search_query: String,
    pub selected_category: Option<String>,
}

/// Represents a draggable node type in the palette
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PaletteNode {
    pub id: String,
    pub name: String,
    pub description: String,
    pub icon: String,
    pub category: String,
    pub node_type: NodeType,
}

impl NodePalette {
    pub fn new() -> Self {
        Self {
            is_open: false,
            search_query: String::new(),
            selected_category: None,
        }
    }

    /// Render the node palette UI into a provided container (shelf)
    pub fn render_into(&self, document: &Document, container: &Element) -> Result<(), JsValue> {
        // Clear existing content
        container.set_inner_html("");

        // Add header
        let header = document.create_element("div")?;
        header.set_class_name("palette-header");
        header.set_inner_html(
            "
            <h3>
                Node Palette
            </h3>
        ",
        );
        container.append_child(&header)?;

        // Add search box
        let search_container = document.create_element("div")?;
        search_container.set_inner_html(&format!(
            "
            <input
                type='text'
                placeholder='Search nodes...'
                value='{}'
                class='palette-search-input'
                id='palette-search'
            />
        ",
            self.search_query
        ));
        container.append_child(&search_container)?;

        // Get available palette nodes
        let nodes = self.get_palette_nodes();

        // Group nodes by category
        let mut categories: HashMap<String, Vec<PaletteNode>> = HashMap::new();
        for node in nodes {
            categories
                .entry(node.category.clone())
                .or_insert_with(Vec::new)
                .push(node);
        }

        // Render categories in a consistent order (HashMap iteration is non-deterministic)
        let mut sorted_categories: Vec<_> = categories.iter().collect();
        sorted_categories.sort_by(|a, b| {
            // Define a specific order for categories
            let order = |cat: &str| match cat {
                "Triggers" => 0,
                "I/O Tools" => 1,
                "Optional Tools" => 2,
                _ => 999, // Unknown categories go last
            };
            order(a.0).cmp(&order(b.0))
        });

        for (category_name, category_nodes) in sorted_categories {
            self.render_category(document, container, category_name, category_nodes)?;
        }

        Ok(())
    }

    /// Render a category section
    fn render_category(
        &self,
        document: &Document,
        container: &Element,
        category_name: &str,
        nodes: &[PaletteNode],
    ) -> Result<(), JsValue> {
        // Category header
        let category_header = document.create_element("div")?;
        category_header.set_class_name("category-header");
        category_header.set_inner_html(&format!(
            "
            <h4 class='palette-category-header'>
                {}
            </h4>
        ",
            category_name
        ));
        container.append_child(&category_header)?;

        // Render nodes in this category
        for node in nodes {
            self.render_palette_node(document, container, node)?;
        }

        // Add spacing between categories
        let spacer = document.create_element("div")?;
        spacer.set_attribute("style", "height: 16px;")?;
        container.append_child(&spacer)?;

        Ok(())
    }

    /// Render an individual palette node
    fn render_palette_node(
        &self,
        document: &Document,
        container: &Element,
        node: &PaletteNode,
    ) -> Result<(), JsValue> {
        let node_element = document.create_element("div")?;
        node_element.set_class_name("palette-node");
        node_element.set_attribute("draggable", "true")?;
        node_element.set_attribute("data-node-type", &node.id)?;

        // Style the node
        // Styles moved to CSS file
        // Add hover effects with event listeners
        let node_clone = node_element.clone();
        let onmouseenter = wasm_bindgen::closure::Closure::wrap(Box::new(move |_: MouseEvent| {
            let _ = node_clone.set_class_name("palette-node hover"); // Apply hover class
        }) as Box<dyn FnMut(_)>);

        let node_clone2 = node_element.clone();
        let onmouseleave = wasm_bindgen::closure::Closure::wrap(Box::new(move |_: MouseEvent| {
            let _ = node_clone2.set_class_name("palette-node"); // Remove hover class
        }) as Box<dyn FnMut(_)>);

        node_element.add_event_listener_with_callback(
            "mouseenter",
            onmouseenter.as_ref().unchecked_ref(),
        )?;
        node_element.add_event_listener_with_callback(
            "mouseleave",
            onmouseleave.as_ref().unchecked_ref(),
        )?;

        // Store closures to prevent them from being dropped
        onmouseenter.forget();
        onmouseleave.forget();

        // Node content
        node_element.set_inner_html(&format!(
            "
            <span class='palette-node-icon'>{}</span>
            <div>
                <div class='palette-node-name'>{}</div>
                <div class='palette-node-description'>{}</div>
            </div>
        ",
            node.icon, node.name, node.description
        ));

        // Add drag event listeners
        self.add_drag_listeners(&node_element, node)?;

        container.append_child(&node_element)?;
        Ok(())
    }

    /// Add drag event listeners to a palette node
    fn add_drag_listeners(&self, element: &Element, node: &PaletteNode) -> Result<(), JsValue> {
        let node_data = node.clone();

        // Drag start event
        let ondragstart = wasm_bindgen::closure::Closure::wrap(Box::new(move |event: DragEvent| {
            if let Some(data_transfer) = event.data_transfer() {
                // Store the node type information
                let node_json = serde_json::to_string(&node_data).unwrap_or_default();
                let _ = data_transfer.set_data("application/json", &node_json);
                let _ = data_transfer.set_data("text/plain", &node_data.id);

                // Set drag effect
                data_transfer.set_effect_allowed("copy");

                // Create a clean drag image instead of showing the full element
                if let Ok(document) = web_sys::window().unwrap().document().ok_or("No document") {
                    let drag_image = document.create_element("div").unwrap();

                    // Choose color based on node type
                    let (bg_color, text) = match &node_data.node_type {
                        crate::models::NodeType::Tool { .. } => ("#10b981", "Tool"),
                        crate::models::NodeType::Trigger { .. } => ("#f59e0b", "Trigger"),
                        _ => ("#6366f1", "Node"),
                    };

                    drag_image
                        .set_attribute(
                            "style",
                            &format!(
                                "position: absolute; top: -1000px; left: -1000px; \
                         width: 80px; height: 30px; \
                         background: {}; color: white; \
                         border-radius: 15px; \
                         display: flex; align-items: center; justify-content: center; \
                         font-size: 12px; font-weight: 500; \
                         box-shadow: 0 2px 8px rgba(0,0,0,0.2);",
                                bg_color
                            ),
                        )
                        .unwrap();
                    drag_image.set_inner_html(text);

                    if let Some(body) = document.body() {
                        body.append_child(&drag_image).unwrap();

                        // Set the custom drag image
                        data_transfer.set_drag_image(&drag_image, 40, 15);

                        // Clean up the temporary element after a short delay
                        let cleanup_drag_image = drag_image.clone();
                        let cleanup_closure =
                            wasm_bindgen::closure::Closure::once(Box::new(move || {
                                if let Some(parent) = cleanup_drag_image.parent_node() {
                                    let _ = parent.remove_child(&cleanup_drag_image);
                                }
                            }));

                        web_sys::window()
                            .unwrap()
                            .set_timeout_with_callback_and_timeout_and_arguments_0(
                                cleanup_closure.as_ref().unchecked_ref(),
                                100,
                            )
                            .unwrap();
                        cleanup_closure.forget();
                    }
                }
            }
        }) as Box<dyn FnMut(_)>);

        element
            .add_event_listener_with_callback("dragstart", ondragstart.as_ref().unchecked_ref())?;
        ondragstart.forget();

        Ok(())
    }

    /// Get available palette nodes based on current state
    fn get_palette_nodes(&self) -> Vec<PaletteNode> {
        let mut nodes = Vec::new();

        // Trigger nodes
        nodes.push(PaletteNode {
            id: "trigger_webhook".to_string(),
            name: "Webhook Trigger".to_string(),
            description: "Trigger from HTTP webhook".to_string(),
            icon: "🔗".to_string(),
            category: "Triggers".to_string(),
            node_type: NodeType::Trigger {
                trigger_type: TriggerType::Webhook,
                config: TriggerConfig {
                    params: HashMap::new(),
                    enabled: true,
                    filters: Vec::new(),
                },
            },
        });

        nodes.push(PaletteNode {
            id: "trigger_schedule".to_string(),
            name: "Schedule Trigger".to_string(),
            description: "Trigger on schedule".to_string(),
            icon: "⏰".to_string(),
            category: "Triggers".to_string(),
            node_type: NodeType::Trigger {
                trigger_type: TriggerType::Schedule,
                config: TriggerConfig {
                    params: HashMap::new(),
                    enabled: true,
                    filters: Vec::new(),
                },
            },
        });

        nodes.push(PaletteNode {
            id: "trigger_email".to_string(),
            name: "Email Trigger".to_string(),
            description: "Trigger from email".to_string(),
            icon: "📧".to_string(),
            category: "Triggers".to_string(),
            node_type: NodeType::Trigger {
                trigger_type: TriggerType::Email,
                config: TriggerConfig {
                    params: HashMap::new(),
                    enabled: true,
                    filters: Vec::new(),
                },
            },
        });

        // Tool nodes - these would come from MCP registry in a real implementation
        nodes.push(PaletteNode {
            id: "tool_http_request".to_string(),
            name: "HTTP Request".to_string(),
            description: "Make HTTP API calls".to_string(),
            icon: "🌐".to_string(),
            category: "I/O Tools".to_string(),
            node_type: NodeType::Tool {
                tool_name: ToolName::HttpRequest.as_str().to_string(),
                server_name: ServerName::Http.as_str().to_string(),
                config: ToolConfig {
                    static_params: HashMap::new(),
                    input_mappings: HashMap::new(),
                    auto_execute: true,
                },
                visibility: ToolVisibility::AlwaysExternal,
            },
        });

        nodes.push(PaletteNode {
            id: "tool_github_create_issue".to_string(),
            name: "Create GitHub Issue".to_string(),
            description: "Create issues in GitHub".to_string(),
            icon: "🐙".to_string(),
            category: "I/O Tools".to_string(),
            node_type: NodeType::Tool {
                tool_name: "create_issue".to_string(),
                server_name: "github".to_string(),
                config: ToolConfig {
                    static_params: HashMap::new(),
                    input_mappings: HashMap::new(),
                    auto_execute: true,
                },
                visibility: ToolVisibility::AlwaysExternal,
            },
        });

        nodes.push(PaletteNode {
            id: "tool_slack_message".to_string(),
            name: "Send Slack Message".to_string(),
            description: "Send messages to Slack".to_string(),
            icon: "💬".to_string(),
            category: "I/O Tools".to_string(),
            node_type: NodeType::Tool {
                tool_name: "send_message".to_string(),
                server_name: "slack".to_string(),
                config: ToolConfig {
                    static_params: HashMap::new(),
                    input_mappings: HashMap::new(),
                    auto_execute: true,
                },
                visibility: ToolVisibility::AlwaysExternal,
            },
        });

        // Optional external tools (can be promoted from internal)
        nodes.push(PaletteNode {
            id: "tool_web_search".to_string(),
            name: "Web Search".to_string(),
            description: "Search the web (optional)".to_string(),
            icon: "🔍".to_string(),
            category: "Optional Tools".to_string(),
            node_type: NodeType::Tool {
                tool_name: "web_search".to_string(),
                server_name: "web".to_string(),
                config: ToolConfig {
                    static_params: HashMap::new(),
                    input_mappings: HashMap::new(),
                    auto_execute: true,
                },
                visibility: ToolVisibility::OptionalExternal,
            },
        });

        nodes
    }

    /// Toggle palette visibility
    pub fn toggle(&mut self) {
        self.is_open = !self.is_open;
    }

    /// Show the palette
    pub fn show(&mut self) {
        self.is_open = true;
    }

    /// Hide the palette
    pub fn hide(&mut self) {
        self.is_open = false;
    }
}

/// Initialize the node palette and add it to the global state
pub fn init_node_palette(document: &Document) -> Result<(), JsValue> {
    // No floating palette toggle button anymore

    // Initialize drag and drop handling for the canvas
    init_canvas_drop_handling(document)?;

    Ok(())
}

/// Initialize drag and drop handling for the canvas
fn init_canvas_drop_handling(document: &Document) -> Result<(), JsValue> {
    if let Some(canvas) = document.get_element_by_id("canvas") {
        // Prevent default drag behavior
        let ondragover = wasm_bindgen::closure::Closure::wrap(Box::new(move |event: DragEvent| {
            event.prevent_default();
        }) as Box<dyn FnMut(_)>);

        let ondrop = wasm_bindgen::closure::Closure::wrap(Box::new(move |event: DragEvent| {
            event.prevent_default();

            if let Some(data_transfer) = event.data_transfer() {
                if let Ok(node_data) = data_transfer.get_data("application/json") {
                    // Parse the node data
                    if let Ok(palette_node) = serde_json::from_str::<PaletteNode>(&node_data) {
                        // Get drop position
                        let rect = event
                            .current_target()
                            .unwrap()
                            .dyn_into::<Element>()
                            .unwrap()
                            .get_bounding_client_rect();
                        let x = event.client_x() as f64 - rect.left();
                        let y = event.client_y() as f64 - rect.top();

                        // Create node on canvas
                        APP_STATE.with(|state| {
                            let mut state = state.borrow_mut();
                            create_node_from_palette(&mut state, &palette_node, x, y);
                        });
                    }
                }
            }
        }) as Box<dyn FnMut(_)>);

        canvas.add_event_listener_with_callback("dragover", ondragover.as_ref().unchecked_ref())?;
        canvas.add_event_listener_with_callback("drop", ondrop.as_ref().unchecked_ref())?;

        ondragover.forget();
        ondrop.forget();
    }

    Ok(())
}

/// Create a node on the canvas from a palette node
pub fn create_node_from_palette(state: &mut AppState, palette_node: &PaletteNode, x: f64, y: f64) {
    // Transform canvas coordinates to world coordinates
    let world_x = x / state.zoom_level + state.viewport_x;
    let world_y = y / state.zoom_level + state.viewport_y;

    // Generate a unique node_id using timestamp and a random number
    let node_id = format!(
        "tool_{}_{}",
        js_sys::Date::now() as u64,
        js_sys::Math::random().to_string().replace(".", "")
    );

    // Clone the node_type and name for the new node
    let node_type = palette_node.node_type.clone();
    let name = palette_node.name.clone();

    // Enforce manual trigger invariant (defensive; palette currently doesn't offer manual)
    if let NodeType::Trigger { trigger_type, .. } = &node_type {
        if matches!(trigger_type, TriggerType::Manual) {
            let already_has_manual = state
                .workflow_nodes
                .values()
                .any(|n| matches!(n.get_semantic_type(), NodeType::Trigger { trigger_type: TriggerType::Manual, .. }));
            if already_has_manual {
                crate::toast::error("Only one Manual trigger allowed per workflow");
                return;
            }
        }
    }

    // Create the node via builder to ensure semantics-first construction
    use crate::node_builder::NodeBuilder;
    let node = NodeBuilder::from_semantic(node_type.clone())
        .id(node_id.clone())
        .at(world_x, world_y)
        .size(200.0, 80.0)
        .color("#f59e0b")
        .label(&name)
        .build();
    state.workflow_nodes.insert(node_id.clone(), node.clone());
    state
        .ui_state
        .insert(node_id.clone(), crate::models::UiNodeState::default());

    // Add node to current workflow structure (same as agent nodes)
    state.add_node_to_current_workflow(node);

    crate::debug_log!(
        "Created node {} at ({}, {})",
        node_id, world_x, world_y
    );

    // Mark state as dirty for re-render
    state.mark_dirty();
}
