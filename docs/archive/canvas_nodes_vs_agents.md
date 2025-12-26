# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---

# Canvas Nodes vs. Agents

_Last updated: 2025-04-23_

Historically the Zerg frontend treated a **Node** on the canvas as the source of
truth for an _Agent_. Editing a node mutated the agent object and backend
updates tried to push changes back into the node — it worked, but produced
numerous edge-case bugs (blank names, schedule resets, etc.) once agents became
first-class backend records.

## The Decoupling

The April-2025 _node-agent decoupling_ refactor established a strict separation:

| Concept        | Responsibilities                                        | Persistence |
| -------------- | ------------------------------------------------------- | ----------- |
| **Agent**      | name, system & task instructions, schedule, run history | Database    |
| **CanvasNode** | x/y/size/colour, display `text`, optional `agent_id`    | Front-end   |

`CanvasNode.agent_id` is therefore **just a pointer** – it never owns the
business data.

### One-way Sync (Agent → Node)

• Creating a node (dragging from the Agent Shelf or calling
`Message::AddCanvasNode`) copies the agent’s name into the node’s `text`
field _once_.

• When an agent is renamed the frontend dispatches
`Message::RefreshCanvasLabels`. The update handler iterates over the
`agent_id_to_node_id` map and copies the fresh `agent.name` into each linked
node’s `text` property, followed by a canvas redraw.

• **Node → Agent sync no longer exists.** Changing the node label in the UI
(e.g. via future inline-rename support) will _not_ mutate the agent – by
design.

## Practical Guidelines

1. **Never** look up agent data via a node. Always query `state.agents` by
   `agent_id`.
2. The Agent-Config modal takes an `agent_id`, _not_ a `node_id`.
3. Tools and features that only concern visuals (dragging, connecting, colour
   changes) should operate on CanvasNode exclusively.

## Why it matters

- Fewer accidental regressions (blank names → overwritten DB rows, etc.)
- Cleaner Elm-style data flow: backend state and presentation state are clearly
  separated.
- Unlocks richer workflow editing: the canvas can now host nodes that have no
  backend counterpart (Triggers, Tool calls, Conditions, …).

Keep this contract intact when adding new features!
