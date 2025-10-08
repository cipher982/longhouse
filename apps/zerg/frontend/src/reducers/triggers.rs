//! Triggers domain reducer: handles trigger CRUD, trigger UI refresh, Gmail integration, etc.

use crate::messages::{Command, Message};
use crate::state::AppState;

/// Handles trigger-related messages. Returns true if the message was handled.
pub fn update(state: &mut AppState, msg: &Message, commands: &mut Vec<Command>) -> bool {
    match msg {
        Message::LoadTriggers(agent_id) => {
            commands.push(Command::FetchTriggers(*agent_id));
            true
        }
        Message::TriggersLoaded { agent_id, triggers } => {
            state.triggers.insert(*agent_id, triggers.clone());
            state.mark_dirty();
            let agent_id_clone = *agent_id;
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        if let Some(content) = doc.get_element_by_id("triggers-content") {
                            if content
                                .get_attribute("style")
                                .map(|s| s.contains("display: block"))
                                .unwrap_or(false)
                            {
                                let _ = crate::components::agent_config_modal::render_triggers_list(
                                    &doc,
                                    agent_id_clone,
                                );
                            }
                        }
                    }
                }
            })));
            true
        }
        Message::TriggerCreated { agent_id, trigger } => {
            state
                .triggers
                .entry(*agent_id)
                .or_default()
                .push(trigger.clone());
            state.mark_dirty();
            let agent_id_clone = *agent_id;
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::agent_config_modal::render_triggers_list(
                            &doc,
                            agent_id_clone,
                        );
                    }
                }
            })));
            true
        }
        Message::TriggerDeleted {
            agent_id,
            trigger_id,
        } => {
            if let Some(list) = state.triggers.get_mut(agent_id) {
                list.retain(|t| t.id != *trigger_id);
            }
            state.mark_dirty();
            let agent_id_clone = *agent_id;
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::agent_config_modal::render_triggers_list(
                            &doc,
                            agent_id_clone,
                        );
                    }
                }
            })));
            true
        }
        Message::GmailConnected => {
            state.gmail_connected = true;
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::agent_config_modal::render_gmail_connect_status(&doc);
                    }
                }
            })));
            true
        }
        Message::GmailConnectedWithConnector { connector_id } => {
            state.gmail_connected = true;
            state.gmail_connector_id = Some(*connector_id);
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::agent_config_modal::render_gmail_connect_status(&doc);
                    }
                }
            })));
            true
        }
        _ => false,
    }
}
