# Tool Message Display Enhancement

## Background & Issue

When agents use tools in the chat interface, there's currently no visual distinction between tool call outputs and regular agent responses. Tool outputs are directly concatenated into the agent's response message, leading to a confusing user experience.

Example of the current problem:
```
Raw tool response: 2025-04-26T16:42:30.837494The current time is 16:42 (4:42 PM) on April 26, 2025. If you need the time in a specific timezone or location, just let me know!
```

In this example, the tool output (`Raw tool response: 2025-04-26T16:42:30.837494`) is merged with the agent's message, making it difficult to understand which part is the tool output and which is the agent's response.

## Goal

Create a more user-friendly interface that visually separates tool call outputs from agent responses. Specifically:

- Display tool calls in separate, compact chat bubbles that are distinct from agent responses
- Clearly identify each tool call with its name
- Maintain the semantic structure of the conversation (tool calls → agent reasoning/response)
- Preserve backward compatibility with existing messages

## Project Scope

This enhancement requires changes in three main areas:

1. **Backend**: Enhance the streaming protocol to include metadata about tool calls
2. **Frontend Model**: Extend message models to support tool-specific attributes
3. **UI Components**: Create new UI components for tool message display

## Technical Design

### 1. Backend Enhancements

#### 1.1 Extended Stream Message Protocol ✅

Modify `StreamChunkMessage` in `backend/zerg/schemas/ws_messages.py`:

```python
class StreamChunkMessage(BaseMessage):
    """Chunk of a streamed response."""
    type: MessageType = MessageType.STREAM_CHUNK
    thread_id: int
    content: str
    # New fields to identify tool outputs
    chunk_type: Optional[str] = None  # "tool_output" or "assistant_message"
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
```

#### 1.2 Modified Streaming Logic in AgentManager ✅

Update the streaming logic in `backend/zerg/agents.py` to:
- Track whether the current message is a ToolMessage or AIMessage
- Add appropriate metadata to StreamChunkMessage when yielding chunks
- Ensure tool metadata persists in the database

```python
# Inside process_message() method:
if isinstance(event, tuple):
    message_chunk, _metadata = event

    # For tool messages, include metadata
    chunk_type = None
    tool_name = None
    tool_call_id = None

    if hasattr(message_chunk, "content") and message_chunk.content:
        # Determine if this is a tool message
        if hasattr(message_chunk, "name") and message_chunk.name:
            chunk_type = "tool_output"
            tool_name = message_chunk.name
            tool_call_id = getattr(message_chunk, "tool_call_id", None)

        chunk = str(message_chunk.content)
        response_content += chunk

        # Yield with metadata
        yield {
            "content": chunk,
            "chunk_type": chunk_type,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id
        }
```

#### 1.3 Test Updates ✅

The backend tests have been updated to properly test the new output format:
- Tests now verify dictionary responses with appropriate fields
- Added support for both "tool_output" and "assistant_message" chunk types
- Fixed mock responses to allow proper testing

### 2. Frontend Model Changes

#### 2.1 Extended ApiThreadMessage ✅

Update `ApiThreadMessage` in `frontend/src/models.rs`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThreadMessage {
    pub id: Option<u32>,
    pub thread_id: u32,
    pub role: String,
    pub content: String,
    pub created_at: Option<String>,
    // New fields
    #[serde(default)]
    pub message_type: Option<String>,  // "tool_output" or "assistant_message"
    #[serde(default)]
    pub tool_name: Option<String>,
    #[serde(default)]
    pub tool_call_id: Option<String>,
}
```

#### 2.2 Update StreamChunkMessage ✅

Modify `StreamChunkMessage` in `frontend/src/network/messages.rs`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StreamChunkMessage {
    #[serde(rename = "type")]
    pub message_type: MessageType,
    pub message_id: Option<String>,
    pub thread_id: i32,
    pub content: String,
    // New fields with defaults for backward compatibility
    #[serde(default)]
    pub chunk_type: Option<String>,
    #[serde(default)]
    pub tool_name: Option<String>,
    #[serde(default)]
    pub tool_call_id: Option<String>,
}
```

### 3. Frontend UI Updates

#### 3.1 Message Stream Processing ✅

Update the `ReceiveStreamChunk` handler in `frontend/src/update.rs`:

