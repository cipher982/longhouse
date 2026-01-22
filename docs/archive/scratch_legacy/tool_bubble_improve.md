# Frontend Task: Refine Tool Call Display in Chat Interface

## 1. Problem Statement

The current implementation for displaying AI agent tool calls (function calls made by the agent, e.g., `get_current_time`) within the chat interface is functionally present but visually disruptive.

*   **Visual Prominence:** The tool call visualization (white background bubble) is currently too prominent and visually competes with the primary user/agent conversation bubbles. It draws attention away from the main interaction flow.
*   **Aesthetic Inconsistency:** The styling feels disconnected from the established look and feel of the user (blue) and agent (grey) message bubbles.
*   **Scalability Issues:** The current design doesn't gracefully handle potentially large tool outputs (e.g., long JSON responses from APIs), which could break the chat layout or become unreadable.
*   **Information Hierarchy:** While the information is present, it's given equal or greater visual weight than the agent's actual response, which is counterintuitive for a debugging aid (it should be supplementary).

*(Link to current screenshot/example can be added here if available)*

## 2. Goals

*   **Improve Visual Hierarchy:** Make tool call information visible but clearly subordinate to the main conversation messages.
*   **Enhance Aesthetics:** Integrate the tool call display more seamlessly into the existing chat UI design.
*   **Improve Scanability:** Allow users to easily scan the conversation flow without being bogged down by tool details unless they choose to inspect them.
*   **Handle Variable Output Size:** Implement a solution that works well for both very short tool outputs (like a timestamp) and very large outputs (like API responses).
*   **Provide Necessary Details:** Ensure the tool name, inputs (if applicable), and outputs are accessible for debugging purposes.
*   **Maintain Context:** Clearly associate the tool call information with the relevant agent turn/message.

## 3. Context

*   **Feature Area:** AI Agent Platform - Agent Chat Interface
*   **Purpose:** This interface allows users to interact with, test, and debug AI agents they are building or using.
*   **User Need:** Users (often developers) need to understand *how* an agent arrived at its response, which includes seeing which tools were called, with what arguments (inputs), and what data (outputs) was returned. This is crucial for debugging agent behavior.

## 4. Proposed Solution: Collapsible Tool Use Indicator

Instead of a full message bubble, we will implement a subtle, collapsible indicator for tool calls.

### 4.1. Collapsed State (Default View)

*   **Appearance:** A compact, single-line element or a very small, low-contrast block. It should *not* resemble a standard chat message bubble.
*   **Placement:** Position this indicator logically within the chat flow, ideally **directly preceding** the agent message that utilized the tool's output. This reflects the sequence of operations (Tool Call -> Output -> Agent Response).
*   **Styling:**
    *   **Background:** Use a low-contrast background (e.g., a slightly darker shade of the main chat background, or the agent bubble grey but significantly desaturated/transparent). Avoid bright white or strong borders.
    *   **Icon:** Include a small, subtle icon (e.g., 🛠️, ⚙️, `</>`) for quick identification. Make it visually smaller/less prominent than in the current version.
    *   **Text:** Use a smaller font size than the main chat text. Display a brief summary. Examples: `🛠️ Tool Used: get_current_time` or `⚙️ Ran get_current_time`.
    *   **Interactivity:** The element must clearly indicate it's interactive (e.g., `cursor: pointer;`, subtle background change on hover). Consider adding a small expand icon (e.g., `▾` or `▸`) to reinforce this.

### 4.2. Expanded State (On Click)

*   **Mechanism:** Clicking the collapsed indicator should expand it in place to reveal details. Use a smooth transition/animation (e.g., `height` or `max-height` transition).
*   **Layout:** The expanded view appears directly below the collapsed indicator, pushing subsequent content down.
*   **Content Structure:**
    *   `Tool:` Clearly label the tool name (e.g., `Tool: get_current_time`).
    *   `Inputs:` Clearly label the inputs. Display formatted inputs (e.g., pretty-printed JSON) if they exist. If no inputs were used, display `Inputs: None`.
    *   `Output:` Clearly label the output.
*   **Handling Large Outputs:**
    *   **Truncation:** By default, display only the first N lines (e.g., 5-10) or ~500 characters of the output.
    *   **Expand/Show More:** Include a "Show More" / "Expand" link/button *within* the output section if the content is truncated. Clicking this reveals the full output within the expanded area.
    *   **Scrolling:** The container holding the *full* output (after "Show More" is clicked, or if the output is large but not truncated initially) must have a `max-height` (e.g., `200px` or `300px`) and `overflow: auto;` to enable scrolling without breaking the page layout.
    *   **Copy Button:** Add a small "Copy" button next to the `Output:` label or near the output content itself, allowing users to easily copy the *full* output to their clipboard.
*   **Styling:**
    *   Maintain the subdued background from the collapsed state for the expanded container.
    *   Use a monospaced font (e.g., `monospace`, `Consolas`, `Courier New`) for displaying Inputs and Outputs, especially if they are code or JSON.

## 5. Key Implementation Details

*   Implement as a reusable component if applicable.
*   State management for collapsed/expanded state for each tool call instance.
*   Click handler for toggling expansion.
*   CSS for distinct collapsed and expanded styling, including transitions.
*   Logic for input formatting (JSON pretty-printing).
*   Logic for output truncation and the "Show More" functionality.
*   Implement the "Copy to Clipboard" functionality for the output.
*   Ensure proper ARIA attributes for accessibility (e.g., `aria-expanded`, `aria-controls`).
