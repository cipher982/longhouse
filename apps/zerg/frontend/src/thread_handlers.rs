use crate::messages::Message;
use crate::models::{ApiThread, ApiThreadMessage};
use crate::state::dispatch_global_message;
use std::collections::HashMap;
use wasm_bindgen_futures::spawn_local;
// Removed unused import
use crate::debug_log;

/// Message status for optimistic UI updates
#[derive(Debug, Clone, PartialEq)]
#[allow(dead_code)]
pub enum MessageStatus {
    /// Message is pending confirmation from the server
    Pending,
    /// Message has been confirmed by the server
    Confirmed,
    /// Message failed to send
    Failed,
}

/// Handles when a user sends a new message to a thread
/// Instead of using optimistic UI pattern, we'll use a loading state
/// 1. Show loading state during API call
/// 2. Wait for the server response
/// 3. Update UI with confirmed data from the server
pub fn handle_send_thread_message(
    thread_id: u32,
    content: String,
    _thread_messages: &mut HashMap<u32, Vec<ApiThreadMessage>>,
    _threads: &HashMap<u32, ApiThread>,
    _current_thread_id: Option<u32>,
) -> Box<dyn FnOnce()> {
    // Clone data for async operations
    let content_clone = content.clone();

    // Return a closure to be executed after the borrow is released
    Box::new(move || {
        // Show loading state in the UI
        if let Some(_document) = web_sys::window().and_then(|w| w.document()) {
            dispatch_global_message(Message::UpdateLoadingState(true));
        }

        // Send the message to the backend - NO optimistic UI
        spawn_local(async move {
            match crate::network::api_client::ApiClient::create_thread_message(
                thread_id,
                &content_clone,
            )
            .await
            {
                Ok(response) => {
                    // Parse the response and update the UI directly with the server data
                    if let Ok(_thread_message) = serde_json::from_str::<ApiThreadMessage>(&response)
                    {
                        // Reload thread messages from the server to get the most accurate state
                        match crate::network::api_client::ApiClient::get_thread_messages(
                            thread_id, 0, 100,
                        )
                        .await
                        {
                            Ok(messages_json) => {
                                if let Ok(messages) =
                                    serde_json::from_str::<Vec<ApiThreadMessage>>(&messages_json)
                                {
                                    // Update the UI with the confirmed messages from the server
                                    dispatch_global_message(Message::ThreadMessagesLoaded(
                                        thread_id, messages,
                                    ));
                                    dispatch_global_message(Message::UpdateLoadingState(false));
                                }
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to reload messages: {:?}", e).into(),
                                );
                                dispatch_global_message(Message::UpdateLoadingState(false));
                            }
                        }

                        // Now trigger the thread to run and process the message
                        debug_log!(
                            "Now running thread {} to process the message",
                            thread_id
                        );
                        match crate::network::api_client::ApiClient::run_thread(thread_id).await {
                            Ok(_) => {
                                debug_log!(
                                    "Successfully triggered thread {} to process message",
                                    thread_id
                                );
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to run thread: {:?}", e).into(),
                                );
                            }
                        }
                    } else {
                        web_sys::console::error_1(
                            &"Failed to parse thread message response".into(),
                        );
                        dispatch_global_message(Message::UpdateLoadingState(false));
                    }
                }
                Err(e) => {
                    web_sys::console::error_1(
                        &format!("Failed to send thread message: {:?}", e).into(),
                    );
                    dispatch_global_message(Message::UpdateLoadingState(false));
                }
            }
        });
    })
}

/// Handles the response from the server after sending a message
/// Updates the optimistic message with the confirmed one from the server
#[allow(dead_code)]
pub fn handle_thread_message_sent(
    response: String,
    client_id: String,
    thread_messages: &mut HashMap<u32, Vec<ApiThreadMessage>>,
) -> Option<Box<dyn FnOnce()>> {
    // Parse the response from the server
    if let Ok(thread_message) = serde_json::from_str::<ApiThreadMessage>(&response) {
        let thread_id = thread_message.thread_id;

        // Try to parse the client_id string back to u32
        if let Ok(client_id_num) = client_id.parse::<u32>() {
            // Find and replace the optimistic message with the confirmed message
            if let Some(messages) = thread_messages.get_mut(&thread_id) {
                // Find the index of the optimistic message
                if let Some(index) = messages
                    .iter()
                    .position(|msg| msg.id.as_ref().map_or(false, |id| *id == client_id_num))
                {
                    // Replace the optimistic message with the confirmed message
                    messages[index] = thread_message.clone();

                    // Return a closure to update the UI
                    let messages_clone = messages.clone();
                    return Some(Box::new(move || {
                        if let Some(_document) = web_sys::window().and_then(|w| w.document()) {
                            // Update the conversation UI
                            dispatch_global_message(Message::UpdateConversation(messages_clone));
                        }
                    }));
                }
            }
        }
    }

    None
}

