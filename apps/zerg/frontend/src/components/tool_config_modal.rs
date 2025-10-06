use crate::models::{InputMapping, NodeType, ToolConfig, WorkflowNode};
use std::collections::HashMap;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, HtmlInputElement, HtmlSelectElement};

/// Enhanced tool configuration modal with validation and parameter mapping.
pub struct ToolConfigModal;

impl ToolConfigModal {
    /// Opens the tool config modal for the given node with validation and enhanced UI.
    pub fn open(document: &Document, node: &WorkflowNode) -> Result<(), JsValue> {
        let (modal, modal_content) =
            crate::components::modal::ensure_modal(document, "tool-config-modal")?;

        // Only allow opening for tool nodes
        let (tool_name, _server_name, config) = match node.get_semantic_type() {
            NodeType::Tool {
                tool_name,
                server_name,
                config,
                ..
            } => (tool_name, server_name, config),
            _ => return Err(JsValue::from_str("Not a tool node")),
        };

        // Build modal HTML with enhanced styling
        let mut body_html = format!(
            "<div class='modal-header'><h2>Configure Tool: {}</h2><span class='close' id='tool-modal-close'>&times;</span></div>\
            <div class='modal-body'><p class='text-sm text-gray-400 mb-4'>Configure parameters for the {} tool</p>",
            tool_name, tool_name
        );

        // Use default tool parameters since we don't have tool schema access here
        let tool_inputs = vec![
            ("input", "Input", true),
            ("format", "Output Format", false),
            ("options", "Additional Options", false),
        ];

        // Enhanced input configuration with validation
        for (input_key, input_label, is_required) in &tool_inputs {
            let mapping_type = match config.input_mappings.get(*input_key) {
                Some(InputMapping::Static(_)) | None => "static",
                Some(InputMapping::FromNode { .. }) => "node_output",
            };
            let static_value = match config.input_mappings.get(*input_key) {
                Some(InputMapping::Static(val)) => val.as_str().unwrap_or("").to_string(),
                _ => "".to_string(),
            };
            let node_ref = match config.input_mappings.get(*input_key) {
                Some(InputMapping::FromNode {
                    node_id,
                    output_key,
                }) => format!("{}:{}", node_id, output_key),
                _ => "".to_string(),
            };

            let required_html = if *is_required {
                "<span class='required text-red-500'>*</span>"
            } else {
                ""
            };

            body_html.push_str(&format!(
                "<div class='form-group mb-4'>
                    <label class='block mb-2 font-medium'>{} {}</label>
                    <select class='input w-full mb-2 mapping-type-selector' data-input-key='{}'>
                        <option value='static' {}>Static Value</option>
                        <option value='node_output' {}>From Node Output</option>
                    </select>
                    <input type='text' class='input w-full mb-2 static-value-input {}' data-input-key='{}' value='{}' placeholder='Enter {} value'>
                    <input type='text' class='input w-full mb-2 node-ref-input {}' data-input-key='{}' value='{}' placeholder='node_id:output_key'>
                    <small class='text-gray-500 block'>{}</small>
                </div>",
                input_label,
                required_html,
                input_key,
                if mapping_type == "static" { "selected" } else { "" },
                if mapping_type == "node_output" { "selected" } else { "" },
                if mapping_type == "static" { "" } else { "hidden" },
                input_key,
                static_value,
                input_label.to_lowercase(),
                if mapping_type == "node_output" { "" } else { "hidden" },
                input_key,
                node_ref,
                if *is_required {
                    "This parameter is required for the tool to function properly."
                } else {
                    "This parameter is optional."
                }
            ));
        }

        // Add auto-execute option
        body_html.push_str(&format!(
            "<div class='form-group mb-4'>
                <label class='flex items-center'>
                    <input type='checkbox' class='mr-2' id='tool-auto-execute' {}>
                    <span>Auto-execute when inputs are ready</span>
                </label>
                <small class='text-gray-500 block mt-1'>If enabled, this tool will run automatically when all required inputs are available.</small>
            </div>",
            if config.auto_execute { "checked" } else { "" }
        ));

        body_html.push_str("</div>\
            <div class='modal-buttons'><button class='btn' id='tool-modal-cancel'>Cancel</button><button class='btn-primary' id='tool-modal-save'>Save</button></div>");

        modal_content.set_inner_html(&body_html);

        // Add dynamic behavior for each selector individually
        for (input_key, _, _) in &tool_inputs {
            if let Some(selector) = document
                .query_selector(&format!(
                    ".mapping-type-selector[data-input-key='{}']",
                    input_key
                ))
                .ok()
                .flatten()
                .and_then(|el| el.dyn_into::<HtmlSelectElement>().ok())
            {
                let input_key_clone = input_key.to_string();
                let document_clone = document.clone();

                let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                    let selector = document_clone
                        .query_selector(&format!(
                            ".mapping-type-selector[data-input-key='{}']",
                            input_key_clone
                        ))
                        .ok()
                        .flatten()
                        .and_then(|el| el.dyn_into::<HtmlSelectElement>().ok());

                    if let Some(sel) = selector {
                        let value = sel.value();
                        let static_input = document_clone
                            .query_selector(&format!(
                                ".static-value-input[data-input-key='{}']",
                                input_key_clone
                            ))
                            .ok()
                            .flatten();
                        let node_input = document_clone
                            .query_selector(&format!(
                                ".node-ref-input[data-input-key='{}']",
                                input_key_clone
                            ))
                            .ok()
                            .flatten();

                        if value == "static" {
                            if let Some(input) = static_input {
                                let _ = input.class_list().remove_1("hidden");
                            }
                            if let Some(input) = node_input {
                                let _ = input.class_list().add_1("hidden");
                            }
                        } else {
                            if let Some(input) = static_input {
                                let _ = input.class_list().add_1("hidden");
                            }
                            if let Some(input) = node_input {
                                let _ = input.class_list().remove_1("hidden");
                            }
                        }
                    }
                }));

