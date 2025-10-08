//! Helper for populating the "agent-select" <select> element in the canvas
//! toolbar with the list of current agents.

use wasm_bindgen::JsCast;
use wasm_bindgen::JsValue;
use web_sys::{Document, HtmlElement, HtmlSelectElement};

use crate::state::APP_STATE;

/// Populate (or refresh) the dropdown with current agents.
#[allow(dead_code)] // kept until the DOM integration lands
pub fn update_agent_dropdown(document: &Document) -> Result<(), JsValue> {
    let Some(el) = document.get_element_by_id("agent-select") else {
        return Ok(()); // canvas not rendered yet
    };
    let select: HtmlSelectElement = el.dyn_into().unwrap();

    // Clear existing options
    select.set_inner_html("");

    // Insert a placeholder option

    let placeholder = document
        .create_element("option")?
        .dyn_into::<HtmlElement>()?;
    placeholder.set_attribute("value", "")?;
    placeholder.set_inner_text("-- choose agent --");
    select.append_child(&placeholder)?;

    // Iterate over agents in state
    APP_STATE.with(|state| {
        let state = state.borrow();
        for agent in state.agents.values() {
            if let Some(id) = agent.id {
                let opt = document
                    .create_element("option")
                    .unwrap()
                    .dyn_into::<HtmlElement>()
                    .unwrap();
                opt.set_attribute("value", &id.to_string()).unwrap();
                opt.set_inner_text(&agent.name);
                select.append_child(&opt).unwrap();
            }
        }
    });

    Ok(())
}