/// Handles marking a message as failed
#[allow(dead_code)]
pub fn handle_thread_message_failed(
    thread_id: u32,
    client_id: String,
    thread_messages: &mut HashMap<u32, Vec<ApiThreadMessage>>,
) -> Option<Box<dyn FnOnce()>> {
    // Try to parse the client_id string back to u32
    if let Ok(client_id_num) = client_id.parse::<u32>() {
        // Find and update the status of the optimistic message
        if let Some(messages) = thread_messages.get_mut(&thread_id) {
            // Find the optimistic message by its client ID
            if let Some(message) = messages
                .iter_mut()
                .find(|msg| msg.id.as_ref().map_or(false, |id| *id == client_id_num))
            {
                // Mark as failed by adding a special tag to the content
                message.content = format!("[Failed to send] {}", message.content);

                // Return a closure to update the UI
                let messages_clone = messages.clone();
                return Some(Box::new(move || {
                    if let Some(_document) = web_sys::window().and_then(|w| w.document()) {
                        // Update the conversation UI with the failed message status
                        dispatch_global_message(Message::UpdateConversation(messages_clone));
                    }
                }));
            }
        }
    }

    None
}

/// Handles when a message is received from the websocket
#[allow(dead_code)]
pub fn handle_thread_message_received(
    message_str: String,
    thread_messages: &mut HashMap<u32, Vec<ApiThreadMessage>>,
    threads: &HashMap<u32, ApiThread>,
    current_thread_id: Option<u32>,
) -> Option<Box<dyn FnOnce()>> {
    // Parse the received message
    if let Ok(message_value) = serde_json::from_str::<serde_json::Value>(&message_str) {
        // Check the message type first to handle different message formats
        let message_type = message_value.get("type").and_then(|t| t.as_str());

        match message_type {
            // Handle thread_history messages (different format than individual messages)
            Some("thread_history") => {
                let thread_id = message_value
                    .get("thread_id")
                    .and_then(|t| t.as_u64())
                    .map(|t| t as u32);

                if let Some(thread_id) = thread_id {
                    let messages_array = message_value.get("messages").and_then(|m| m.as_array());

                    if let Some(messages_array) = messages_array {
                        // Parse and store the messages
                        let mut parsed_messages = Vec::new();

                        for msg_value in messages_array {
                            if let Ok(message) =
                                serde_json::from_value::<ApiThreadMessage>(msg_value.clone())
                            {
                                parsed_messages.push(message);
                            }
                        }

                        // Store the messages
                        thread_messages.insert(thread_id, parsed_messages.clone());

                        // Variables to collect data for UI updates
                        let conversation_messages = parsed_messages;
                        let threads_data: Vec<ApiThread> = threads.values().cloned().collect();
                        let current_thread_id_opt = current_thread_id;
                        let thread_messages_map = thread_messages.clone();

                        // Return a closure to update the UI
                        return Some(Box::new(move || {
                            if let Some(_document) = web_sys::window().and_then(|w| w.document()) {
                                // Always update the thread list
                                dispatch_global_message(Message::UpdateThreadList(
                                    threads_data,
                                    current_thread_id_opt,
                                    thread_messages_map,
                                ));

                                // Only update conversation if this is the current thread
                                if current_thread_id_opt == Some(thread_id) {
                                    dispatch_global_message(Message::UpdateConversation(
                                        conversation_messages,
                                    ));
                                }
                            }
                        }));
                    }
                }
            }

            // Handle individual thread messages
            Some("thread_message") => {
                // Extract the message from the "message" field
                if let Some(message_data) = message_value.get("message") {
                    if let Ok(message) =
                        serde_json::from_value::<ApiThreadMessage>(message_data.clone())
                    {
                        let thread_id = message.thread_id;

                        // Variables to collect data for UI updates
                        let mut conversation_messages = Vec::new();
                        // Suppress unused assignment warning by reading the initial value
                        let _ = &conversation_messages;
                        let threads_data: Vec<ApiThread> = threads.values().cloned().collect();
                        let current_thread_id_opt = current_thread_id;
                        let mut thread_messages_map = thread_messages.clone();

                        // Add the message to the thread
                        if let Some(messages) = thread_messages.get_mut(&thread_id) {
                            messages.push(message.clone());
                            conversation_messages = messages.clone();
                        } else {
                            thread_messages.insert(thread_id, vec![message.clone()]);
                            conversation_messages = vec![message.clone()];

                            // Update our thread_messages_map clone with the new message
                            thread_messages_map.insert(thread_id, vec![message.clone()]);
                        }

                        // Return a closure to update the UI
                        return Some(Box::new(move || {
                            // Update the UI using message dispatch
                            if let Some(_document) = web_sys::window().and_then(|w| w.document()) {
                                dispatch_global_message(Message::UpdateConversation(
                                    conversation_messages,
                                ));
                                dispatch_global_message(Message::UpdateThreadList(
                                    threads_data,
                                    current_thread_id_opt,
                                    thread_messages_map,
                                ));
                            }
                        }));
                    }
                }
            }
            _ => {}
        }
    }
    None
}