                selector.add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }
        }

        // Event listeners for close/cancel/save
        if let Some(close_btn) = document.get_element_by_id("tool-modal-close") {
            let modal = modal.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                let _ = crate::components::modal::hide(&modal);
            }));
            close_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }
        if let Some(cancel_btn) = document.get_element_by_id("tool-modal-cancel") {
            let modal = modal.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                let _ = crate::components::modal::hide(&modal);
            }));
            cancel_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }
        if let Some(save_btn) = document.get_element_by_id("tool-modal-save") {
            let node_id = node.node_id.clone();
            let tool_inputs_clone = tool_inputs.clone();
            let document = document.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                // Collect values for each input with validation
                let mut new_config = ToolConfig {
                    static_params: HashMap::new(),
                    input_mappings: HashMap::new(),
                    auto_execute: true,
                };
                let mut validation_errors = Vec::new();

                // Get auto-execute setting
                new_config.auto_execute = document
                    .get_element_by_id("tool-auto-execute")
                    .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                    .map(|checkbox| checkbox.checked())
                    .unwrap_or(true);

                for (input_key, input_label, is_required) in &tool_inputs_clone {
                    let mapping_type = document
                        .query_selector(&format!(
                            ".mapping-type-selector[data-input-key='{}']",
                            input_key
                        ))
                        .ok()
                        .flatten()
                        .and_then(|sel| sel.dyn_into::<HtmlSelectElement>().ok())
                        .map(|sel| sel.value())
                        .unwrap_or_else(|| "static".to_string());

                    if mapping_type == "static" {
                        let value = document
                            .query_selector(&format!(
                                ".static-value-input[data-input-key='{}']",
                                input_key
                            ))
                            .ok()
                            .flatten()
                            .and_then(|inp| inp.dyn_into::<HtmlInputElement>().ok())
                            .map(|inp| inp.value())
                            .unwrap_or_default();

                        // Validate required fields
                        if *is_required && value.trim().is_empty() {
                            validation_errors.push(format!("{} is required", input_label));
                        } else if !value.trim().is_empty() {
                            new_config.input_mappings.insert(
                                input_key.to_string(),
                                InputMapping::Static(serde_json::Value::String(value)),
                            );
                        }
                    } else {
                        let node_ref = document
                            .query_selector(&format!(
                                ".node-ref-input[data-input-key='{}']",
                                input_key
                            ))
                            .ok()
                            .flatten()
                            .and_then(|inp| inp.dyn_into::<HtmlInputElement>().ok())
                            .map(|inp| inp.value())
                            .unwrap_or_default();

                        // Validate node reference format
                        if *is_required && node_ref.trim().is_empty() {
                            validation_errors
                                .push(format!("{} node reference is required", input_label));
                        } else if !node_ref.trim().is_empty() {
                            let parts: Vec<&str> = node_ref.split(':').collect();
                            if parts.len() != 2 {
                                validation_errors.push(format!(
                                    "{} node reference must be in format 'node_id:output_key'",
                                    input_label
                                ));
                            } else {
                                new_config.input_mappings.insert(
                                    input_key.to_string(),
                                    InputMapping::FromNode {
                                        node_id: parts[0].to_string(),
                                        output_key: parts[1].to_string(),
                                    },
                                );
                            }
                        }
                    }
                }

                // Show validation errors if any
                if !validation_errors.is_empty() {
                    let error_msg = validation_errors.join("\\n");
                    crate::toast::error(&error_msg);
                    return;
                }

                // Dispatch message to update tool config
                crate::state::dispatch_global_message(crate::messages::Message::SaveToolConfig {
                    node_id: node_id.clone(),
                    config: new_config,
                });

                // Close modal
                if let Some(modal) = document.get_element_by_id("tool-config-modal") {
                    let _ = crate::components::modal::hide(&modal);
                }
            }));
            save_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }

        crate::components::modal::show(&modal);
        Ok(())
    }
}
