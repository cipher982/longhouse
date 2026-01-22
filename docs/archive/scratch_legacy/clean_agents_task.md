Below is a conceptual overview and rationale for cleanly separating agents from nodes, ensuring agents remain a primary structure in the application while nodes simply provide optional UI overlays. The key is enforcing strict boundaries between your domain logic (agents) and the visual canvas elements (nodes).

────────────────────────────────────────────────────────────────────────
1) Treat Agents as a Pure Domain Model
────────────────────────────────────────────────────────────────────────
Agents should exist independently of any canvas or UI. This means:
• Store agent-related data—such as name, status, timestamps, and instructions—in a dedicated data structure or API-bound model (ApiAgent).
• Make all CRUD operations revolve around agents, entirely separate from the UI. For example, to create or update an agent, call your backend API (via your network.rs) without referencing the canvas.
• Refrain from storing any coordinate or UI-centric properties (x, y, node width, etc.) in ApiAgent objects. Those properties belong to the node layer.

────────────────────────────────────────────────────────────────────────
2) Treat Nodes as Optional Visualization Artifacts
────────────────────────────────────────────────────────────────────────
Nodes become relevant only when you wish to visualize agents on a canvas. Consequently:
• Nodes should store layout or geometry details (like x, y, width, height, color).
• Each node can have an optional agent_id to indicate which agent (if any) it is displaying.
• Node creation is an explicit action: you might say “Create a node on the canvas for Agent #42.” But you do not automatically create or remove nodes whenever an agent is created/deleted, unless the user specifically wants to reflect it visually.

────────────────────────────────────────────────────────────────────────
3) Refactor State Management into Two Collections
────────────────────────────────────────────────────────────────────────
In your AppState (or any global store), keep two separate HashMaps:
• agents: HashMap<u32, ApiAgent> — The business domain data (fully loaded from the API).
• nodes: HashMap<String, Node> — The UI data used for drawings.
A node may store agent_id as a foreign key, but interactions on agents should never rely on a node’s existence (or vice versa).

────────────────────────────────────────────────────────────────────────
4) Remove Auto-Creation or Auto-Synchronization in the Storage/Startup
────────────────────────────────────────────────────────────────────────
When you load agents from the backend, do not automatically create nodes. Instead:
• Load the agents and store them in state.agents.
• (Optionally) Provide a user action (e.g., “Generate Canvas from Agents”) to create nodes for each agent if the user wants a visual layout.
• Likewise, when saving your state, do not auto-update an agent purely because a node changed. Instead, only update the agent in the backend if the user explicitly saved agent changes.

────────────────────────────────────────────────────────────────────────
5) Introduce Explicit “Sync” Messages or Functions
────────────────────────────────────────────────────────────────────────
If you sometimes need to push node data (like node.text if it’s doubling as an agent name), add a discrete action such as:
• Message::SyncNodeToAgent { node_id, agent_id }
• This translates node fields into agent updates (e.g., if the node’s “text” is actually acting like an “agent name”).
• Conversely, if you want to pull agent data into the node (e.g., new instructions), use Message::SyncAgentToNode { agent_id, node_id }.
Doing it this way keeps synchronization intentional, rather than automatic or accidental.

────────────────────────────────────────────────────────────────────────
6) Maintain a Strict “API-Layer → Agents” and “UI-Layer → Nodes” Partition
────────────────────────────────────────────────────────────────────────
• When you call your backend (create, update, delete agents), do so via functions that operate strictly on agent data (agent_id, agent fields).
• Only call node functions (createNode, moveNode, etc.) within the front-end UI code, not in global agent logic.
• If you discover that some logic belongs purely to the agent domain (e.g., scheduling or status transitions), strip UI references from it. The node layer simply taps in if it needs to reflect the new status visually.

────────────────────────────────────────────────────────────────────────
7) Result: Agents as First-Class Citizens, Nodes as Optional Overlays
────────────────────────────────────────────────────────────────────────
By removing node creation logic from your agent code, you free your domain model from the noise of layout details. Agents remain the core entity your API and data store revolve around. Meanwhile, nodes become optional, ephemeral “views” that users can employ to see or manipulate those agents visually. If you later introduce a new way to visualize agents (lists, trees, forms), you can do so without touching agent logic; similarly, you can modify canvas node behaviors without risking agent data corruption.

Overall, this approach ensures your domain model (agents) remains pure and stable, while your UI representation (nodes) is naturally looser and optional. The explicit separation of responsibilities (and explicit “sync” steps only when needed) is a “best practice” that avoids the coupling you’ve been fighting. Once implemented, you can add or remove UI features, such as the canvas, with minimal impact on your core agent logic.
  llm -c "create an overview list of important files and sections that will be relevant here"
