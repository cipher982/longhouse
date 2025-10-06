//! Minimal, working trigger configuration modal for canvas trigger nodes.
//! Follows the modal and message dispatch patterns used in tool_config_modal.rs.

use crate::components::modal;
use crate::messages::Message;
use crate::models::{NodeType, TriggerType, WorkflowNode};
use crate::state::dispatch_global_message;
use wasm_bindgen::prelude::*;
use web_sys::Document;

/// Public API for the trigger config modal.
pub struct TriggerConfigModal;

impl TriggerConfigModal {
    /// Opens the trigger config modal for the given node.
    pub fn open(document: &Document, node: &WorkflowNode) -> Result<(), JsValue> {
        let (modal, modal_content) = modal::ensure_modal(document, "trigger-config-modal")?;

        // Only allow opening for trigger nodes
        let (trigger_type, config) = match node.get_semantic_type() {
            NodeType::Trigger {
                trigger_type,
                config,
            } => (trigger_type, config),
            _ => return Err(JsValue::from_str("Not a trigger node")),
        };

        // Build modal HTML
        let mut body_html = format!(
            "<div class='modal-header'><h2>Configure Trigger: {}</h2><span class='close' id='trigger-modal-close'>&times;</span></div>\
             <div class='modal-body'>",
            match trigger_type {
                TriggerType::Webhook => "Webhook",
                TriggerType::Schedule => "Schedule",
                TriggerType::Email => "Email",
                TriggerType::Manual => "Manual",
            }
        );

        // Enhanced config UI for each trigger type with validation
        match trigger_type {
            TriggerType::Webhook => {
                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Webhook URL</label>");
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-webhook-url' type='text' value='{}' readonly />",
                    config.params.get("url").and_then(|v| v.as_str()).unwrap_or("Will be generated")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>This URL will be generated when the trigger is created</small>");
                body_html.push_str("</div>");

                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Secret</label>");
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-webhook-secret' type='text' value='{}' readonly />",
                    config.params.get("secret").and_then(|v| v.as_str()).unwrap_or("Will be generated")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>Used to verify webhook authenticity</small>");
                body_html.push_str("</div>");
            }

            TriggerType::Schedule => {
                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Schedule Type</label>");
                let schedule_type = config
                    .params
                    .get("schedule_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("workflow");
                body_html.push_str("<select class='input w-full mb-2' id='trigger-schedule-type'>");
                body_html.push_str(&format!(
                    "<option value='workflow' {}>Workflow (schedule this workflow)</option>",
                    if schedule_type == "workflow" {
                        "selected"
                    } else {
                        ""
                    }
                ));
                body_html.push_str(&format!(
                    "<option value='agent' {}>Agent (schedule an agent)</option>",
                    if schedule_type == "agent" {
                        "selected"
                    } else {
                        ""
                    }
                ));
                body_html.push_str("</select>");
                body_html.push_str(
                    "<small class='text-gray-500 mb-4 block'>Choose what to schedule</small>",
                );
                body_html.push_str("</div>");

                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Cron Expression</label>");
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-schedule-cron' type='text' value='{}' placeholder='0 9 * * *' />",
                    config.params.get("cron_expression").and_then(|v| v.as_str()).unwrap_or("")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>Examples: '0 9 * * *' (daily at 9 AM), '0 */6 * * *' (every 6 hours)</small>");
                body_html.push_str("</div>");

                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Timezone</label>");
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-schedule-tz' type='text' value='{}' placeholder='UTC' />",
                    config.params.get("timezone").and_then(|v| v.as_str()).unwrap_or("UTC")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>Timezone for the schedule (e.g., UTC, America/New_York)</small>");
                body_html.push_str("</div>");
            }

            TriggerType::Email => {
                body_html.push_str("<div class='form-group'>");
                body_html.push_str("<label class='block mb-2 font-medium'>Email Provider</label>");
                let provider = config
                    .params
                    .get("provider")
                    .and_then(|v| v.as_str())
                    .unwrap_or("gmail");
                body_html
                    .push_str("<select class='input w-full mb-2' id='trigger-email-provider'>");
                body_html.push_str(&format!(
                    "<option value='gmail' {}>Gmail</option>",
                    if provider == "gmail" { "selected" } else { "" }
                ));
                body_html.push_str(&format!(
                    "<option value='outlook' {}>Outlook</option>",
                    if provider == "outlook" {
                        "selected"
                    } else {
                        ""
                    }
                ));
                body_html.push_str("</select>");
                body_html.push_str(
                    "<small class='text-gray-500 mb-4 block'>Email service provider</small>",
                );
                body_html.push_str("</div>");

                body_html.push_str("<div class='form-group'>");
                body_html.push_str(
                    "<label class='block mb-2 font-medium'>Sender Filter (optional)</label>",
                );
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-email-sender' type='text' value='{}' placeholder='user@example.com' />",
                    config.params.get("sender").and_then(|v| v.as_str()).unwrap_or("")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>Only trigger for emails from this sender</small>");
                body_html.push_str("</div>");

                body_html.push_str("<div class='form-group'>");
                body_html.push_str(
                    "<label class='block mb-2 font-medium'>Subject Filter (optional)</label>",
                );
                body_html.push_str(&format!(
                    "<input class='input w-full mb-2' id='trigger-email-subject' type='text' value='{}' placeholder='Important' />",
                    config.params.get("subject").and_then(|v| v.as_str()).unwrap_or("")
                ));
                body_html.push_str("<small class='text-gray-500 mb-4 block'>Only trigger for emails containing this text in subject</small>");
                body_html.push_str("</div>");
            }

            TriggerType::Manual => {
                body_html.push_str("<div class='form-group text-center p-4'>");
                body_html.push_str("<p class='mb-4 text-gray-600'>This trigger must be started manually using the Run button in the toolbar.</p>");
                body_html.push_str(
                    "<p class='text-sm text-gray-500'>No additional configuration needed.</p>",
                );
                body_html.push_str("</div>");
            }
        }

        body_html.push_str("</div>\
            <div class='modal-buttons'><button class='btn' id='trigger-modal-cancel'>Cancel</button><button class='btn-primary' id='trigger-modal-save'>Save</button></div>");

        modal_content.set_inner_html(&body_html);

        // Event listeners for close/cancel/save
        if let Some(close_btn) = document.get_element_by_id("trigger-modal-close") {
            let modal = modal.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                let _ = modal::hide(&modal);
            }));
            let _ =
                close_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
            cb.forget();
        }
        if let Some(cancel_btn) = document.get_element_by_id("trigger-modal-cancel") {
            let modal = modal.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                let _ = modal::hide(&modal);
            }));
            let _ =
                cancel_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
            cb.forget();
        }
        if let Some(save_btn) = document.get_element_by_id("trigger-modal-save") {
            let node_id = node.node_id.clone();
            let trigger_type = trigger_type.clone();
            let document = document.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_event: web_sys::Event| {
                // Gather updated config from modal fields with validation
                let mut params = serde_json::Map::new();
                let mut validation_errors = Vec::new();

                match trigger_type {
                    TriggerType::Webhook => {
                        // Webhook params are readonly, so nothing to update
                    }
                    TriggerType::Schedule => {
                        let schedule_type = document
                            .get_element_by_id("trigger-schedule-type")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlSelectElement>().ok())
                            .map(|select| select.value())
                            .unwrap_or("workflow".to_string());

                        let cron = document
                            .get_element_by_id("trigger-schedule-cron")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                            .map(|input| input.value())
                            .unwrap_or_default();

                        let tz = document
                            .get_element_by_id("trigger-schedule-tz")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                            .map(|input| input.value())
                            .unwrap_or("UTC".to_string());

                        // Basic cron validation
                        if cron.trim().is_empty() {
                            validation_errors.push("Cron expression is required");
                        } else {
                            let parts: Vec<&str> = cron.trim().split_whitespace().collect();
                            if parts.len() != 5 {
                                validation_errors.push("Cron expression must have 5 parts (minute hour day month weekday)");
                            }
                        }

                        if validation_errors.is_empty() {
                            params.insert(
                                "schedule_type".to_string(),
                                serde_json::Value::String(schedule_type),
                            );
                            params.insert(
                                "cron_expression".to_string(),
                                serde_json::Value::String(cron),
                            );
                            params.insert("timezone".to_string(), serde_json::Value::String(tz));
                        }
                    }
                    TriggerType::Email => {
                        let provider = document
                            .get_element_by_id("trigger-email-provider")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlSelectElement>().ok())
                            .map(|select| select.value())
                            .unwrap_or("gmail".to_string());

                        let sender = document
                            .get_element_by_id("trigger-email-sender")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                            .map(|input| input.value())
                            .unwrap_or_default();

                        let subject = document
                            .get_element_by_id("trigger-email-subject")
                            .and_then(|el| el.dyn_into::<web_sys::HtmlInputElement>().ok())
                            .map(|input| input.value())
                            .unwrap_or_default();

                        params.insert("provider".to_string(), serde_json::Value::String(provider));
                        if !sender.trim().is_empty() {
                            params.insert("sender".to_string(), serde_json::Value::String(sender));
                        }
                        if !subject.trim().is_empty() {
                            params
                                .insert("subject".to_string(), serde_json::Value::String(subject));
                        }
                    }
                    TriggerType::Manual => {}
                }

                // Show validation errors if any
                if !validation_errors.is_empty() {
                    let error_msg = validation_errors.join("\\n");
                    web_sys::window()
                        .unwrap()
                        .alert_with_message(&error_msg)
                        .unwrap();
                    return;
                }
                // Dispatch message to update node config
                dispatch_global_message(Message::UpdateTriggerNodeConfig {
                    node_id: node_id.clone(),
                    params: serde_json::Value::Object(params),
                });
                // Close modal
                if let Some(modal) = document.get_element_by_id("trigger-config-modal") {
                    let _ = modal::hide(&modal);
                }
            }));
            let _ = save_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
            cb.forget();
        }

        modal::show(&modal);
        Ok(())
    }
}
