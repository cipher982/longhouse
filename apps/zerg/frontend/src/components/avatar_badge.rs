//! Small reusable avatar component.
//!
//! Renders either the user's `avatar_url` <img> or a coloured circle with the
//! first letter of their display_name / email.

use wasm_bindgen::prelude::*;
use web_sys::{Document, Element, HtmlElement};

use crate::models::CurrentUser;

/// Render an avatar badge and return the root element.
pub fn render(document: &Document, user: &CurrentUser) -> Result<Element, JsValue> {
    // Root wrapper (circle)
    let wrapper: HtmlElement = document.create_element("div")?.dyn_into()?;
    wrapper.set_class_name("avatar-badge");

    if let Some(url) = user.avatar_url.as_ref().filter(|s| !s.is_empty()) {
        // Use <img>
        let img: HtmlElement = document.create_element("img")?.dyn_into()?;
        img.set_attribute("src", url)?;
        img.set_class_name("avatar-img");
        wrapper.append_child(&img)?;
    } else {
        // Fallback – initials
        let initial = user
            .display_name
            .as_ref()
            .and_then(|s| s.chars().next())
            .or_else(|| user.email.chars().next())
            .unwrap_or('U');

        wrapper.set_inner_html(&initial.to_string());

        // Apply deterministic background colour based on user id
        let hue = (user.id % 360) as i32; // simple hash
        wrapper
            .style()
            .set_property("background", &format!("hsl({},70%,60%)", hue))?;
    }

    Ok(wrapper.into())
}
