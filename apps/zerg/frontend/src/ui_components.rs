//! Reusable UI component helpers to reduce code duplication and ensure consistency.
//!
//! This module provides factory functions for common UI patterns like buttons,
//! form fields, and modal structures. All components follow accessibility best
//! practices and use consistent styling from constants.

use crate::constants::*;
use wasm_bindgen::prelude::*;
use web_sys::{Document, Element};

/// Button configuration for the button factory
#[derive(Debug, Clone)]
pub struct ButtonConfig {
    pub id: Option<String>,
    pub text: String,
    pub class_name: Option<String>,
    pub data_testid: Option<String>,
    pub aria_label: Option<String>,
    pub disabled: bool,
}

impl Default for ButtonConfig {
    fn default() -> Self {
        Self {
            id: None,
            text: String::new(),
            class_name: None,
            data_testid: None,
            aria_label: None,
            disabled: false,
        }
    }
}

impl ButtonConfig {
    pub fn new(text: &str) -> Self {
        Self {
            text: text.to_string(),
            ..Default::default()
        }
    }

    pub fn with_id(mut self, id: &str) -> Self {
        self.id = Some(id.to_string());
        self
    }

    pub fn with_class(mut self, class_name: &str) -> Self {
        self.class_name = Some(class_name.to_string());
        self
    }

    pub fn with_testid(mut self, testid: &str) -> Self {
        self.data_testid = Some(testid.to_string());
        self
    }

    pub fn with_aria_label(mut self, aria_label: &str) -> Self {
        self.aria_label = Some(aria_label.to_string());
        self
    }

    pub fn disabled(mut self) -> Self {
        self.disabled = true;
        self
    }
}

/// Create a button element with consistent attributes and styling
pub fn create_button(document: &Document, config: ButtonConfig) -> Result<Element, JsValue> {
    let button = document.create_element("button")?;

    // Always set type="button" to prevent form submission
    button.set_attribute(ATTR_TYPE, BUTTON_TYPE_BUTTON)?;

    // Set text content
    button.set_inner_html(&config.text);

    // Set optional attributes
    if let Some(id) = config.id {
        button.set_id(&id);
    }

    if let Some(class_name) = config.class_name {
        button.set_class_name(&class_name);
    }

    if let Some(testid) = config.data_testid {
        button.set_attribute(ATTR_DATA_TESTID, &testid)?;
    }

    if let Some(aria_label) = config.aria_label {
        button.set_attribute("aria-label", &aria_label)?;
    }

    if config.disabled {
        button.set_attribute("disabled", "true")?;
    }

    Ok(button)
}

// ---------------------------------------------------------------------------
// Loading / spinner helpers
// ---------------------------------------------------------------------------

/// Toggle a button into *loading* state: disables it, swaps text for spinner.
/// Pass `false` to restore original text.
pub fn set_button_loading(btn: &Element, loading: bool) {
    let class_list = btn.class_list();
    if loading {
        let _ = class_list.add_1("loading");
        let _ = btn.set_attribute("disabled", "true");
        // save original
        if btn.get_attribute("data-orig-label").is_none() {
            if let Some(label) = btn.text_content() {
                let _ = btn.set_attribute("data-orig-label", &label);
            }
        }
        btn.set_inner_html("<span class='spinner'></span>");
    } else {
        let _ = class_list.remove_1("loading");
        let _ = btn.remove_attribute("disabled");
        if let Some(orig) = btn.get_attribute("data-orig-label") {
            btn.set_inner_html(&orig);
        }
    }
}

/// Create a primary action button (blue background)
pub fn create_primary_button(
    document: &Document,
    text: &str,
    id: Option<&str>,
) -> Result<Element, JsValue> {
    let mut config = ButtonConfig::new(text).with_class("btn-primary");

    if let Some(id) = id {
        config = config.with_id(id);
    }

    create_button(document, config)
}

/// Create a secondary action button (default styling)
pub fn create_secondary_button(
    document: &Document,
    text: &str,
    id: Option<&str>,
) -> Result<Element, JsValue> {
    let mut config = ButtonConfig::new(text).with_class("btn");

    if let Some(id) = id {
        config = config.with_id(id);
    }

    create_button(document, config)
}

