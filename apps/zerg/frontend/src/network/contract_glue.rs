use serde_json::Value;

use super::generated_client::WorkflowDataContract;

fn map_node_type(frontend_type: &str) -> &str {
    match frontend_type {
        "AgentIdentity" => "agent",
        "Tool" => "tool",
        "Trigger" => "trigger",
        // All others map to conditional
        "UserInput" | "ResponseOutput" | "GenericNode" | _ => "conditional",
    }
}

fn lower_str(v: &Value) -> Option<String> { v.as_str().map(|s| s.to_ascii_lowercase()) }

/// Normalize and validate a canvas JSON structure into the exact API contract.
/// - Maps node `type` to backend literals (agent/tool/trigger/conditional)
/// - Preserves typed trigger metadata under `config.trigger` (canonical)
/// - Does NOT write legacy flattened keys (`trigger_type`, `params`, `filters`, `enabled`)
pub fn normalize_canvas_for_api(mut canvas: Value) -> Result<WorkflowDataContract, String> {
    // Ensure nodes exist and normalize node types + trigger subtype casing
    let mut manual_count = 0usize;

    if let Some(nodes) = canvas.get_mut("nodes").and_then(|n| n.as_array_mut()) {
        for node in nodes {
            // Map semantic type token to backend literal
            if let Some(t) = node.get_mut("type") {
                if let Some(ts) = t.as_str() {
                    *t = Value::String(map_node_type(ts).to_string());
                }
            }

            // For trigger nodes: lowercase config.trigger.type, count 'manual'
            let is_trigger = node
                .get("type")
                .and_then(|v| v.as_str())
                .map(|s| s == "trigger")
                .unwrap_or(false);

            if is_trigger {
                if let Some(cfg) = node.get_mut("config").and_then(|c| c.as_object_mut()) {
                    if let Some(trig) = cfg.get_mut("trigger").and_then(|t| t.as_object_mut()) {
                        if let Some(tt) = trig.get_mut("type") {
                            if let Some(lc) = lower_str(tt) {
                                *tt = Value::String(lc);
                            }
                        }
                        // Count manual subtype
                        if trig
                            .get("type")
                            .and_then(|v| v.as_str())
                            .map(|s| s == "manual")
                            .unwrap_or(false)
                        {
                            manual_count += 1;
                        }
                    }
                }
            }
            // No legacy mirroring – typed `config.trigger` is preserved as-is.
        }
    }

    // Enforce FE invariant: ≤ 1 Manual trigger
    if manual_count > 1 {
        return Err("Only one Manual trigger allowed per workflow".to_string());
    }

    // Validate to API contract type
    serde_json::from_value::<WorkflowDataContract>(canvas)
        .map_err(|e| format!("Canvas data doesn't match contract: {}", e))
}
