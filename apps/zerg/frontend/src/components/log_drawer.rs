use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element};

use crate::state::APP_STATE;

/// Ensure the log drawer exists and redraw its content from `AppState`.
pub fn refresh(document: &Document) -> Result<(), JsValue> {
    // Create drawer if missing
    let drawer_el: Element = if let Some(el) = document.get_element_by_id("log-drawer") {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_attribute("id", "log-drawer")?;
        el.set_attribute("style", "position:fixed;bottom:0;left:0;width:100%;height:25vh;background:#1e1e1e;color:#eee;font-family:monospace;font-size:12px;overflow:auto;display:none;padding:4px 8px;box-sizing:border-box;border-top:1px solid #333;")?;
        document.body().unwrap().append_child(&el)?;
        el
    };

    // Toggle visibility based on AppState.logs_open
    let logs_open = APP_STATE.with(|st| st.borrow().logs_open);
    if logs_open {
        drawer_el.set_attribute("style", "position:fixed;bottom:0;left:0;width:100%;height:25vh;background:#1e1e1e;color:#eee;font-family:monospace;font-size:12px;overflow:auto;padding:4px 8px;box-sizing:border-box;border-top:1px solid #333;display:block;")?;
    } else {
        drawer_el.set_attribute("style", "display:none;")?;
        return Ok(());
    }

    // Rebuild content
    while let Some(child) = drawer_el.first_child() {
        drawer_el.remove_child(&child)?;
    }

    let logs = APP_STATE.with(|st| st.borrow().execution_logs.clone());
    for log in logs {
        let line = document.create_element("div")?;
        if log.stream == "stderr" || log.stream == "error" {
            line.set_attribute("style", "color:#ff4e4e;")?;
        }
        line.set_text_content(Some(&format!("[{}] {}", log.node_id, log.text)));
        drawer_el.append_child(&line)?;
    }

    // Auto-scroll to bottom
    if let Some(html_el) = drawer_el.dyn_ref::<web_sys::HtmlElement>() {
        html_el.set_scroll_top(html_el.scroll_height());
    }

    Ok(())
}
