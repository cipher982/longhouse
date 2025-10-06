//! Right-hand drawer listing recent workflow executions.

use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element};

use crate::messages::Message;
use crate::state::{dispatch_global_message, APP_STATE};

/// Create or refresh the execution sidebar.
/// Call after state changes that affect `executions` or visibility.
pub fn refresh(document: &Document) -> Result<(), JsValue> {
    // Ensure root element exists
    let root_el: Element = if let Some(el) = document.get_element_by_id("exec-sidebar") {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_attribute("id", "exec-sidebar")?;
        el.set_attribute("style", "position:fixed;top:0;right:0;width:260px;height:100%;background:#1e1e1e;color:#eee;font-family:Inter, sans-serif;font-size:13px;overflow:auto;display:none;border-left:1px solid #333;box-sizing:border-box;")?;
        document.body().unwrap().append_child(&el)?;
        el
    };

    // Show/hide based on state
    let open = APP_STATE.with(|st| st.borrow().exec_history_open);
    if !open {
        root_el.set_attribute("style", "display:none;")?;
        return Ok(());
    }

    root_el.set_attribute("style", "position:fixed;top:0;right:0;width:260px;height:100%;background:#1e1e1e;color:#eee;font-family:Inter,sans-serif;font-size:13px;overflow:auto;border-left:1px solid #333;box-sizing:border-box;display:block;padding:8px;")?;

    // Clear previous list
    while let Some(child) = root_el.first_child() {
        root_el.remove_child(&child)?;
    }

    // Header
    let header = document.create_element("div")?;
    header.set_text_content(Some("Execution History"));
    header.set_attribute("style", "font-weight:600;margin-bottom:6px;")?;
    root_el.append_child(&header)?;

    // Fetch executions
    let executions = APP_STATE.with(|st| st.borrow().executions.clone());

    for exec in executions {
        let item = document.create_element("div")?;
        item.set_attribute("style", "padding:4px 6px;border-radius:4px;cursor:pointer;margin-bottom:4px;background:#2c2c2c;")?;
        let status_color = match exec.status.as_str() {
            "success" => "#86efac",   // green
            "failed" => "#fca5a5",    // red
            "cancelled" => "#fcd34d", // amber
            _ => "#e0e7ff",           // indigo
        };
        item.set_inner_html(&format!(
            "<span style=\"color:{};font-weight:600;\">{}</span> <span style=\"font-size:11px;color:#aaa;\">#{}</span>",
            status_color, exec.status, exec.id
        ));

        // Click handler to select execution
        {
            let exec_id = exec.id;
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
                dispatch_global_message(Message::SelectExecution {
                    execution_id: exec_id,
                });
            }));
            item.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }

        root_el.append_child(&item)?;
    }

    Ok(())
}
