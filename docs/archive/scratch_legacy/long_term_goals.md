# Agent Platform Enhancement Plan

This document outlines a set of improvements and new features to be implemented in the Agent Platform. Each section details the scope of work, recommended approaches, relevant code or file locations, and suggested steps for implementation. The goal is to guide our team over the next week (and beyond) in enhancing scheduling, adding external tools, and improving how agent outputs are managed and displayed.

--------------------------------------------------------------------------------
## Table of Contents

1. [Scheduling & Automation](#scheduling--automation)
   1. [Basic CRON-like Scheduling](#basic-cron-like-scheduling)
   2. [One‑Click “On Demand” vs. “Scheduled”](#oneclick-on-demand-vs-scheduled)
   3. [Advanced Queue/Worker Setup](#advanced-queueworker-setup)

2. [Adding Tools & External Services](#adding-tools--external-services)
   1. [General “Tools” Interface](#general-tools-interface)
   2. [Common Built‑in Tools](#common-builtin-tools)
   3. [Access Control & Permissions](#access-control--permissions)

3. [Better Presentation & Utilization of Outputs](#better-presentation--utilization-of-outputs)
   1. [Extensible Output Panels](#extensible-output-panels)
   2. [“Thread Log” vs. “Result Panel”](#thread-log-vs-result-panel)
   3. [Export / Summaries](#export--summaries)
   4. [Analytics & Stats](#analytics--stats)

4. [Smaller Improvements & Helpful Extras](#smaller-improvements--helpful-extras)

5. [Suggested Implementation Order](#suggested-implementation-order)

--------------------------------------------------------------------------------
## 1. Scheduling & Automation

### 1.1 Basic CRON‑like Scheduling

• **Goal**
  Allow agents to run automatically at specified intervals. This could be as simple as:
  – “Run every X hours/days”
  – “At 2:00 AM daily”

• **Approach**
  – Install a scheduling mechanism in Python. For example:
    » [APScheduler](https://apscheduler.readthedocs.io/)
    » A custom CRON job approach
  – Storage for scheduling details can be in a small DB or JSON config.

• **Backend Code Locations**
  – Currently, you have your main logic in “backend/main.py”.
  – Consider creating a new file, e.g. “backend/scheduler.py”, to encapsulate scheduling code.
  – Keep references to your agent data structures from “backend/main.py” or any data model if you create one.

• **Frontend UI Changes**
  – In the canvas or dashboard:
    » A new “Schedule” field or button in “frontend/src/components/agent_modal.rs” (or currently “canvas_editor.rs” & “dashboard.rs”) could let the user pick intervals.
  – Possibly store user-selected schedule data in the “AppState” (see “frontend/src/state.rs”).

• **Implementation Steps**
  1. Add scheduling parameters to agent data model (in “backend/main.py” or wherever you store agent configs).
  2. On each agent’s creation or update, store the schedule (CRON expression or simple poll interval).
  3. Use APScheduler to define a job for each scheduled agent.
  4. When the job fires, call the same internal method used for a user-initiated “Run Agent.”

--------------------------------------------------------------------------------
### 1.2 One‑Click “On Demand” vs. “Scheduled”

• **Goal**
  Provide a simple toggle or button so an agent can either run on demand or be put into a scheduled automation state.

• **Approach**
  – Extend your agent object with a “mode” property: { “manual”, “scheduled” }.
  – In the UI, show a toggle or a small dropdown in the agent card to switch modes.

• **File References**
  – “frontend/src/components/dashboard.rs” for the agent card actions (Run, Edit, etc.).
  – “frontend/src/state.rs” to store the ‘mode’ property in each agent’s struct.
  – “backend/main.py” or “scheduler.py” to decide if the agent’s schedule job is “active” or not based on this mode.

• **Implementation Steps**
  1. Add a “mode” parameter in your agent structure (e.g., “idle” or “scheduled”).
  2. In the front-end, create a toggle button. On “scheduled,” activate the CRON logic in the backend. On “manual,” skip scheduling.
  3. Provide a “Run Now” button (just call the same function that scheduling would call).

--------------------------------------------------------------------------------
### 1.3 Advanced Queue/Worker Setup

• **Goal**
  Scale beyond a single process. Let a separate worker handle agent tasks while the main server remains responsive.

• **Approach**
  – Integrate a queuing system: [Celery](https://docs.celeryproject.org/), [Redis Queue (RQ)](https://python-rq.org/), or possibly a Rust-based approach if you prefer.
  – Agents post tasks to the queue. A worker process picks them up, does the AI calls, and updates the results in real-time.

• **File References**
  – “backend/main.py” or a new “backend/worker_handler.py” for queueing logic.
  – “frontend/src/network.rs” to handle status updates from the worker through WebSockets.

• **Implementation Steps**
  1. Install Celery or RQ.
  2. Adjust the “Run Agent” function to insert a job into the queue.
  3. Worker consumes the job, streams partial tokens to the WebSocket.
  4. Confirm final results are saved to the agent’s thread log or output field.

--------------------------------------------------------------------------------
## 2. Adding Tools & External Services

### 2.1 General “Tools” Interface

• **Goal**
  Let agents do more than just chat with OpenAI—give them capabilities like emailing, reading logs, or making HTTP requests.

• **Approach**
  – Use a node-based design: each “tool” is a node the agent can call.
  – In the agent data model, store a list/array of “tools” that can be invoked.
  – The front-end Canvas Editor can display these tool nodes, connected to your “AgentIdentity” node or “AgentResponse” node.

• **Relevant Code**
  – The node structures are in “frontend/src/models.rs” (the Node struct).
  – The canvas drawing is in “frontend/src/canvas/*.rs” and “frontend/src/components/canvas_editor.rs”.
  – Back-end might store tool definitions in “backend/main.py” or a separate file “tools.py”.

• **Implementation Steps**
  1. Create a “Tool” trait or interface in Python (and/or in Rust if you handle logic in the front-end) that standardizes interactions.
  2. In the UI, add a “Add Tool” button or a palette of available tools the user can drag onto the canvas.
  3. Each tool node can store configuration (e.g., API key, endpoint URL).
  4. When an agent runs, the backend checks which tools are connected and calls them as needed.

--------------------------------------------------------------------------------
### 2.2 Common Built‑in Tools

• **Examples**
  1. Email Sending Tool:
     – Fields: SMTP server, from/to addresses, subject, body.
     – Node shape or color: Distinguish from default nodes.
  2. Log Retrieval Tool:
     – Reads a specified log file or queries a log aggregator.
  3. Webhook/HTTP Tool:
     – Allows GET/POST requests to external APIs or webhooks.

• **File References**
  – For email, you might implement simple Python logic in “tools.py” (e.g., using `smtplib` or a library).
  – For logs, either local reading from disk or a service request.
  – In “frontend/src/canvas/*.rs,” add custom code in the “shapes.rs” or “renderer.rs” to visually represent each tool node differently.

--------------------------------------------------------------------------------
### 2.3 Access Control & Permissions

• **Goal**
  Ensure that not all agents can call all tools, or that secrets are safe.

• **Approach**
  – Extend your “Agent” or “Tool” data with “permissions,” e.g. { “agentX can use Email Tool,” “agentY cannot.” }
  – Possibly store tool credentials in a secure location (like environment variables or a small secrets manager).
  – The front-end can show an “Access Denied” if a user tries to connect an agent to a restricted tool.

• **Implementation Steps**
  1. Add a “permissions” field to the agent’s data or a global config.
  2. In the backend, upon running a tool, check if the agent is allowed.
  3. Hide or gray out the tool icon in the Canvas Editor if the agent lacks permission.

--------------------------------------------------------------------------------
## 3. Better Presentation & Utilization of Outputs

### 3.1 Extensible Output Panels

• **Goal**
  Instead of raw text, display structured or multi-form outputs (tables, charts, etc.).

• **Approach**
  – Let each agent define an “output type” (text, JSON, HTML snippet, etc.).
  – Enhance the front-end nodes to parse and present the output.
  – If it’s structured data, show an interactive table or mini visualization (like a bar chart).

• **File References**
  – “frontend/src/messages.rs” handles chunked messages from the backend.
  – “frontend/src/network.rs” receives streaming tokens.
  – “frontend/src/components/canvas_editor.rs” or “dashboard.rs” is where you can embed new “Output Panel” code.

• **Implementation Steps**
  1. Modify the chunked streaming approach to optionally include a content-type (plain text vs. JSON).
  2. In the UI, detect JSON and parse it, display in a small table or bullet list.
  3. Provide a fallback for plain text if parsing fails.

--------------------------------------------------------------------------------
### 3.2 “Thread Log” vs. “Result Panel”

• **Goal**
  Differentiate between the step-by-step chat log and a final summarized “result” of the agent’s operation.

• **Approach**
  – Maintain a “history” array for thread transcripts.
  – Also store a “latestResult” or “finalOutput.”
  – The UI can show a collapsible Chat Log area plus a more prominent “Result” box.

• **Relevant Code**
  – Node and agent history is currently in “frontend/src/models.rs” (`history` field).
  – “frontend/src/components/modals.rs” or “ui/modals.rs” for the agent modal that displays thread history.

• **Implementation Steps**
  1. Add a “finalOutput” field in the node or agent structure.
  2. After the last streaming chunk, store the text in “finalOutput.”
  3. In the UI’s agent modal or dashboard, show a distinct “Result Panel” next to the thread log.

--------------------------------------------------------------------------------
### 3.3 Export / Summaries

• **Goal**
  Let users export thread logs or final outputs, and optionally generate short summaries.

• **Approach**
  – Add export buttons in the dashboard or modal (e.g., “Export as JSON” or “Export as CSV”).
  – Summaries: Use an additional quick prompt to your AI (like “Please summarize the above thread in 100 words.”).

• **File References**
  – “frontend/src/components/dashboard.rs” or “modals.rs” for your new “Export” button.
  – “backend/main.py” (if you want to do the summary generation server-side).

• **Implementation Steps**
  1. Implement a small function that clones the agent’s thread from state.
  2. Convert to JSON or CSV, prompt user to download.
  3. Optionally, have a “Summarize” button that calls your AI endpoint with a short summary prompt.
  4. Display the result in a read-only text area or message node.

--------------------------------------------------------------------------------
### 3.4 Analytics & Stats

• **Goal**
  Track usage metrics (token counts, calls performed, time cost, success/failure rate).

• **Approach**
  – Increment counters each time an agent is invoked.
  – Store in DB or at least in memory, then display in the UI.
  – Possibly show a small usage chart or numeric stats panel.

• **File References**
  – “backend/main.py” or a new “stats.py” for tracking usage in memory or a tiny database.
  – “frontend/src/components/dashboard.rs” to present these numbers as columns (e.g., “Run Count,” “Avg Duration,” “Success Rate”).

• **Implementation Steps**
  1. On each agent invoke, measure the start/end times, compute durations.
  2. Save those stats in your agent record or a separate “AgentStats” struct.
  3. The next time the user opens the dashboard or agent modal, display the stats.

--------------------------------------------------------------------------------
## 4. Smaller Improvements & Helpful Extras

• **Notifications**
  – Email or Slack notifications for agent completion or failure. Ties in well with the “Tools” feature if you use an “Email Tool.”

• **Fine‑Tuning**
  – Let advanced users supply their own fine-tuned model references if you’re using openai.FineTune.

• **Multi-Agent Orchestration**
  – One agent can call another agent’s “Run” function, enabling chaining. Great for more complex tasks, but requires a clear cycle detection or concurrency approach.

• **UI Polish**
  – Add tooltips explaining each icon/button.
  – Animate transitions or connections in the Canvas Editor.
  – Provide a responsive design for tablets or smaller devices.

--------------------------------------------------------------------------------
## 5. Suggested Implementation Order

If your team wants a phased approach:

1) **Enable Basic Scheduling**
   – Minimal CRON or interval scheduling.
   – One toggle in the UI to set “Scheduled” vs. “On Demand.”

2) **Add Tools**
   – Start with a simple Email or Webhook tool.
   – Provide a small UI to connect them in the Canvas Editor.

3) **Enhance Output Panels**
   – Differentiate final output from the thread log.
   – Optionally add the “Export” and “Summarize” features.

4) **Introduce Worker Queue**
   – If you need better performance or concurrency, integrate Celery/RQ.
   – Move all agent runs into the queue.

5) **Advanced Tools & Analytics**
   – Add more specialized tools (Log Reader, Database queries).
   – Show usage stats, scheduling metrics, and cost analytics in the Dashboard.

Following this order gives you immediate benefits (scheduling, basic tools) and sets the stage for more advanced expansions (workers, analytics, dynamic diagrams) without overwhelming your codebase in one go.
