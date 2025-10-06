use wasm_bindgen::JsValue;
use web_sys::{Document};

/// Mount the Ops dashboard view container and render initial contents.
pub fn mount_ops_dashboard(document: &Document) -> Result<(), JsValue> {
    let app_container = document
        .get_element_by_id("app-container")
        .ok_or(JsValue::from_str("Could not find app-container"))?;

    // Create container if missing
    let container = if let Some(el) = document.get_element_by_id("ops-dashboard-container") {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_id("ops-dashboard-container");
        el.set_class_name("ops-dashboard-container");

        // Inner root
        let root = document.create_element("div")?;
        root.set_id("ops-dashboard");
        root.set_class_name("ops-dashboard");
        el.append_child(&root)?;
        app_container.append_child(&el)?;
        el
    };

    crate::dom_utils::show(&container);
    crate::components::ops::render_ops_dashboard(document)?;
    Ok(())
}

#[allow(dead_code)]
pub fn is_ops_dashboard_mounted(document: &Document) -> bool {
    document
        .get_element_by_id("ops-dashboard-container")
        .is_some()
}
