//! UserMenu – top-bar dropdown with avatar and quick actions.

use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement};

use crate::components::avatar_badge;
use crate::messages::Message;
use crate::state::dispatch_global_message;
use crate::state::APP_STATE;
use crate::storage::ActiveView;

/// Create & mount the user menu inside the `header` element.  Calling this
/// multiple times is safe – it will update the avatar if the element already
/// exists.
pub fn mount_user_menu(document: &Document) -> Result<(), JsValue> {
    let header = document
        .get_element_by_id("header")
        .ok_or(JsValue::from_str("header element missing"))?;

    // Ensure container exists
    let container: HtmlElement = if let Some(el) = document.get_element_by_id("user-menu-container")
    {
        el.dyn_into()?
    } else {
        let el: HtmlElement = document.create_element("div")?.dyn_into()?;
        el.set_id("user-menu-container");
        el.set_class_name("user-menu-container");
        header.append_child(&el)?;
        el
    };

    // Wipe existing children (simpler stateless render)
    while let Some(child) = container.first_child() {
        let _ = container.remove_child(&child);
    }

    // Retrieve profile
    let maybe_user = APP_STATE.with(|s| s.borrow().current_user.clone());
    if let Some(user) = maybe_user {
        // AvatarBadge
        let avatar_el = avatar_badge::render(document, &user)?;
        container.append_child(&avatar_el)?;

        // Dropdown toggle (simplified – clicking avatar toggles menu)
        let dropdown = document.create_element("div")?;
        dropdown.set_class_name("user-dropdown hidden");

        let profile_item = create_menu_item(document, "Profile")?;
        let power_mode_item = create_power_mode_toggle(document)?;
        let logout_item = create_menu_item(document, "Logout")?;

        // Event listeners ------------------------------------------------
        {
            // Clicking "Profile" toggles to the dedicated profile view via
            // the normal message/command flow so state & UI stay in sync.
            let _cb = Closure::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
                if let Some(win) = web_sys::window() {
                    let _ = win.location().set_hash("#/profile");
                }
                dispatch_global_message(Message::ToggleView(ActiveView::Profile));
            }) as Box<dyn FnMut(_)>);
            profile_item.add_event_listener_with_callback("click", _cb.as_ref().unchecked_ref())?;
            _cb.forget();
        }

        // Power Mode toggle event is attached inside create_power_mode_toggle

        {
            let _cb = Closure::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
                crate::utils::logout().ok();
                web_sys::window().unwrap().location().reload().ok();
            }) as Box<dyn FnMut(_)>);
            logout_item.add_event_listener_with_callback("click", _cb.as_ref().unchecked_ref())?;
            _cb.forget();
        }

        dropdown.append_child(&profile_item)?;
        dropdown.append_child(&power_mode_item)?;
        dropdown.append_child(&logout_item)?;

        container.append_child(&dropdown)?;

        // Toggle show/hide on avatar click
        {
            let dropdown_clone = dropdown.clone();
            let _cb = Closure::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
                let cls = dropdown_clone.class_list();
                if cls.contains("hidden") {
                    let _ = cls.remove_1("hidden");
                } else {
                    let _ = cls.add_1("hidden");
                }
            }) as Box<dyn FnMut(_)>);
            avatar_el
                .unchecked_ref::<HtmlElement>()
                .add_event_listener_with_callback("click", _cb.as_ref().unchecked_ref())?;
            _cb.forget();
        }
    }
    /// Create a menu item with a checkbox toggle for "Power Mode" (keyboard shortcuts)
    fn create_power_mode_toggle(document: &Document) -> Result<Element, JsValue> {
        use wasm_bindgen::JsCast;
        use web_sys::HtmlInputElement;
        // Render as: [ ] ⚡ Power Mode: Keyboard Shortcuts
        let item = document.create_element("div")?;
        item.set_class_name("user-menu-item");

        let label = document.create_element("label")?;
        label.set_class_name("user-menu-power-toggle");
        label.set_text_content(Some("⚡ Power Mode: Keyboard shortcuts"));

        let checkbox = document
            .create_element("input")?
            .dyn_into::<HtmlInputElement>()?;
        checkbox.set_type("checkbox");
        // Set initial value from AppState
        let checked = APP_STATE.with(|s| s.borrow().power_mode);
        checkbox.set_checked(checked);

        // --- Handle toggle ---
        let cb = Closure::wrap(Box::new(move |evt: web_sys::Event| {
            let checked = evt
                .target()
                .unwrap()
                .dyn_ref::<HtmlInputElement>()
                .map(|el| el.checked())
                .unwrap_or(false);
            crate::state::dispatch_global_message(crate::messages::Message::SetPowerMode(checked));
        }) as Box<dyn FnMut(_)>);
        checkbox.add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
        cb.forget();

        label.insert_before(&checkbox, label.first_child().as_ref())?;
        item.append_child(&label)?;
        Ok(item)
    }

    Ok(())
}

fn create_menu_item(document: &Document, label: &str) -> Result<Element, JsValue> {
    let item = document.create_element("div")?;
    item.set_class_name("user-menu-item");
    item.set_inner_html(label);
    Ok(item)
}