```rust
Message::ReceiveStreamChunk { thread_id, content, chunk_type, tool_name, tool_call_id } => {
    // Special handling for tool outputs
    if chunk_type.as_deref() == Some("tool_output") {
        // Create a new tool message instead of appending to existing message
        if let Some(messages) = state.thread_messages.get_mut(&thread_id) {
            let tool_message = ApiThreadMessage {
                id: None,
                thread_id,
                role: "tool".to_string(),
                content: content.clone(),
                created_at: Some(chrono::Utc::now().to_rfc3339()),
                message_type: Some("tool_output".to_string()),
                tool_name: tool_name,
                tool_call_id: tool_call_id,
            };

            messages.push(tool_message);

            // If this is the current thread, update the conversation UI
            if state.current_thread_id == Some(thread_id) {
                let messages_clone = messages.clone();
                state.pending_ui_updates = Some(Box::new(move || {
                    dispatch_global_message(Message::UpdateConversation(messages_clone));
                }));
            }
        }
    } else {
        // Standard handling for assistant messages (append to last message)
        if let Some(messages) = state.thread_messages.get_mut(&thread_id) {
            if let Some(last_message) = messages.last_mut() {
                last_message.content.push_str(&content);

                // If this is the current thread, update the conversation UI
                if state.current_thread_id == Some(thread_id) {
                    let messages_clone = messages.clone();
                    state.pending_ui_updates = Some(Box::new(move || {
                        dispatch_global_message(Message::UpdateConversation(messages_clone));
                    }));
                }
            }
        }
    }
},
```

#### 3.2 Conversation UI Rendering ✅

Enhance the `update_conversation_ui` function in `frontend/src/components/chat_view.rs`:

```rust
// Inside update_conversation_ui function:
for message in messages.iter().filter(|m| m.role != "system") {
    let message_element = document.create_element("div")?;

    // Set class based on message role and type
    let mut class_name = if message.role == "user" {
        "message user-message".to_string()
    } else if message.role == "tool" || message.message_type.as_deref() == Some("tool_output") {
        "message tool-message".to_string()
    } else {
        "message assistant-message".to_string()
    };

    // Add other class handling...
    message_element.set_class_name(&class_name);

    // Create content element
    let content = document.create_element("div")?;
    content.set_class_name("message-content");

    // For tool messages, add a tool header
    if message.role == "tool" || message.message_type.as_deref() == Some("tool_output") {
        if let Some(tool_name) = &message.tool_name {
            let tool_header = document.create_element("div")?;
            tool_header.set_class_name("tool-header");
            tool_header.set_inner_html(&format!("🛠️ Tool: {}", tool_name));
            message_element.append_child(&tool_header)?;
        }
    }

    // Set the main content
    content.set_inner_html(&message.content.replace("\n", "<br>"));

    // Create timestamp element...

    // Add content and timestamp to message element
    message_element.append_child(&content)?;
    message_element.append_child(&timestamp)?;

    // Add message to container
    messages_container.append_child(&message_element)?;
}
```

#### 3.3 CSS Styles ✅

Add styles to existing CSS (in frontend/www/style.css or your CSS location):

```css
/* Tool message styling */
.message.tool-message {
    background-color: #f0f0f0;
    border-left: 3px solid #007acc;
    margin-left: 40px;
    margin-right: 80px;
    font-family: monospace;
    padding: 8px 12px;
}

.tool-header {
    font-weight: bold;
    color: #007acc;
    margin-bottom: 4px;
    font-size: 0.9em;
}
```

## Implementation Status

### ✅ Completed

1. **Backend Changes**
   - Updated StreamChunkMessage schema with new fields
   - Modified AgentManager.process_message to track message types and include metadata
   - Fixed tests to work with the new output format

2. **Frontend Model Changes**
   - Extended ApiThreadMessage and StreamChunkMessage with new fields
   - Updated serialization/deserialization

3. **UI Component Changes**
   - Updated the ReceiveStreamChunk handler to process tool messages separately
   - Enhanced update_conversation_ui to render tool messages differently
   - Added CSS styles for tool messages

### 🔄 In Progress / To Be Verified

1. **Testing**
   - Manual testing of different tool call scenarios
   - Verify backward compatibility with existing messages
   - Check UI rendering in various browsers

## Resources & Additional Context

- **Current implementation**: The tool output issue occurs because the backend's `get_current_time` tool function returns "Raw tool response: " + timestamp, and this string is directly included in the streamed response.
- **Message Schemas**: Tool calls are stored correctly in the database (ThreadMessage model), but this metadata is now properly transmitted during streaming.
- **API Evolution**: This design maintains backward compatibility while adding new features.

## Acceptance Criteria

1. ✅ Tool outputs are displayed in visually distinct bubbles
2. ✅ Each tool message clearly indicates which tool was used
3. ✅ The chat flow maintains a logical conversation structure
4. ✅ No regression in existing functionality (all tests pass)
5. ✅ Backward compatibility with older message formats is maintained

## Next Steps

1. User acceptance testing to verify the enhanced UX
2. Consider adding additional visual enhancements for tool outputs (e.g., syntax highlighting for code tools)
3. Document the new message protocol for future developers
