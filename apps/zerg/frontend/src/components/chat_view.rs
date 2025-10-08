use serde_json;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Event, HtmlInputElement};

use std::collections::HashMap;

use crate::dom_utils;
use crate::messages::Message;
use crate::models::{ApiThread, ApiThreadMessage};
use crate::state::dispatch_global_message;

// Main function to setup the chat view
pub fn setup_chat_view(document: &Document) -> Result<(), JsValue> {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            if let Ok(flag) = storage.get_item("zerg_use_react_chat") {
                if flag.as_deref() == Some("1") {
                    let base_url = storage
                        .get_item("zerg_react_chat_base")
                        .ok()
                        .flatten()
                        .or_else(|| window.location().origin().ok().map(|origin| format!("{origin}/chat")));

                    if let Some(mut target) = base_url {
                        let (agent_id, thread_id) = crate::state::APP_STATE.with(|state_cell| {
                            let state = state_cell.borrow();
                            let agent_id = state.current_agent_id;
                            let thread_id = agent_id.and_then(|id| {
                                state
                                    .agent_states
                                    .get(&id)
                                    .and_then(|agent_state| agent_state.current_thread_id)
                            });
                            (agent_id, thread_id)
                        });

                        if let Some(agent_id_val) = agent_id {
                            if !target.ends_with('/') {
                                target.push('/');
                            }
                            target.push_str(&agent_id_val.to_string());

                            if let Some(thread_id_val) = thread_id {
                                target.push('/');
                                target.push_str(&thread_id_val.to_string());
                            }
                        }

                        if let Err(err) = window.location().set_href(&target) {
                            web_sys::console::warn_1(&format!("Failed to redirect to React chat: {:?}", err).into());
                        }
                        return Ok(());
                    } else {
                        web_sys::console::warn_1(&"React chat flag enabled but no base URL configured".into());
                    }
                }
            }
        }
    }

    // Create the chat view container if it doesn't exist
    if document.get_element_by_id("chat-view-container").is_none() {
        let chat_container = document.create_element("div")?;
        chat_container.set_id("chat-view-container");
        chat_container.set_class_name("chat-view-container");

        // Make chat container visible by default (E2E tests expect it)

        // Add the chat layout structure
        chat_container.set_inner_html(r#"
            <div class="chat-header">
                <div class="back-button">←</div>
                <div class="agent-info">
                    <div class="agent-name"></div>
                    <span class="thread-title-label">Thread: </span><span class="thread-title-text"></span>
                </div>
            </div>
            <div class="chat-body">
                <div class="thread-sidebar">
                    <div class="sidebar-header">
                        <h3>Threads</h3>
                        <button class="new-thread-btn" data-testid="new-thread-btn">New Thread</button>
                    </div>
                    <div class="thread-list"></div>
                </div>
                <div class="conversation-area">
                    <div class="messages-container"></div>
                </div>
            </div>
            <div class="chat-input-area">
            <input type="text" class="chat-input" placeholder="Type your message..." data-testid="chat-input">
            <button class="send-button" data-testid="send-message-btn">Send</button>
            </div>
        "#);

        // Add the chat container to the app container
        let app_container = document
            .get_element_by_id("app-container")
            .ok_or(JsValue::from_str("Could not find app-container"))?;

        app_container.append_child(&chat_container)?;

        // Setup event handlers
        setup_chat_event_handlers(document)?;
    }

    Ok(())
}

