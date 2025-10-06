//! Template Gallery component for browsing and deploying workflow templates

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element};

use crate::messages::Message;
use crate::models::WorkflowTemplate;
use crate::state::{dispatch_global_message, APP_STATE};

/// Show the template gallery modal
pub fn show_gallery() -> Result<(), JsValue> {
    let window = web_sys::window().ok_or("No window")?;
    let document = window.document().ok_or("No document")?;

    // Load templates when showing gallery
    dispatch_global_message(Message::ShowTemplateGallery);

    // Create modal overlay
    let overlay = document.create_element("div")?;
    overlay.set_id("template-gallery-overlay");
    overlay.set_class_name("modal-overlay");

    // Create modal container
    let modal = document.create_element("div")?;
    modal.set_class_name("modal template-gallery-modal");

    // Modal header
    let header = document.create_element("div")?;
    header.set_class_name("modal-header");
    header.set_inner_html("<h2>Template Gallery</h2>");

    // Close button
    let close_btn = document.create_element("button")?;
    close_btn.set_class_name("modal-close");
    close_btn.set_inner_html("×");

    {
        let overlay_clone = overlay.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            let _ = overlay_clone.remove();
        }));
        close_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    header.append_child(&close_btn)?;

    // Modal body
    let body = document.create_element("div")?;
    body.set_class_name("modal-body");

    // Filter controls
    let filters = document.create_element("div")?;
    filters.set_class_name("template-filters");

    // Category filter
    let category_select = document.create_element("select")?;
    category_select.set_id("template-category-filter");
    category_select.set_class_name("filter-select");

    // Add "All Categories" option
    let all_option = document.create_element("option")?;
    all_option.set_attribute("value", "")?;
    all_option.set_text_content(Some("All Categories"));
    category_select.append_child(&all_option)?;

    // My templates toggle
    let my_templates_toggle = document.create_element("label")?;
    my_templates_toggle.set_class_name("toggle-label");

    let checkbox = document.create_element("input")?;
    checkbox.set_attribute("type", "checkbox")?;
    checkbox.set_id("my-templates-only");

    my_templates_toggle.append_child(&checkbox)?;
    my_templates_toggle.append_with_str_1(" My Templates Only")?;

    filters.append_child(&category_select)?;
    filters.append_child(&my_templates_toggle)?;

    // Templates grid
    let grid = document.create_element("div")?;
    grid.set_id("templates-grid");
    grid.set_class_name("templates-grid");

    body.append_child(&filters)?;
    body.append_child(&grid)?;

    // Modal footer
    let footer = document.create_element("div")?;
    footer.set_class_name("modal-footer");

    let refresh_btn = document.create_element("button")?;
    refresh_btn.set_class_name("btn btn-secondary");
    refresh_btn.set_text_content(Some("Refresh"));

    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            dispatch_global_message(Message::ShowTemplateGallery);
        }));
        refresh_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    footer.append_child(&refresh_btn)?;

    // Assemble modal
    modal.append_child(&header)?;
    modal.append_child(&body)?;
    modal.append_child(&footer)?;
    overlay.append_child(&modal)?;

    // Event handlers
    setup_event_handlers(&category_select, &checkbox)?;

    // Add to DOM
    document.body().unwrap().append_child(&overlay)?;

    // Initial render
    refresh_templates_grid(&document)?;

    Ok(())
}

/// Setup event handlers for filters
fn setup_event_handlers(category_select: &Element, checkbox: &Element) -> Result<(), JsValue> {
    // Category filter change
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            if let Some(select) = web_sys::window()
                .and_then(|w| w.document())
                .and_then(|d| d.get_element_by_id("template-category-filter"))
                .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
            {
                let value = select.value();
                let category = if value.is_empty() { None } else { Some(value) };
                dispatch_global_message(Message::SetTemplateCategory(category));
            }
        }));
        category_select.add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    // My templates toggle
    {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            dispatch_global_message(Message::ToggleMyTemplatesOnly);
        }));
        checkbox.add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    Ok(())
}

/// Refresh the templates grid display
pub fn refresh_templates_grid(document: &Document) -> Result<(), JsValue> {
    let grid = match document.get_element_by_id("templates-grid") {
        Some(g) => g,
        None => return Ok(()), // Gallery not open
    };

    // Clear existing content
    grid.set_inner_html("");

    // Get templates from state - safe to borrow here as this is UI-initiated refresh
    let (templates, categories, loading) = APP_STATE.with(|state| {
        // This refresh is only called from UI events, not during state updates
        let s = state.borrow();
        (
            s.templates.clone(),
            s.template_categories.clone(),
            s.templates_loading,
        )
    });

    // Update category filter options
    if let Some(select) = document
        .get_element_by_id("template-category-filter")
        .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
    {
        // Keep the "All Categories" option
        let current_value = select.value();
        select.set_inner_html(r#"<option value="">All Categories</option>"#);

        for category in &categories {
            let option = document.create_element("option")?;
            option.set_attribute("value", category)?;
            option.set_text_content(Some(category));
            select.append_child(&option)?;
        }

        // Restore selection
        select.set_value(&current_value);
    }

    if loading {
        grid.set_inner_html("<div class='loading'>Loading templates...</div>");
        return Ok(());
    }

    if templates.is_empty() {
        grid.set_inner_html("<div class='empty-state'>No templates found.</div>");
        return Ok(());
    }

    // Render template cards
    for template in &templates {
        let card = create_template_card(document, template)?;
        grid.append_child(&card)?;
    }

    Ok(())
}

/// Create a template card element
fn create_template_card(
    document: &Document,
    template: &WorkflowTemplate,
) -> Result<Element, JsValue> {
    let card = document.create_element("div")?;
    card.set_class_name("template-card");

    let name = document.create_element("h3")?;
    name.set_class_name("template-name");
    name.set_text_content(Some(&template.name));

    let category = document.create_element("div")?;
    category.set_class_name("template-category");
    category.set_text_content(Some(&template.category));

    let description = document.create_element("p")?;
    description.set_class_name("template-description");
    description.set_text_content(template.description.as_deref());

    let tags = document.create_element("div")?;
    tags.set_class_name("template-tags");
    for tag in &template.tags {
        let tag_span = document.create_element("span")?;
        tag_span.set_class_name("template-tag");
        tag_span.set_text_content(Some(tag));
        tags.append_child(&tag_span)?;
    }

    let actions = document.create_element("div")?;
    actions.set_class_name("template-actions");

    let deploy_btn = document.create_element("button")?;
    deploy_btn.set_class_name("btn btn-primary");
    deploy_btn.set_text_content(Some("Deploy"));

    {
        let template_id = template.id;
        let template_name = template.name.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            // Simple deployment - could be enhanced with name/description customization
            dispatch_global_message(Message::DeployTemplate {
                template_id,
                name: format!("{} (Copy)", template_name),
                description: "Deployed from template gallery".to_string(),
            });

            // Close the gallery modal
            if let Some(overlay) = web_sys::window()
                .and_then(|w| w.document())
                .and_then(|d| d.get_element_by_id("template-gallery-overlay"))
            {
                let _ = overlay.remove();
            }
        }));
        deploy_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }

    actions.append_child(&deploy_btn)?;

    card.append_child(&name)?;
    card.append_child(&category)?;
    card.append_child(&description)?;
    card.append_child(&tags)?;
    card.append_child(&actions)?;

    Ok(card)
}
