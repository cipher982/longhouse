//! Chat-view specific reducer split out from `update.rs`.
//!
//! For the moment this file only contains *place-holders* so the project
//! compiles.  Real variants and logic will move here incrementally over the
//! next refactor commits.

use crate::messages::{Command, Message};
use crate::state::AppState;
use crate::debug_log;

/// Returns `true` when the message was handled by the chat reducer.
pub fn update(state: &mut AppState, msg: &Message, cmds: &mut Vec<Command>) -> bool {
    match msg {
        crate::messages::Message::CreateThread(agent_id, title) => {
            // Log for debugging
            debug_log!(
                "Creating thread for agent {} with title: {}",
                agent_id, title
            );
            // Instead of spawning directly, return a command that will be executed after state update
            cmds.push(crate::messages::Command::CreateThread {
                agent_id: *agent_id,
                title: title.clone(),
            });
            true
        }
        crate::messages::Message::ThreadCreated(thread) => {
            debug_log!("Update: Handling ThreadCreated: {:?}", thread);
            if let Some(thread_id) = thread.id {
                let agent_id = thread.agent_id;

                // Add thread to agent-scoped state
                let agent_state = state.ensure_agent_state(agent_id);
                agent_state.threads.insert(thread_id, thread.clone());

                // Select the new thread in agent context
                cmds.push(crate::messages::Command::SendMessage(
                    crate::messages::Message::SelectAgentThread {
                        agent_id,
                        thread_id,
                    },
                ));
            } else {
                web_sys::console::error_1(
                    &format!("ThreadCreated message missing thread ID: {:?}", thread).into(),
                );
            }
            true
        }
        crate::messages::Message::DeleteThread(thread_id) => {
            // Find which agent owns this thread and delete from agent state
            let mut agent_id_opt = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    agent_id_opt = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = agent_id_opt {
                if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                    // Delete the thread from agent state
                    agent_state.threads.remove(thread_id);
                    agent_state.thread_messages.remove(thread_id);

                    // If this was the current thread, clear it
                    if agent_state.current_thread_id == Some(*thread_id) {
                        agent_state.current_thread_id = None;
                    }

                    // Update UI with new thread list
                    cmds.push(Command::UpdateUI(Box::new(move || {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::RequestThreadListUpdate(agent_id),
                        );
                    })));
                }
            }

            // Send the delete request to the backend
            let thread_id_clone = *thread_id;
            wasm_bindgen_futures::spawn_local(async move {
                match crate::network::api_client::ApiClient::delete_thread(thread_id_clone).await {
                    Ok(_) => {}
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to delete thread: {:?}", e).into(),
                        );
                    }
                }
            });

            true
        }
        crate::messages::Message::SelectThread(thread_id) => {
            web_sys::console::warn_1(
                &"DEPRECATED: SelectThread called. Use SelectAgentThread instead.".into(),
            );

            // Find which agent owns this thread
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    return update(
                        state,
                        &crate::messages::Message::SelectAgentThread {
                            agent_id: *agent_id,
                            thread_id: *thread_id,
                        },
                        cmds,
                    );
                }
            }

            web_sys::console::error_1(
                &format!("Thread {} not found in any agent state", thread_id).into(),
            );
            true
        }
        crate::messages::Message::LoadThreadMessages(thread_id) => {
            state.is_chat_loading = true;
            cmds.push(crate::messages::Command::FetchThreadMessages(*thread_id));
            true
        }
        crate::messages::Message::ThreadMessagesLoaded(thread_id, messages) => {
            // Find which agent owns this thread and update agent-scoped state
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                let current_agent_id = state.current_agent_id;
                let should_update_ui =
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        agent_state
                            .thread_messages
                            .insert(*thread_id, messages.clone());

                        // Check if UI should be updated
                        current_agent_id == Some(agent_id)
                            && agent_state.current_thread_id == Some(*thread_id)
                    } else {
                        false
                    };

                if should_update_ui {
                    // Use agent-scoped UI refresh instead of global dispatches
                    cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                        crate::components::chat_view::refresh_chat_ui_from_agent_state(agent_id);
                    })));
                }
            } else {
                web_sys::console::warn_1(
                    &format!(
                        "ThreadMessagesLoaded: No agent found owning thread {}",
                        thread_id
                    )
                    .into(),
                );
            }
            true
        }
        crate::messages::Message::UpdateThreadTitle(thread_id, title) => {
            // Find which agent owns this thread and update agent-scoped state
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                    if let Some(thread) = agent_state.threads.get_mut(thread_id) {
                        thread.title = title.clone();
                    }

                    let title_clone = title.clone();
                    let thread_id_clone = *thread_id;
                    cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                        // Update thread list for this agent
                        crate::state::dispatch_global_message(
                            crate::messages::Message::RequestThreadListUpdate(agent_id),
                        );

                        // Update title UI if this is the current thread
                        if let Some(current_agent_state) = crate::state::APP_STATE
                            .with(|s| s.borrow().get_agent_state(agent_id).map(|as_| as_.clone()))
                        {
                            if current_agent_state.current_thread_id == Some(thread_id_clone) {
                                crate::state::dispatch_global_message(
                                    crate::messages::Message::UpdateThreadTitleUI(
                                        title_clone.clone(),
                                    ),
                                );
                            }
                        }
                    })));
                }
            }

            // Send update to backend
            cmds.push(crate::messages::Command::UpdateThreadTitle {
                thread_id: *thread_id,
                title: title.clone(),
            });
            true
        }
        crate::messages::Message::UpdateThreadList(threads, current_thread_id, thread_messages) => {
            if let Some(document) = web_sys::window()
                .expect("no global window exists")
                .document()
            {
                let _ = crate::components::chat_view::update_thread_list_ui(
                    &document,
                    &threads,
                    *current_thread_id,
                    &thread_messages,
                );
            }
            true
        }
        crate::messages::Message::UpdateConversation(messages) => {
            let messages_clone = messages.clone();
            cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                if let Some(document) = web_sys::window().unwrap().document() {
                    let current_user_opt =
                        crate::state::APP_STATE.with(|s| s.borrow().current_user.clone());
                    let _ = crate::components::chat_view::update_conversation_ui(
                        &document,
                        &messages_clone,
                        current_user_opt.as_ref(),
                    );
                }
            })));
            true
        }
        crate::messages::Message::UpdateThreadTitleUI(title) => {
            let title_clone = title.clone();
            cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                if let Some(document) = web_sys::window()
                    .expect("no global window exists")
                    .document()
                {
                    let _ = crate::components::chat_view::update_thread_title_with_data(
                        &document,
                        &title_clone,
                    );
                }
            })));
            true
        }
        crate::messages::Message::RequestUpdateThreadTitle(title) => {
            // Use current agent's current thread
            if let Some(agent_state) = state.current_agent() {
                if let Some(thread_id) = agent_state.current_thread_id {
                    let title_clone = title.clone();
                    cmds.push(crate::messages::Command::SendMessage(
                        crate::messages::Message::UpdateThreadTitle(thread_id, title_clone),
                    ));
                }
            }
            true
        }
        crate::messages::Message::RequestThreadListUpdate(agent_id) => {
            // Use agent-scoped state only
            let threads: Vec<crate::models::ApiThread> =
                if let Some(agent_state) = state.get_agent_state(*agent_id) {
                    agent_state
                        .get_threads_sorted()
                        .into_iter()
                        .cloned()
                        .collect()
                } else {
                    // No agent state found - return empty list
                    Vec::new()
                };

            let current_thread_id = if let Some(agent_state) = state.get_agent_state(*agent_id) {
                agent_state.current_thread_id
            } else {
                None
            };

            let thread_messages = if let Some(agent_state) = state.get_agent_state(*agent_id) {
                agent_state.thread_messages.clone()
            } else {
                std::collections::HashMap::new()
            };

            cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                crate::state::dispatch_global_message(crate::messages::Message::UpdateThreadList(
                    threads,
                    current_thread_id,
                    thread_messages,
                ));
            })));
            true
        }
        crate::messages::Message::ReceiveStreamChunk {
            thread_id,
            content,
            chunk_type,
            tool_name,
            tool_call_id,
            message_id,
        } => {
            // (migrated logic from update.rs)
            if chunk_type.as_deref() == Some("assistant_token") {
                state.token_mode_threads.insert(*thread_id);

                // Find which agent owns this thread
                let mut owning_agent_id = None;
                for (agent_id, agent_state) in &state.agent_states {
                    if agent_state.threads.contains_key(thread_id) {
                        owning_agent_id = Some(*agent_id);
                        break;
                    }
                }

                if let Some(agent_id) = owning_agent_id {
                    let is_streaming = state.streaming_threads.contains(thread_id);
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        let messages = agent_state.thread_messages.entry(*thread_id).or_default();
                        let need_new_bubble = match messages.last() {
                            Some(last) => last.role != "assistant" || !is_streaming,
                            None => true,
                        };
                        if need_new_bubble {
                            let now = chrono::Utc::now().to_rfc3339();
                            messages.push(crate::models::ApiThreadMessage {
                                id: None,
                                thread_id: *thread_id,
                                role: "assistant".to_string(),
                                content: content.clone(),
                                timestamp: Some(now),
                                message_type: Some("assistant_message".to_string()),
                                tool_name: None,
                                tool_call_id: None,
                                tool_input: None,
                                parent_id: None,
                            });
                        } else if let Some(last) = messages.last_mut() {
                            last.content.push_str(content);
                        }
                    }
                } else {
                    web_sys::console::warn_1(
                        &format!(
                            "ReceiveStreamChunk: No agent found owning thread {}",
                            thread_id
                        )
                        .into(),
                    );
                }
            } else if chunk_type.as_deref() == Some("tool_output") {
                let now = chrono::Utc::now().to_rfc3339();
                let parent_id = state.current_assistant_id(*thread_id);
                let tool_message = crate::models::ApiThreadMessage {
                    id: None,
                    thread_id: *thread_id,
                    role: "tool".to_string(),
                    content: content.clone(),
                    timestamp: Some(now),
                    message_type: chunk_type.clone(),
                    tool_name: tool_name.clone(),
                    tool_call_id: tool_call_id.clone(),
                    tool_input: None,
                    parent_id,
                };

                // Find which agent owns this thread
                let mut owning_agent_id = None;
                for (agent_id, agent_state) in &state.agent_states {
                    if agent_state.threads.contains_key(thread_id) {
                        owning_agent_id = Some(*agent_id);
                        break;
                    }
                }

                if let Some(agent_id) = owning_agent_id {
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        let messages = agent_state.thread_messages.entry(*thread_id).or_default();
                        messages.push(tool_message);
                        debug_log!(
                            "Added tool message for thread {}: {}",
                            thread_id, content
                        );
                    }
                } else {
                    web_sys::console::warn_1(
                        &format!(
                            "ReceiveStreamChunk (tool_output): No agent found owning thread {}",
                            thread_id
                        )
                        .into(),
                    );
                }
            } else if chunk_type.as_deref() == Some("assistant_message") {
                // Non-streaming assistant message - should have message_id
                debug_log!(
                    "Processing assistant_message chunk with message_id: {:?}",
                    message_id
                );

                let mid_u32 = message_id.as_ref().and_then(|s| s.parse::<u32>().ok());

                // Find which agent owns this thread
                let mut owning_agent_id = None;
                for (agent_id, agent_state) in &state.agent_states {
                    if agent_state.threads.contains_key(thread_id) {
                        owning_agent_id = Some(*agent_id);
                        break;
                    }
                }

                if let Some(agent_id) = owning_agent_id {
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        let messages = agent_state.thread_messages.entry(*thread_id).or_default();
                        let now = chrono::Utc::now().to_rfc3339();
                        let assistant_message = crate::models::ApiThreadMessage {
                            id: mid_u32,
                            thread_id: *thread_id,
                            role: "assistant".to_string(),
                            content: content.clone(),
                            timestamp: Some(now),
                            message_type: Some("assistant_message".to_string()),
                            tool_name: None,
                            tool_call_id: None,
                            tool_input: None,
                            parent_id: None,
                        };
                        messages.push(assistant_message);
                        debug_log!(
                            "Added assistant_message for thread {}: {}",
                            thread_id, content
                        );
                    }
                } else {
                    web_sys::console::warn_1(&format!("ReceiveStreamChunk (assistant_message): No agent found owning thread {}", thread_id).into());
                }
            } else {
                let mid_u32 = message_id.as_ref().and_then(|s| s.parse::<u32>().ok());
                let current_mid = state.current_assistant_id(*thread_id);
                let start_new = current_mid != mid_u32;

                // Find which agent owns this thread
                let mut owning_agent_id = None;
                for (agent_id, agent_state) in &state.agent_states {
                    if agent_state.threads.contains_key(thread_id) {
                        owning_agent_id = Some(*agent_id);
                        break;
                    }
                }

                if let Some(agent_id) = owning_agent_id {
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        let messages = agent_state.thread_messages.entry(*thread_id).or_default();
                        if start_new {
                            debug_log!("Update: starting NEW assistant bubble");
                            let now = chrono::Utc::now().to_rfc3339();
                            let assistant_message = crate::models::ApiThreadMessage {
                                id: mid_u32,
                                thread_id: *thread_id,
                                role: "assistant".to_string(),
                                content: content.clone(),
                                timestamp: Some(now),
                                message_type: chunk_type.clone(),
                                tool_name: None,
                                tool_call_id: None,
                                tool_input: None,
                                parent_id: None,
                            };
                            messages.push(assistant_message);
                            state.active_streams.insert(*thread_id, mid_u32);
                        } else if let Some(last_message) = messages.last_mut() {
                            last_message.content.push_str(content);
                        }
                    }
                } else {
                    web_sys::console::warn_1(
                        &format!(
                            "ReceiveStreamChunk (assistant): No agent found owning thread {}",
                            thread_id
                        )
                        .into(),
                    );
                }
            }
            // Update UI if this is the current agent's current thread
            if let Some(agent_state) = state.current_agent() {
                if agent_state.current_thread_id == Some(*thread_id) {
                    if let Some(messages) = agent_state.thread_messages.get(thread_id) {
                        debug_log!(
                            "[DEBUG] Scheduling UI update after ReceiveStreamChunk for thread {} ({} messages)",
                            thread_id,
                            messages.len()
                        );
                        let messages_clone = messages.clone();
                        cmds.push(Command::UpdateUI(Box::new(move || {
                            crate::state::dispatch_global_message(
                                crate::messages::Message::UpdateConversation(messages_clone),
                            );
                        })));
                    }
                }
            }
            true
        }
        crate::messages::Message::ReceiveStreamStart(thread_id) => {
            state.streaming_threads.insert(*thread_id);
            state.active_streams.insert(*thread_id, None);
            debug_log!("Stream started for thread {}.", thread_id);
            true
        }
        crate::messages::Message::ReceiveStreamEnd(thread_id) => {
            state.streaming_threads.remove(thread_id);
            state.token_mode_threads.remove(thread_id);

            // Find which agent owns this thread
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                let current_agent_id = state.current_agent_id;
                let mut should_update_ui = false;
                let mut messages_for_ui = None;

                if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                    if let Some(messages) = agent_state.thread_messages.get_mut(thread_id) {
                        if let Some(last_user_message) =
                            messages.iter_mut().filter(|msg| msg.role == "user").last()
                        {
                            last_user_message.id = None;
                            debug_log!(
                                "Stream ended: Set last user message ID to None for thread {}.",
                                thread_id
                            );
                        }

                        // Check if UI should be updated
                        if current_agent_id == Some(agent_id)
                            && agent_state.current_thread_id == Some(*thread_id)
                        {
                            should_update_ui = true;
                            messages_for_ui = Some(messages.clone());
                        }
                    }
                }

                if should_update_ui {
                    if let Some(messages_clone) = messages_for_ui {
                        cmds.push(Command::UpdateUI(Box::new(move || {
                            crate::state::dispatch_global_message(
                                crate::messages::Message::UpdateConversation(messages_clone),
                            );
                        })));
                    }
                }
            }
            debug_log!("Stream ended for thread {}.", thread_id);
            true
        }
        crate::messages::Message::ReceiveAssistantId {
            thread_id,
            message_id,
        } => {
            debug_log!(
                "Received AssistantId {} for thread {}",
                message_id, thread_id
            );
            state.active_streams.insert(*thread_id, Some(*message_id));

            // Find which agent owns this thread
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                    if let Some(messages) = agent_state.thread_messages.get_mut(thread_id) {
                        if let Some(last) = messages
                            .iter_mut()
                            .rev()
                            .find(|m| m.role == "assistant" && m.id.is_none())
                        {
                            last.id = Some(*message_id);
                        }
                    }
                }
            }
            true
        }
        crate::messages::Message::ReceiveNewMessage(message) => {
            let thread_id = message.thread_id;

            // Find which agent owns this thread and update agent-scoped state
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(&thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                let current_agent_id = state.current_agent_id;
                let should_update_ui =
                    if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                        let messages = agent_state.thread_messages.entry(thread_id).or_default();
                        messages.push(message.clone());

                        // Check if UI should be updated
                        current_agent_id == Some(agent_id)
                            && agent_state.current_thread_id == Some(thread_id)
                    } else {
                        false
                    };

                if should_update_ui {
                    // Get messages for UI update in a separate borrow
                    if let Some(agent_state) = state.get_agent_state(agent_id) {
                        if let Some(messages) = agent_state.thread_messages.get(&thread_id) {
                            let messages_clone = messages.clone();
                            cmds.push(Command::UpdateUI(Box::new(move || {
                                crate::state::dispatch_global_message(
                                    crate::messages::Message::UpdateConversation(messages_clone),
                                );
                            })));
                        }
                    }
                }

                // Update thread list for this agent
                cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::RequestThreadListUpdate(agent_id),
                    );
                })));
            } else {
                web_sys::console::warn_1(
                    &format!(
                        "ReceiveNewMessage: No agent found owning thread {}",
                        thread_id
                    )
                    .into(),
                );
            }
            true
        }
        crate::messages::Message::ReceiveThreadUpdate { thread_id, title } => {
            // Find which agent owns this thread and update agent-scoped state
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                let current_agent_id = state.current_agent_id;
                let mut should_update_ui = false;
                let mut title_for_ui = String::new();

                if let Some(agent_state) = state.get_agent_state_mut(agent_id) {
                    if let Some(thread) = agent_state.threads.get_mut(thread_id) {
                        if let Some(new_title) = title {
                            thread.title = new_title.clone();
                        }

                        // Check if UI should be updated
                        if current_agent_id == Some(agent_id)
                            && agent_state.current_thread_id == Some(*thread_id)
                        {
                            should_update_ui = true;
                            title_for_ui = thread.title.clone();
                        }
                    }
                }

                if should_update_ui {
                    cmds.push(Command::UpdateUI(Box::new(move || {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::UpdateThreadTitleUI(title_for_ui),
                        );
                    })));
                }
            }
            true
        }
        crate::messages::Message::ReceiveThreadHistory(messages) => {
            let system_messages_count = messages.iter().filter(|msg| msg.role == "system").count();
            if system_messages_count > 0 {
                debug_log!(
                    "Thread history contains {} system messages which won't be displayed in the chat UI",
                    system_messages_count
                );
            }
            debug_log!(
                "Update handler: Received thread history ({} messages, {} displayable)",
                messages.len(),
                messages.len() - system_messages_count
            );

            // Use current agent's current thread
            if let Some(agent_state) = state.current_agent() {
                if let Some(active_thread_id) = agent_state.current_thread_id {
                    // Update agent-scoped thread messages
                    if let Some(agent_state_mut) = state.current_agent_mut() {
                        agent_state_mut
                            .thread_messages
                            .insert(active_thread_id, messages.clone());
                    }

                    let messages_clone_for_dispatch = messages.clone();
                    cmds.push(Command::UpdateUI(Box::new(move || {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::UpdateConversation(
                                messages_clone_for_dispatch,
                            ),
                        );
                    })));
                } else {
                    web_sys::console::warn_1(
                        &"Received thread history but no active thread selected in current agent."
                            .into(),
                    );
                }
            } else {
                web_sys::console::warn_1(
                    &"Received thread history but no active agent selected.".into(),
                );
            }
            true
        }
        crate::messages::Message::UpdateLoadingState(is_loading) => {
            state.is_chat_loading = *is_loading;
            if let Some(document) = web_sys::window()
                .expect("no global window exists")
                .document()
            {
                let _ = crate::components::chat_view::update_loading_state(&document, *is_loading);
            }
            true
        }
        crate::messages::Message::NavigateToThreadView(thread_id) => {
            // Find which agent owns this thread
            let mut owning_agent_id = None;
            for (agent_id, agent_state) in &state.agent_states {
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    break;
                }
            }

            if let Some(agent_id) = owning_agent_id {
                // Navigate to agent chat and select the thread
                crate::state::dispatch_global_message(
                    crate::messages::Message::NavigateToAgentChat(agent_id),
                );
                crate::state::dispatch_global_message(
                    crate::messages::Message::SelectAgentThread {
                        agent_id,
                        thread_id: *thread_id,
                    },
                );
            } else {
                web_sys::console::error_1(
                    &format!("Thread {} not found in any agent state", thread_id).into(),
                );
            }
            true
        }
        crate::messages::Message::RequestNewThread => {
            let agent_id_opt = state.current_agent_id;
            debug_log!("RequestNewThread - agent_id: {:?}", agent_id_opt);
            if let Some(agent_id) = agent_id_opt {
                let title = crate::constants::DEFAULT_THREAD_TITLE.to_string();
                debug_log!("Creating new thread for agent: {}", agent_id);
                cmds.push(crate::messages::Command::SendMessage(
                    crate::messages::Message::CreateThread(agent_id, title),
                ));
            } else {
                web_sys::console::error_1(&"Cannot create thread: No agent selected".into());
            }
            true
        }
        crate::messages::Message::RequestSendMessage(content) => {
            if let Some(agent_id) = state.current_agent_id {
                if let Some(agent_state) = state.get_agent_state(agent_id) {
                    if let Some(thread_id) = agent_state.current_thread_id {
                        debug_log!("Sending message to thread {}: {}", thread_id, content);

                        // Dispatch SendThreadMessage to trigger optimistic UI update
                        cmds.push(crate::messages::Command::SendMessage(
                            crate::messages::Message::SendThreadMessage(thread_id, content.clone()),
                        ));
                    } else {
                        web_sys::console::error_1(
                            &"RequestSendMessage but no current thread selected".into(),
                        );
                    }
                } else {
                    web_sys::console::error_1(
                        &"RequestSendMessage but no agent state found".into(),
                    );
                }
            } else {
                web_sys::console::error_1(
                    &"RequestSendMessage but no current agent selected".into(),
                );
            }
            true
        }
        crate::messages::Message::SendThreadMessage(thread_id, content) => {
            debug_log!(
                "🔍 [DEBUG] Processing SendThreadMessage for thread {}: {}",
                thread_id, content
            );

            // Find which agent owns this thread for optimistic UI update
            let mut owning_agent_id = None;
            debug_log!("🔍 [DEBUG] Looking for agent owning thread {}", thread_id);
            for (agent_id, agent_state) in &state.agent_states {
                debug_log!(
                    "🔍 [DEBUG] Checking agent {} with {} threads",
                    agent_id,
                    agent_state.threads.len()
                );
                if agent_state.threads.contains_key(thread_id) {
                    owning_agent_id = Some(*agent_id);
                    debug_log!("🔍 [DEBUG] Found owning agent: {}", agent_id);
                    break;
                }
            }

            if owning_agent_id.is_none() {
                web_sys::console::warn_1(
                    &format!(
                        "🔍 [DEBUG] WARNING: No agent found owning thread {}",
                        thread_id
                    )
                    .into(),
                );
            }

            // Add optimistic message to agent-scoped state
            if let Some(agent_id) = owning_agent_id {
                let current_agent_id_opt = state.current_agent_id;
                let should_update_ui = if let Some(agent_state) =
                    state.get_agent_state_mut(agent_id)
                {
                    // Create optimistic user message with unique high ID
                    let existing_messages = agent_state
                        .thread_messages
                        .entry(*thread_id)
                        .or_insert_with(Vec::new);
                    let optimistic_id = 1000000 + existing_messages.len() as u32; // High IDs for optimistic messages

                    let optimistic_message = crate::models::ApiThreadMessage {
                        id: Some(optimistic_id), // High ID that won't conflict with normal backend IDs
                        thread_id: *thread_id,
                        role: "user".to_string(),
                        content: content.clone(),
                        timestamp: Some(js_sys::Date::new_0().to_iso_string().as_string().unwrap()),
                        message_type: None,
                        tool_name: None,
                        tool_call_id: None,
                        tool_input: None,
                        parent_id: None,
                    };

                    // Add to agent's thread messages
                    agent_state
                        .thread_messages
                        .entry(*thread_id)
                        .or_insert_with(Vec::new)
                        .push(optimistic_message);

                    // Check if UI should be updated
                    current_agent_id_opt == Some(agent_id)
                        && agent_state.current_thread_id == Some(*thread_id)
                } else {
                    false
                };

                // Trigger UI update if needed
                if should_update_ui {
                    debug_log!("🔍 [DEBUG] Triggering UI update for agent {}", agent_id);
                    let current_agent_id = agent_id;
                    cmds.push(crate::messages::Command::UpdateUI(Box::new(move || {
                        debug_log!(
                            "🔍 [DEBUG] Executing UI refresh for agent {}",
                            current_agent_id
                        );
                        crate::components::chat_view::refresh_chat_ui_from_agent_state(
                            current_agent_id,
                        );
                    })));
                } else {
                    debug_log!("🔍 [DEBUG] Skipping UI update - not current agent/thread");
                }
            }

            // Send to backend
            cmds.push(crate::messages::Command::SendThreadMessage {
                thread_id: *thread_id,
                content: content.clone(),
                client_id: None,
            });
            true
        }
        crate::messages::Message::ThreadMessageSent(_response, _client_id) => {
            web_sys::console::error_1(
                &"ThreadMessageSent is fully deprecated and should not be used".into(),
            );
            true
        }
        crate::messages::Message::ThreadMessageFailed(_thread_id, _client_id) => {
            web_sys::console::error_1(
                &"ThreadMessageFailed is fully deprecated and should not be used".into(),
            );
            // Remove functionality - this should be handled by proper error handling in commands
            true
        }
        crate::messages::Message::LoadThreads(agent_id) => {
            debug_log!("Loading threads for agent: {}", agent_id);
            cmds.push(crate::messages::Command::FetchThreads(*agent_id));
            true
        }
        crate::messages::Message::ThreadsLoaded(_threads) => {
            web_sys::console::error_1(
                &"DEPRECATED: ThreadsLoaded should not be used. Use AgentThreadsLoaded instead."
                    .into(),
            );
            // No longer redirecting - this should be fixed at the source
            true
        }

        // NEW: Agent-Scoped Thread Handlers
        crate::messages::Message::NavigateToAgentChat(agent_id) => {
            debug_log!("NavigateToAgentChat: agent_id={}", agent_id);

            // Set up chat view and clean agent state
            state.active_view = crate::storage::ActiveView::ChatView;
            state.is_chat_loading = true;
            state.set_current_agent(*agent_id);

            let agent_id_for_effects = *agent_id;
            cmds.push(Command::UpdateUI(Box::new(move || {
                if let Some(document) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::chat_view::setup_chat_view(&document);
                    let _ = crate::components::chat_view::show_chat_view(
                        &document,
                        agent_id_for_effects,
                    );
                }

                // Dispatch agent-scoped thread loading
                crate::state::dispatch_global_message(crate::messages::Message::LoadAgentThreads(
                    agent_id_for_effects,
                ));
            })));
            true
        }

        crate::messages::Message::LoadAgentThreads(agent_id) => {
            debug_log!("LoadAgentThreads: agent_id={}", agent_id);

            let agent_id_for_fetch = *agent_id;
            wasm_bindgen_futures::spawn_local(async move {
                match crate::network::api_client::ApiClient::get_threads(Some(agent_id_for_fetch))
                    .await
                {
                    Ok(response) => {
                        match serde_json::from_str::<Vec<crate::models::ApiThread>>(&response) {
                            Ok(threads) => {
                                crate::state::dispatch_global_message(
                                    crate::messages::Message::AgentThreadsLoaded {
                                        agent_id: agent_id_for_fetch,
                                        threads,
                                    },
                                );
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!(
                                        "Failed to parse threads for agent {}: {:?}",
                                        agent_id_for_fetch, e
                                    )
                                    .into(),
                                );
                            }
                        }
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!(
                                "Failed to load threads for agent {}: {:?}",
                                agent_id_for_fetch, e
                            )
                            .into(),
                        );
                    }
                }
            });
            true
        }

        crate::messages::Message::AgentThreadsLoaded { agent_id, threads } => {
            debug_log!(
                "AgentThreadsLoaded: agent_id={}, {} threads",
                agent_id,
                threads.len()
            );

            // Get or create agent state
            let agent_state = state.ensure_agent_state(*agent_id);

            // Clear existing threads and load new ones (clean slate)
            agent_state.clear_threads();

            // Add new threads
            for thread in threads {
                if let Some(thread_id) = thread.id {
                    agent_state.threads.insert(thread_id, thread.clone());
                }
            }

            // Auto-select the first thread if none is selected
            let mut selected_thread_id = None;
            if agent_state.current_thread_id.is_none() && !agent_state.threads.is_empty() {
                let threads_sorted = agent_state.get_threads_sorted();
                if let Some(first_thread) = threads_sorted.first() {
                    if let Some(thread_id) = first_thread.id {
                        agent_state.current_thread_id = Some(thread_id);
                        selected_thread_id = Some(thread_id);
                    }
                }
            } else if let Some(thread_id) = agent_state.current_thread_id {
                selected_thread_id = Some(thread_id);
            }

            // Update UI - agent-scoped state is already updated above
            state.is_chat_loading = false;

            // Trigger UI refresh and select the thread to set up WebSocket subscriptions
            let current_agent_id = *agent_id;
            cmds.push(Command::UpdateUI(Box::new(move || {
                crate::components::chat_view::refresh_chat_ui_from_agent_state(current_agent_id);
            })));

            // Automatically select the thread to set up proper WebSocket subscriptions
            if let Some(thread_id) = selected_thread_id {
                debug_log!(
                    "Auto-selecting thread {} for agent {} to set up WebSocket subscriptions",
                    thread_id, agent_id
                );
                cmds.push(crate::messages::Command::SendMessage(
                    crate::messages::Message::SelectAgentThread {
                        agent_id: *agent_id,
                        thread_id,
                    },
                ));
            }
            true
        }

        crate::messages::Message::SelectAgentThread {
            agent_id,
            thread_id,
        } => {
            debug_log!(
                "SelectAgentThread: agent_id={}, thread_id={}",
                agent_id, thread_id
            );

            if let Some(agent_state) = state.get_agent_state_mut(*agent_id) {
                agent_state.current_thread_id = Some(*thread_id);

                // Update UI and set up WebSocket subscriptions
                let current_agent_id = *agent_id;
                let current_thread_id = *thread_id;
                cmds.push(Command::UpdateUI(Box::new(move || {
                    // Set up WebSocket subscriptions for the selected thread
                    crate::state::APP_STATE.with(|state_cell| {
                        let state = state_cell.borrow();
                        let topic_manager = state.topic_manager.clone();

                        if let Err(e) = crate::components::chat::init_chat_view_ws(
                            current_thread_id,
                            topic_manager,
                        ) {
                            web_sys::console::error_1(
                                &format!(
                                    "Failed to initialize WebSocket for thread {}: {:?}",
                                    current_thread_id, e
                                )
                                .into(),
                            );
                        }
                    });

                    crate::state::dispatch_global_message(
                        crate::messages::Message::RequestThreadListUpdate(current_agent_id),
                    );
                    crate::state::dispatch_global_message(
                        crate::messages::Message::LoadThreadMessages(current_thread_id),
                    );
                })));
            }
            true
        }

        _ => false,
    }
}
