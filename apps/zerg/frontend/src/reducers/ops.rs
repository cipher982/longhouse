use crate::messages::Message;
use crate::state::{AppState, OpsTick};

/// Returns true if the message was handled by this reducer
pub fn update(state: &mut AppState, msg: &Message, commands: &mut Vec<crate::messages::Command>) -> bool {
    match msg {
        Message::OpsSummaryLoaded(summary) => {
            state.ops_summary = Some(summary.clone());
            // Update HUD immediately
            commands.push(crate::messages::Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::ops::render_ops_hud(&doc);
                }
            })));
            true
        }
        Message::OpsAppendEvent { ts, kind, text } => {
            // Prepend newest-first and cap to 200 entries
            state.ops_ticker.insert(0, OpsTick { ts: *ts, kind: kind.clone(), text: text.clone() });
            if state.ops_ticker.len() > 200 { state.ops_ticker.truncate(200); }
            // Update ticker UI if present
            commands.push(crate::messages::Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::ops::render_ticker(&doc);
                }
            })));
            true
        }
        _ => false,
    }
}