// Setup event handlers for the chat view
fn setup_chat_event_handlers(document: &Document) -> Result<(), JsValue> {
    // Back button handler
    if let Some(back_button) = document.query_selector(".back-button")? {
        let back_handler = Closure::wrap(Box::new(move |_: Event| {
            dispatch_global_message(Message::NavigateToDashboard);
        }) as Box<dyn FnMut(_)>);

        back_button
            .add_event_listener_with_callback("click", back_handler.as_ref().unchecked_ref())?;
        back_handler.forget();
    }

    // New thread button handler
    if let Some(new_thread_btn) = document.query_selector(".new-thread-btn")? {
        let document_clone = document.clone();
        let new_thread_handler = Closure::wrap(Box::new(move |_: Event| {
            // ------------------------------------------------------------------
            // Minimal modal for thread title input (E2E test compatibility)
            // ------------------------------------------------------------------
            if document_clone
                .get_element_by_id("new-thread-modal")
                .is_some()
            {
                return; // Already open
            }

            let modal = document_clone.create_element("div").unwrap();
            modal.set_id("new-thread-modal");
            modal.set_class_name("new-thread-modal");

            // Simple styles (inline) – translucent backdrop & centering
            modal.set_attribute("style", "position:fixed;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.4);").ok();

            modal.set_inner_html(r#"
                <div style="background:#fff;padding:20px;border-radius:8px;min-width:300px;display:flex;flex-direction:column;gap:10px;">
                    <input type="text" class="thread-title-input" data-testid="thread-title-input" placeholder="Thread title" style="padding:8px;border:1px solid #ccc;border-radius:4px;" />
                    <button class="create-thread-confirm" data-testid="create-thread-confirm" style="padding:8px 12px;">Create</button>
                </div>
            "#);

            document_clone.body().unwrap().append_child(&modal).unwrap();

            // Attach click handler for confirm
            if let Some(confirm_btn) = modal.query_selector(".create-thread-confirm").unwrap() {
                let modal_clone = modal.clone();
                let doc_for_btn = document_clone.clone();
                let handler = Closure::wrap(Box::new(move |_: Event| {
                    if let Some(input_el) = modal_clone
                        .query_selector(".thread-title-input")
                        .ok()
                        .flatten()
                    {
                        let input = input_el.dyn_into::<HtmlInputElement>().unwrap();
                        let title = input.value();

                        // Fallback title if empty
                        let used_title = if title.trim().is_empty() {
                            "Untitled Thread".to_string()
                        } else {
                            title
                        };

                        // Determine current agent_id from global state
                        let mut agent_id_opt: Option<u32> = None;
                        crate::state::APP_STATE.with(|state_cell| {
                            agent_id_opt = state_cell.borrow().current_agent_id;
                        });

                        if let Some(agent_id) = agent_id_opt {
                            dispatch_global_message(Message::CreateThread(agent_id, used_title));
                        } else {
                            // ------------------------------------------------------------------
                            // No agent yet – create a throwaway one so tests can proceed
                            // ------------------------------------------------------------------
                            let title_clone2 = used_title.clone();

                            wasm_bindgen_futures::spawn_local(async move {
                                use crate::network::api_client::ApiClient;

                                let payload = r#"{\"name\": \"E2E Agent\", \"system_instructions\": \"\", \"task_instructions\": \"\", \"model\": \"gpt-4o\"}"#;

                                match ApiClient::create_agent(payload).await {
                                    Ok(json_str) => {
                                        if let Ok(agent) =
                                            serde_json::from_str::<crate::models::ApiAgent>(
                                                &json_str,
                                            )
                                        {
                                            if let Some(new_id) = agent.id {
                                                crate::state::APP_STATE.with(|cell| {
                                                    let mut s = cell.borrow_mut();
                                                    s.current_agent_id = Some(new_id);
                                                    s.agents.insert(new_id, agent.clone());
                                                });

                                                dispatch_global_message(Message::CreateThread(
                                                    new_id,
                                                    title_clone2,
                                                ));
                                            }
                                        }
                                    }
                                    Err(e) => web_sys::console::error_1(
                                        &format!("Failed to auto-create agent: {:?}", e).into(),
                                    ),
                                }
                            });
                        }

                        // Remove modal
                        doc_for_btn.body().unwrap().remove_child(&modal_clone).ok();
                    }
                }) as Box<dyn FnMut(_)>);

                confirm_btn
                    .add_event_listener_with_callback("click", handler.as_ref().unchecked_ref())
                    .unwrap();
                handler.forget();
            }
        }) as Box<dyn FnMut(_)>);

        new_thread_btn.add_event_listener_with_callback(
            "click",
            new_thread_handler.as_ref().unchecked_ref(),
        )?;
        new_thread_handler.forget();
    }

    // Send message button handler
    if let Some(send_button) = document.query_selector(".send-button")? {
        let document_clone = document.clone();
        let send_handler = Closure::wrap(Box::new(move |_: Event| {
            if let Some(input_el) = document_clone.query_selector(".chat-input").ok().flatten() {
                let input = input_el.dyn_into::<HtmlInputElement>().unwrap();
                let message = input.value();

                if !message.is_empty() {
                    // Request to send message to current thread
                    dispatch_global_message(Message::RequestSendMessage(message.clone()));
                    input.set_value("");
                }
            }
        }) as Box<dyn FnMut(_)>);

        send_button
            .add_event_listener_with_callback("click", send_handler.as_ref().unchecked_ref())?;
        send_handler.forget();
    }

    // Chat input key press handler
    if let Some(input_el) = document.query_selector(".chat-input")? {
        let document_clone = document.clone();
        let keypress_handler = Closure::wrap(Box::new(move |e: web_sys::KeyboardEvent| {
            if e.key() == "Enter" {
                e.prevent_default();

                let input = document_clone
                    .query_selector(".chat-input")
                    .ok()
                    .flatten()
                    .and_then(|el| el.dyn_into::<HtmlInputElement>().ok())
                    .unwrap();

                let message = input.value();

                if !message.is_empty() {
                    // Request to send message to current thread
                    dispatch_global_message(Message::RequestSendMessage(message.clone()));
                    input.set_value("");
                }
            }
        }) as Box<dyn FnMut(_)>);

        input_el.add_event_listener_with_callback(
            "keypress",
            keypress_handler.as_ref().unchecked_ref(),
        )?;
        keypress_handler.forget();
    }

    Ok(())
}

// Function to show the chat view
pub fn show_chat_view(document: &Document, agent_id: u32) -> Result<(), JsValue> {
    // Hide other views using the shared helper
    if let Some(dashboard) = document.get_element_by_id("dashboard-container") {
        dom_utils::hide(&dashboard);
    }

    if let Some(canvas) = document.get_element_by_id("canvas-container") {
        dom_utils::hide(&canvas);
    }

    // Show chat view
    if let Some(chat_view) = document.get_element_by_id("chat-view-container") {
        dom_utils::show(&chat_view);
    }

    // Instead of accessing APP_STATE, dispatch a message to update the thread list after the DOM is ready
    let agent_id_clone = agent_id;
    let update_thread_list = Closure::once(Box::new(move || {
        dispatch_global_message(Message::RequestThreadListUpdate(agent_id_clone));
    }) as Box<dyn FnOnce()>);
    web_sys::window()
        .expect("no global window exists")
        .set_timeout_with_callback_and_timeout_and_arguments_0(
            update_thread_list.as_ref().unchecked_ref(),
            50, // Small delay to ensure DOM is ready
        )?;
    update_thread_list.forget();

    // Schedule the dispatches to occur after the current function returns
    let agent_id_clone = agent_id;
    let load_agent_info = Closure::once(Box::new(move || {
        dispatch_global_message(Message::LoadAgentInfo(agent_id_clone));
    }) as Box<dyn FnOnce()>);

    let agent_id_clone = agent_id;
    let load_threads = Closure::once(Box::new(move || {
        dispatch_global_message(Message::LoadThreads(agent_id_clone));
    }) as Box<dyn FnOnce()>);

    web_sys::window()
        .expect("no global window exists")
        .set_timeout_with_callback_and_timeout_and_arguments_0(
            load_agent_info.as_ref().unchecked_ref(),
            0,
        )?;

    web_sys::window()
        .expect("no global window exists")
        .set_timeout_with_callback_and_timeout_and_arguments_0(
            load_threads.as_ref().unchecked_ref(),
            0,
        )?;

    // Prevent these closures from being garbage collected
    load_agent_info.forget();
    load_threads.forget();

    Ok(())
}

// Update agent info in the UI when received
#[allow(dead_code)]
pub fn update_agent_info(document: &Document, agent_name: &str) -> Result<(), JsValue> {
    if let Some(agent_name_el) = document.query_selector(".agent-name").ok().flatten() {
        agent_name_el.set_text_content(Some(agent_name));
    }
    Ok(())
}

// Update the thread list in the sidebar when thread data is received
pub fn update_thread_list_ui(
    document: &Document,
    threads: &[ApiThread],
    current_thread_id: Option<u32>,
    thread_messages: &HashMap<u32, Vec<ApiThreadMessage>>,
) -> Result<(), JsValue> {
    if let Some(thread_list) = document.query_selector(".thread-list").ok().flatten() {
        // Clear existing threads
        thread_list.set_inner_html("");

        // If no threads, show a message
        if threads.is_empty() {
            let empty_message = document.create_element("div")?;
            empty_message.set_class_name("thread-list-empty");
            empty_message.set_text_content(Some("No threads found"));
            thread_list.append_child(&empty_message)?;
            return Ok(());
        }

        // Sort threads by updated_at (newest first)
        let mut threads = threads.to_vec();
        threads.sort_by(|a, b| {
            b.updated_at
                .as_ref()
                .unwrap_or(&"".to_string())
                .cmp(a.updated_at.as_ref().unwrap_or(&"".to_string()))
        });

        // Create thread items
        for thread in threads {
            if let Some(thread_id) = thread.id {
                let thread_item = document.create_element("div")?;

                // Add selected class if this is the current thread
                if current_thread_id == Some(thread_id) {
                    thread_item.set_class_name("thread-item selected");
                } else {
                    thread_item.set_class_name("thread-item");
                }

                // Set data-id attribute for event handling
                thread_item.set_attribute("data-id", &thread_id.to_string())?;
                thread_item.set_attribute("data-testid", "thread-list-item")?;
                thread_item.set_attribute("data-thread-id", &thread_id.to_string())?;

                // Add thread information
                let title = document.create_element("div")?;
                title.set_class_name("thread-item-title");
                title.set_text_content(Some(&thread.title));

                let timestamp = document.create_element("div")?;
                timestamp.set_class_name("thread-item-time");
                timestamp.set_text_content(Some(&format_timestamp(
                    thread
                        .updated_at
                        .as_ref()
                        .unwrap_or(&thread.created_at.clone().unwrap_or_default()),
                )));

                // Add edit button for thread title
                let edit_button = document.create_element("div")?;
                edit_button.set_class_name("thread-edit-button");
                edit_button.set_text_content(Some("✎"));
                edit_button.set_attribute("aria-label", "Edit thread title")?;

                // Add preview of last message if available
                let preview = document.create_element("div")?;
                preview.set_class_name("thread-item-preview");

                if let Some(messages) = thread_messages.get(&thread_id) {
                    if !messages.is_empty() {
                        let last_message = messages.last().unwrap();
                        preview.set_text_content(Some(&truncate_text(&last_message.content, 50)));
                    } else {
                        preview.set_text_content(Some("No messages"));
                    }
                } else {
                    preview.set_text_content(Some("No messages"));
                }

                // Add elements to thread item
                thread_item.append_child(&title)?;
                thread_item.append_child(&timestamp)?;
                thread_item.append_child(&edit_button)?;
                thread_item.append_child(&preview)?;

                // Add click handler for thread selection
                let thread_id_clone = thread_id;
                let click_handler = Closure::wrap(Box::new(move |_: Event| {
                    dispatch_global_message(Message::SelectThread(thread_id_clone));
                }) as Box<dyn FnMut(_)>);

                thread_item.add_event_listener_with_callback(
                    "click",
                    click_handler.as_ref().unchecked_ref(),
                )?;
                click_handler.forget();

                // Add edit button click handler
                let thread_id_for_edit = thread_id;
                let title_for_edit = thread.title.clone();
                let edit_handler = Closure::wrap(Box::new(move |e: Event| {
                    e.stop_propagation();

                    // Fix the type mismatch by properly handling the Result before the Option
                    let new_title = web_sys::window()
                        .and_then(|w| {
                            match w.prompt_with_message_and_default(
                                "Edit thread title:",
                                &title_for_edit,
                            ) {
                                Ok(Some(title)) => Some(title),
                                _ => Some(title_for_edit.clone()), // Fall back to original title on error or cancel
                            }
                        })
                        .unwrap_or_else(|| title_for_edit.clone());

                    if !new_title.is_empty() {
                        dispatch_global_message(Message::UpdateThreadTitle(
                            thread_id_for_edit,
                            new_title,
                        ));
                    }
                }) as Box<dyn FnMut(_)>);

                edit_button.add_event_listener_with_callback(
                    "click",
                    edit_handler.as_ref().unchecked_ref(),
                )?;
                edit_handler.forget();

                // Add the thread item to the list
                thread_list.append_child(&thread_item)?;
            }
        }

        // Enable or disable chat input based on whether a thread is selected
        if let (Some(input), Some(button)) = (
            document.query_selector(".chat-input").ok().flatten(),
            document.query_selector(".send-button").ok().flatten(),
        ) {
            if current_thread_id.is_none() {
                if let Some(html_input) = input.dyn_ref::<web_sys::HtmlInputElement>() {
                    html_input.set_disabled(true);
                    html_input.set_placeholder("Select a thread to start chatting");
                }
                if let Some(btn_el) = button.dyn_ref::<web_sys::HtmlElement>() {
                    btn_el.set_attribute("disabled", "true").ok();
                    btn_el.set_class_name("send-button disabled");
                }
            } else {
                if let Some(html_input) = input.dyn_ref::<web_sys::HtmlInputElement>() {
                    html_input.set_disabled(false);
                    html_input.set_placeholder("Type your message...");
                }
                if let Some(btn_el) = button.dyn_ref::<web_sys::HtmlElement>() {
                    btn_el.remove_attribute("disabled").ok();
                    btn_el.set_class_name("send-button");
                }
            }
        }
    }

    Ok(())
}

// Update the conversation area with messages
use crate::models::CurrentUser;

pub fn update_conversation_ui(
    document: &Document,
    messages: &[ApiThreadMessage],
    current_user: Option<&CurrentUser>,
) -> Result<(), JsValue> {
    // DEBUG: Log message count and roles to help diagnose missing bubbles
    let roles: Vec<String> = messages.iter().map(|m| m.role.clone()).collect();
    crate::debug_log!(
        "[DEBUG] update_conversation_ui: {} messages, roles: {:?}",
        messages.len(),
        roles
    );
    if let Some(messages_container) = document
        .query_selector(".messages-container")
        .ok()
        .flatten()
    {
        // Clear existing messages
        messages_container.set_inner_html("");

        // ------------------------------------------------------------------
        // Stable chronological ordering - simplified
        // ------------------------------------------------------------------
        // Sort messages by ID (which corresponds to insertion order in the database)
        // Fall back to timestamp ordering only when IDs aren't available

        let mut sorted_messages = messages.to_vec();
        sorted_messages.sort_by(|a, b| {
            match (a.id, b.id) {
                (Some(aid), Some(bid)) => aid.cmp(&bid),
                _ => {
                    // Fallback to timestamp comparison if IDs aren't available
                    let a_time = a.timestamp.as_deref().unwrap_or("");
                    let b_time = b.timestamp.as_deref().unwrap_or("");
                    a_time.cmp(b_time)
                }
            }
        });

        // Build a map from parent_id to tool messages for grouping under the
        // originating assistant message.  We only include messages that have
        // a concrete parent_id – any *tool* messages **without** this field
        // will be rendered later as standalone bubbles so that useful output
        // is never silently dropped.
        let mut tool_messages_by_parent: std::collections::HashMap<u32, Vec<ApiThreadMessage>> =
            std::collections::HashMap::new();
        for msg in sorted_messages
            .iter()
            .filter(|m| m.role == "tool" && m.parent_id.is_some())
        {
            // Safe to unwrap because of the filter guard.
            let pid = msg.parent_id.unwrap();
            tool_messages_by_parent
                .entry(pid)
                .or_default()
                .push(msg.clone());
        }

        // Display thread messages, but filter out system messages
        for message in sorted_messages
            .iter()
            .filter(|m| m.role != "system" && m.role != "tool")
        {
            // DEBUG: Log each message about to be rendered
            crate::debug_log!(
                "[DEBUG] Rendering message: role={}, content={:?}",
                message.role, message.content
            );

            // If this assistant bubble has **no textual content**, we skip
            // rendering the bubble itself.  Any tool messages that reference
            // this assistant via `parent_id` will be rendered directly in
            // chronological order (see below) so the chat flow remains
            // intact without an empty placeholder line.

            if message.role == "assistant" && message.content.trim().is_empty() {
                if let Some(id) = message.id {
                    if let Some(tool_msgs) = tool_messages_by_parent.get(&id) {
                        for tool_msg in tool_msgs {
                            render_tool_message(document, messages_container.clone(), tool_msg)?;
                        }
                    }
                }
                continue;
            }
            // Root container (flex row when showing avatar)
            let row_container = document.create_element("div")?;
            row_container.set_class_name("chat-row");

            // Avatar + name for *own* user messages
            if message.role == "user" {
                if let Some(curr_user) = current_user {
                    let avatar = crate::components::avatar_badge::render(document, curr_user)?;
                    avatar.set_class_name("avatar-badge small");
                    row_container.append_child(&avatar)?;
                }
            }

            // Create the container for message bubble
            let message_element = document.create_element("div")?;
            message_element
                .set_attribute("data-testid", "chat-message")
                .ok();
            // Set class based on message role and type (user vs assistant)
            let class_name = if message.role == "user" {
                "message user-message".to_string()
            } else {
                "message assistant-message".to_string()
            };
            message_element.set_class_name(&class_name);
            // Create content element
            let content = document.create_element("div")?;
            content.set_class_name("message-content preserve-whitespace");
            // XSS Prevention: Set text content (no HTML).  Whitespace preserved via CSS class.
            content.set_text_content(Some(&message.content));
            // Create timestamp element
            let timestamp = document.create_element("div")?;
            timestamp.set_class_name("message-time");
            let time_text = format_timestamp(&message.timestamp.clone().unwrap_or_default());
            timestamp.set_text_content(Some(&time_text));
            // Add content and timestamp to message element
            message_element.append_child(&content)?;
            message_element.append_child(&timestamp)?;
            // Add bubble to row_container
            row_container.append_child(&message_element)?;

            // Add row to messages container
            messages_container.append_child(&row_container)?;

            // If this is an assistant message, render any child tool messages
            if message.role == "assistant" {
                if let Some(id) = message.id {
                    if let Some(tool_msgs) = tool_messages_by_parent.get(&id) {
                        for tool_msg in tool_msgs {
                            render_tool_message(document, messages_container.clone(), tool_msg)?;
                        }
                    }
                }
            }
        }

        // --------------------------------------------------------------
        // Render *tool* messages that are **not** linked to an assistant
        // bubble via `parent_id`.  Historically the backend populated this
        // foreign-key but a recent refactor removed it which resulted in the
        // messages disappearing from the UI.  To remain resilient we now
        // fall back to showing such orphaned tool outputs directly.
        // --------------------------------------------------------------

        for tool_msg in sorted_messages
            .iter()
            .filter(|m| m.role == "tool" && m.parent_id.is_none())
        {
            render_tool_message(document, messages_container.clone(), tool_msg)?;
        }
        // Scroll to the bottom
        messages_container.set_scroll_top(messages_container.scroll_height());
    }

    Ok(())
}

// Helper to render a single tool message (collapsible indicator + optional details)
fn render_tool_message(
    document: &Document,
    messages_container: web_sys::Element,
    tool_msg: &ApiThreadMessage,
) -> Result<(), JsValue> {
    let tool_call_id = tool_msg.tool_call_id.clone().unwrap_or_default();
    // No longer need JavaScript state - using native disclosure

    let details_el = document.create_element("details")?;
    details_el.set_class_name("disclosure");
    details_el.set_attribute("data-tool-call-id", &tool_call_id).ok();
    // Programmatic control: details_el.set_open(true/false) for JS interaction

    let tool_name = tool_msg.tool_name.as_deref().unwrap_or("tool");
    let summary = document.create_element("summary")?;
    summary.set_class_name("disclosure__summary");
    summary.set_inner_html(&format!("🛠️ Tool Used: {}", tool_name));
    details_el.append_child(&summary)?;

    let content_wrap = document.create_element("div")?;
    content_wrap.set_class_name("disclosure__content");

    let inner = document.create_element("div")?;

    let row_tool = document.create_element("div")?;
    row_tool.set_class_name("tool-detail-row");
    row_tool.set_inner_html(&format!("<strong>Tool:</strong> {}", tool_name));
    inner.append_child(&row_tool)?;

    let input_val = tool_msg
        .tool_input
        .clone()
        .unwrap_or_else(|| "None".to_string());
    let row_args = document.create_element("div")?;
    row_args.set_class_name("tool-detail-row");
    row_args.set_inner_html(&format!(
        "<strong>Inputs:</strong> <pre>{}</pre>",
        input_val.replace("\n", "<br>")
    ));
    inner.append_child(&row_args)?;

    // Always show full output - user already chose to expand tool details
    let full_output = tool_msg.content.clone();
    let row_out = document.create_element("div")?;
    row_out.set_class_name("tool-detail-row output-row");
    row_out.set_inner_html(&format!(
        "<strong>Output:</strong> <pre>{}</pre>",
        full_output.replace("\n", "<br>")
    ));
    inner.append_child(&row_out)?;

    content_wrap.append_child(&inner)?;
    details_el.append_child(&content_wrap)?;

    messages_container.append_child(&details_el)?;

    Ok(())
}

/// Utility function for programmatic disclosure control
/// Usage: set_disclosure_state(document, "tool-call-123", true) to open
pub fn set_disclosure_state(document: &Document, tool_call_id: &str, open: bool) -> Result<(), JsValue> {
    if let Some(details) = document.query_selector(&format!("[data-tool-call-id='{}']", tool_call_id))? {
        // Use setAttribute for broader compatibility 
        if open {
            details.set_attribute("open", "")?;
        } else {
            details.remove_attribute("open")?;
        }
    }
    Ok(())
}

// Update the thread title in the header
#[allow(dead_code)]
pub fn update_thread_title(_document: &Document) -> Result<(), JsValue> {
    // Instead of accessing APP_STATE directly, dispatch a message to get the current title
    // This prevents borrowing conflicts since state access will happen in update()

    // This message will cause the update() function to get the current thread title
    // and then dispatch an UpdateThreadTitleUI message
    dispatch_global_message(Message::RequestThreadTitleUpdate);

    Ok(())
}

// New version that accepts the title directly - no APP_STATE access needed
pub fn update_thread_title_with_data(document: &Document, title: &str) -> Result<(), JsValue> {
    if let Some(title_el) = document.query_selector(".thread-title-text").ok().flatten() {
        title_el.set_text_content(Some(title));
    }

    Ok(())
}

// Helper function to get the current agent ID from the state
#[allow(dead_code)]
fn get_current_agent_id(state: &std::cell::Ref<crate::state::AppState>) -> Option<u32> {
    // Use the new agent-centric state
    state.current_agent_id
}

/// Refresh chat UI from agent-scoped state instead of global dispatches
pub fn refresh_chat_ui_from_agent_state(agent_id: u32) {
    use crate::state::APP_STATE;

    crate::debug_log!(
        "🔍 [UI] refresh_chat_ui_from_agent_state called for agent {}",
        agent_id
    );

    if let Some(document) = web_sys::window().and_then(|w| w.document()) {
        crate::debug_log!("🔍 [UI] Document found, accessing APP_STATE");
        APP_STATE.with(|state| {
            let state = state.borrow();

            if let Some(agent_state) = state.agent_states.get(&agent_id) {
                crate::debug_log!(
                    "🔍 [UI] Found agent state for agent {}",
                    agent_id
                );
                crate::debug_log!(
                    "🔍 [UI] Agent has {} threads",
                    agent_state.threads.len()
                );
                crate::debug_log!(
                    "🔍 [UI] Current thread ID: {:?}",
                    agent_state.current_thread_id
                );

                // Update thread list from agent state
                let threads: Vec<&crate::models::ApiThread> = agent_state.get_threads_sorted();
                let thread_refs: Vec<crate::models::ApiThread> =
                    threads.into_iter().cloned().collect();

                crate::debug_log!(
                    "🔍 [UI] Updating thread list with {} threads",
                    thread_refs.len()
                );
                let _ = update_thread_list_ui(
                    &document,
                    &thread_refs,
                    agent_state.current_thread_id,
                    &agent_state.thread_messages,
                );

                // Update conversation UI if we have a current thread
                if let Some(current_thread_id) = agent_state.current_thread_id {
                    if let Some(messages) = agent_state.thread_messages.get(&current_thread_id) {
                        crate::debug_log!(
                            "🔍 [UI] Updating conversation with {} messages for thread {}",
                            messages.len(),
                            current_thread_id
                        );
                        let current_user_opt = state.current_user.clone();
                        let _ =
                            update_conversation_ui(&document, messages, current_user_opt.as_ref());
                        crate::debug_log!("🔍 [UI] Conversation UI update completed");
                    } else {
                        web_sys::console::warn_1(
                            &format!(
                                "🔍 [UI] No messages found for current thread {}",
                                current_thread_id
                            )
                            .into(),
                        );
                    }
                } else {
                    web_sys::console::warn_1(&"🔍 [UI] No current thread ID".into());
                }
            } else {
                web_sys::console::error_1(
                    &format!("🔍 [UI] No agent state found for agent {}", agent_id).into(),
                );
            }
        });
    }
}

// Helper function to format timestamps
fn format_timestamp(timestamp: &str) -> String {
    // Basic formatting - in a real app you'd want more sophisticated date handling
    if timestamp.is_empty() {
        return "[Missing timestamp]".to_string();
    }

    // Parse the timestamp (assuming ISO format)
    // In a real app, use a proper date library
    let date_parts: Vec<&str> = timestamp.split('T').collect();
    if date_parts.len() >= 2 {
        let time_parts: Vec<&str> = date_parts[1].split(':').collect();
        if time_parts.len() >= 2 {
            return format!("{} {}:{}", date_parts[0], time_parts[0], time_parts[1]);
        }
    }

    timestamp.to_string()
}

// Helper function to truncate text with ellipsis
fn truncate_text(text: &str, max_graphemes: usize) -> String {
    use unicode_segmentation::UnicodeSegmentation;

    // Collect the string into user-perceived grapheme clusters so we do not
    // slice through multi-byte characters or emoji sequences ("😎", "🇨🇦",
    // "��‍👩‍👧‍👦", etc.).
    let graphemes: Vec<&str> = text.graphemes(true).collect();

    if graphemes.len() <= max_graphemes {
        // No truncation required – return the original string verbatim.
        text.to_string()
    } else {
        // Join the first `max_graphemes` clusters and append an ellipsis.
        let truncated: String = graphemes[..max_graphemes].concat();
        format!("{}...", truncated)
    }
}

// Update loading state in the UI
pub fn update_loading_state(document: &Document, is_loading: bool) -> Result<(), JsValue> {
    if let Some(thread_list) = document.query_selector(".thread-list").ok().flatten() {
        if is_loading {
            thread_list.set_inner_html("");
            let loading_el = document.create_element("div")?;
            loading_el.set_class_name("thread-loading");
            loading_el.set_attribute("aria-live", "polite")?;
            loading_el.set_attribute("aria-label", "Thread loading status")?;
            loading_el.set_text_content(Some("Loading threads..."));
            thread_list.append_child(&loading_el)?;
        }
    }
    Ok(())
}