Below is a concise directory-level overview, highlighting where you’ll find the most relevant code sections as you work toward separating Agents (domain logic) from Nodes (canvas/UI logic). The goal is to show you which files handle agent data vs. which handle node rendering and layout:

────────────────────────────────────────────────────────────────────────
1) Core Domain & Data Models
────────────────────────────────────────────────────────────────────────
• src/models.rs
  - Defines the ApiAgent (used for all agent data loaded/saved via the backend).
  - Also defines Node (for canvas visualization) and includes some older references tying agents to nodes.
  - Key to separating concerns: remove or limit any direct Node→Agent references.

• src/messages.rs
  - Holds Message variants for events in your UI.
  - Many of these currently intermingle agent operations with node operations.
  - Plan to create distinct messages for agent CRUD vs. node (canvas) tasks.

• src/state.rs
  - Global store (AppState) with two primary HashMaps: agents vs. nodes.
  - Contains the logic that currently attempts to auto-create or auto-sync.
  - You’ll want to keep them separate: agents is purely domain data, nodes is purely metadata for the UI.

────────────────────────────────────────────────────────────────────────
2) Networking & Storage
────────────────────────────────────────────────────────────────────────
• src/network.rs
  - Wraps API calls (CRUD) for agents (get_agents, create_agent, run_agent, etc.).
  - Use these for agent data only, removing any reference to node creation.
  - WebSocket handler (setup_websocket) for agent events.

• src/storage.rs
  - Currently attempts to load/save agent data and node data together.
  - Contains logic to “auto-create” or “auto-remove” nodes when an agent is fetched.
  - You’ll want to refactor so that agent loading from the API does not automatically create nodes.

────────────────────────────────────────────────────────────────────────
3) Canvas & Node Rendering
────────────────────────────────────────────────────────────────────────
• src/canvas/
  ├── shapes.rs, renderer.rs, mod.rs
  - All about drawing nodes on a canvas: shapes, text rendering, etc.
  - Should remain purely about layout, ignoring agent updates.
  - A node can reference an agent_id for styling or text, but no domain-level logic belongs here.

• src/components/canvas_editor.rs
  - The main code for setting up and resizing the canvas, handling mouse events to drag nodes, etc.
  - Currently can blur lines by referencing agent properties. In the new approach, keep node-based logic here only.

────────────────────────────────────────────────────────────────────────
4) UI Events & Views
────────────────────────────────────────────────────────────────────────
• src/ui/
  ├── main.rs, events.rs, modals.rs, setup.rs
  - The front-end logic for your HTML elements, modals, toggles, etc.
  - Where you wire up user interactions (button clicks, form entries) to dispatch relevant messages.
  - Should not rely on agent or node logic except via the state’s messages.

• src/components/dashboard.rs
  - Renders a table of agents, focusing on the domain-level data (agent name, status, run logs).
  - Importantly, keep this purely in “agent world” (no node creation). If you want a “Create Node for This Agent” action, you’d add an explicit button that triggers a separate “Create Node” message, rather than auto-creating one.

────────────────────────────────────────────────────────────────────────
5) General Message/Update Flow
────────────────────────────────────────────────────────────────────────
• src/update.rs
  - The update() function that processes every Message.
  - Currently merges agent logic (“CreateAgentWithDetails”) with node logic (“AddNode”).
  - Refactor to have clearer branching: “Agent messages” vs. “Node messages” vs. “Sync messages.”

────────────────────────────────────────────────────────────────────────
6) build.sh / build-debug.sh / Cargo.toml
────────────────────────────────────────────────────────────────────────
• build.sh, build-debug.sh
  - Scripts for building and serving. Not directly relevant to the agent/node separation, but keep them in place for your local runs.

• Cargo.toml
  - Standard Rust manifest. The crate-type, dependencies, and editions are set here but do not affect the agents vs. nodes issue directly.

────────────────────────────────────────────────────────────────────────
Summary of Key Refactoring Points
────────────────────────────────────────────────────────────────────────
1. models.rs and messages.rs: separate or reduce cross-pollination between Node and ApiAgent.
2. storage.rs and network.rs: ensure agent data loading/saving is distinct from node (canvas) actions.
3. update.rs: break out distinct “agent messages” vs. “node messages,” removing automatic creation of nodes.
4. dashboard.rs: run purely on agent data, no forced node creation.
5. canvas/*: be purely about Node layout/drawing, querying agent data only when needed (like to display agent.name in the node label), but never updating agent data from the node’s side unless explicitly triggered.

This structure ensures you have a pure agent domain (CRUD, statuses, tasks, logs) and an optional node UI layer for those times you want to visually place agents on a canvas.