/// Create an icon-only action button with proper accessibility
pub fn create_icon_button(
    document: &Document,
    icon: &str,
    aria_label: &str,
    class_name: Option<&str>,
) -> Result<Element, JsValue> {
    let config = ButtonConfig::new(icon)
        .with_class(class_name.unwrap_or("action-btn"))
        .with_aria_label(aria_label);

    create_button(document, config)
}

/// Create a delete/danger button (red styling)
pub fn create_danger_button(
    document: &Document,
    text: &str,
    id: Option<&str>,
) -> Result<Element, JsValue> {
    let mut config = ButtonConfig::new(text).with_class("btn-danger");

    if let Some(id) = id {
        config = config.with_id(id);
    }

    create_button(document, config)
}

/// Form field configuration
#[derive(Debug, Clone)]
pub struct FormFieldConfig {
    pub id: String,
    pub label_text: String,
    pub input_type: String,
    pub placeholder: Option<String>,
    pub required: bool,
    pub data_testid: Option<String>,
    pub rows: Option<u32>, // For textarea
}

impl FormFieldConfig {
    pub fn new(id: &str, label_text: &str, input_type: &str) -> Self {
        Self {
            id: id.to_string(),
            label_text: label_text.to_string(),
            input_type: input_type.to_string(),
            placeholder: None,
            required: false,
            data_testid: None,
            rows: None,
        }
    }

    pub fn with_placeholder(mut self, placeholder: &str) -> Self {
        self.placeholder = Some(placeholder.to_string());
        self
    }

    pub fn with_testid(mut self, testid: &str) -> Self {
        self.data_testid = Some(testid.to_string());
        self
    }

    pub fn required(mut self) -> Self {
        self.required = true;
        self
    }

    pub fn textarea_with_rows(mut self, rows: u32) -> Self {
        self.input_type = "textarea".to_string();
        self.rows = Some(rows);
        self
    }
}

/// Create a form field with label and input/textarea
pub fn create_form_field(document: &Document, config: FormFieldConfig) -> Result<Element, JsValue> {
    let container = document.create_element("div")?;
    container.set_class_name(CSS_FORM_ROW);

    // Create label
    let label = document.create_element("label")?;
    label.set_inner_html(&config.label_text);
    label.set_attribute("for", &config.id)?;
    container.append_child(&label)?;

    // Create input or textarea
    let input = if config.input_type == "textarea" {
        let textarea = document.create_element("textarea")?;
        textarea.set_id(&config.id);

        if let Some(rows) = config.rows {
            textarea.set_attribute("rows", &rows.to_string())?;
        }

        if let Some(placeholder) = &config.placeholder {
            textarea.set_attribute("placeholder", placeholder)?;
        }

        if config.required {
            textarea.set_attribute("required", "true")?;
        }

        if let Some(testid) = &config.data_testid {
            textarea.set_attribute(ATTR_DATA_TESTID, testid)?;
        }

        textarea
    } else {
        let input = document.create_element("input")?;
        input.set_id(&config.id);
        input.set_attribute("type", &config.input_type)?;

        if let Some(placeholder) = &config.placeholder {
            input.set_attribute("placeholder", placeholder)?;
        }

        if config.required {
            input.set_attribute("required", "true")?;
        }

        if let Some(testid) = &config.data_testid {
            input.set_attribute(ATTR_DATA_TESTID, testid)?;
        }

        input
    };

    container.append_child(&input)?;
    Ok(container)
}

/// Create a modal header with title and close button
pub fn create_modal_header(
    document: &Document,
    title: &str,
    close_id: &str,
) -> Result<Element, JsValue> {
    let header = document.create_element("div")?;
    header.set_class_name("modal-header");

    let title_element = document.create_element("h2")?;
    title_element.set_inner_html(title);
    header.append_child(&title_element)?;

    let close_button = create_icon_button(document, "&times;", "Close modal", Some("close"))?;
    close_button.set_id(close_id);
    header.append_child(&close_button)?;

    Ok(header)
}

/// Create an actions row container with flex layout
pub fn create_actions_row(document: &Document) -> Result<Element, JsValue> {
    let container = document.create_element("div")?;
    container.set_class_name(CSS_ACTIONS_ROW);
    Ok(container)
}

/// Create a card container with consistent styling
pub fn create_card(document: &Document, id: Option<&str>) -> Result<Element, JsValue> {
    let card = document.create_element("div")?;
    card.set_class_name("card");

    if let Some(id) = id {
        card.set_id(id);
    }

    Ok(card)
}
