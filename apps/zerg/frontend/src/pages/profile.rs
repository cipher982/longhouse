// frontend/src/pages/profile.rs
//
// Basic user profile page (display name & avatar URL for now).

use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, HtmlElement, HtmlInputElement};

use crate::state::APP_STATE;

/// ID constants for DOM nodes so we can grab them later
const CONTAINER_ID: &str = "profile-container";

/// Mount (render) the profile page into the main app container.
pub fn mount_profile(document: &Document) -> Result<(), JsValue> {
    // Re-use app-container so layout is consistent with other pages.
    let app_container = document
        .get_element_by_id("app-container")
        .ok_or_else(|| JsValue::from_str("app-container missing"))?;

    // -------- create container if needed --------
    let container: HtmlElement = if let Some(el) = document.get_element_by_id(CONTAINER_ID) {
        el.dyn_into()?
    } else {
        let el: HtmlElement = document.create_element("div")?.dyn_into()?;
        el.set_id(CONTAINER_ID);
        el.set_class_name("profile-container");
        app_container.append_child(&el)?;
        el
    };

    // Clear existing children for clean re-render.
    while let Some(child) = container.first_child() {
        let _ = container.remove_child(&child);
    }

    // ----------------------------------------------------------------
    // Title
    // ----------------------------------------------------------------
    let title = document.create_element("h2")?;
    title.set_inner_html("User Profile");
    container.append_child(&title)?;

    // ----------------------------------------------------------------
    // Form wrapper
    // ----------------------------------------------------------------
    let form = document.create_element("div")?;
    form.set_class_name("profile-form");

    // Fetch current user (clone to avoid borrow issues later)
    let maybe_user = APP_STATE.with(|s| s.borrow().current_user.clone());

    let display_name_value = maybe_user
        .as_ref()
        .and_then(|u| u.display_name.clone())
        .unwrap_or_default();

    let avatar_url_value = maybe_user
        .as_ref()
        .and_then(|u| u.avatar_url.clone())
        .unwrap_or_default();

    // Display Name field ------------------------------------------------
    let dn_label = document.create_element("label")?;
    dn_label.set_attribute("for", "profile-display-name")?;
    dn_label.set_inner_html("Display name");

    let dn_input: HtmlInputElement = document.create_element("input")?.dyn_into()?;
    dn_input.set_id("profile-display-name");
    dn_input.set_value(&display_name_value);
    dn_input.set_attribute("type", "text")?;
    dn_input.set_class_name("profile-input");

    form.append_child(&dn_label)?;
    form.append_child(&dn_input)?;

    // Avatar URL field --------------------------------------------------
    let av_label = document.create_element("label")?;
    av_label.set_attribute("for", "profile-avatar-url")?;
    av_label.set_inner_html("Avatar URL");

    let av_input: HtmlInputElement = document.create_element("input")?.dyn_into()?;
    av_input.set_id("profile-avatar-url");
    av_input.set_value(&avatar_url_value);
    av_input.set_attribute("type", "text")?;
    av_input.set_class_name("profile-input");

    form.append_child(&av_label)?;
    form.append_child(&av_input)?;

    // ----------------------------------------------------------------
    // Save button
    // ----------------------------------------------------------------
    let save_btn: HtmlElement = document.create_element("button")?.dyn_into()?;
    save_btn.set_attribute("type", "button")?;
    save_btn.set_inner_html("Save");
    save_btn.set_class_name("btn-primary");

    // onClick closure ---------------------------------------------------
    {
        // Capture cloned document for lookups inside closure.
        let document = document.clone();

        let _cb = Closure::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
            // Grab field values
            let dn = document
                .get_element_by_id("profile-display-name")
                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                .map(|input| input.value())
                .unwrap_or_default();

            let av = document
                .get_element_by_id("profile-avatar-url")
                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                .map(|input| input.value())
                .unwrap_or_default();

            // Build patch JSON (skip empty values to avoid clearing accidentally)
            let mut map = serde_json::Map::new();
            if !dn.is_empty() {
                map.insert("display_name".to_string(), serde_json::Value::String(dn));
            }
            if !av.is_empty() {
                map.insert("avatar_url".to_string(), serde_json::Value::String(av));
            }

            let patch_json = serde_json::Value::Object(map).to_string();

            // Make async API call
            wasm_bindgen_futures::spawn_local(async move {
                match crate::network::api_client::ApiClient::update_current_user(&patch_json).await
                {
                    Ok(resp_json) => {
                        if let Ok(user) =
                            serde_json::from_str::<crate::models::CurrentUser>(&resp_json)
                        {
                            crate::state::dispatch_global_message(
                                crate::messages::Message::CurrentUserLoaded(user),
                            );
                            // Optionally show a toast
                            crate::network::ui_updates::flash_activity();
                        }
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to save profile: {:?}", e).into(),
                        );
                    }
                }
            });
        }) as Box<dyn FnMut(_)>);
        save_btn.add_event_listener_with_callback("click", _cb.as_ref().unchecked_ref())?;
        _cb.forget();
    }

    form.append_child(&save_btn)?;

    container.append_child(&form)?;

    // Ensure container is visible
    crate::dom_utils::show(&container);

    Ok(())
}

/// Unmount / remove the profile page from DOM
#[allow(dead_code)]
pub fn unmount_profile(document: &Document) -> Result<(), JsValue> {
    if let Some(el) = document.get_element_by_id(CONTAINER_ID) {
        if let Some(parent) = el.parent_node() {
            parent.remove_child(&el)?;
        }
    }
    Ok(())
}

/// Return true if the profile page is currently mounted
pub fn is_profile_mounted(document: &Document) -> bool {
    document.get_element_by_id(CONTAINER_ID).is_some()
}
