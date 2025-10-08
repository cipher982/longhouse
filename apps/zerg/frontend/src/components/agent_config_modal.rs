//! Agent Configuration Modal – new component-style wrapper around the legacy
//! implementation that lived across multiple `ui/*` modules.  This is **step
//! one** of the ongoing refactor described in `modal_refactor.md` – we create
//! a single Rust module that owns the modal DOM and exposes a minimal public
//! API (`init`, `open`, `close`).
//!
//! For the moment we delegate most of the heavy lifting to the existing
//! helper functions in `crate::ui`, so behaviour is unchanged while we migrate
//! call-sites incrementally.  Future commits will move DOM creation and event
//! wiring here and delete the legacy helpers.

use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::Document;
use web_sys::Element;

use crate::components::tab_bar;
use crate::dom_utils;

use crate::constants::{
    ATTR_DATA_TESTID, CSS_ACTIONS_ROW, CSS_FORM_ROW_SM, DEFAULT_SYSTEM_INSTRUCTIONS,
    DEFAULT_TASK_INSTRUCTIONS,
};
use crate::models::Trigger;
use crate::state::dispatch_global_message;
use crate::state::APP_STATE;
use wasm_bindgen::closure::Closure;

/// Thin wrapper around the existing agent configuration modal.  The internal
/// fields will expand once we fully migrate DOM ownership.
#[derive(Clone, Debug)]
pub struct AgentConfigModal;

impl AgentConfigModal {
    /// Ensure the modal DOM exists.  If the element with id `agent-modal`
    /// is missing we **build the complete DOM tree here** (logic migrated
    /// from the former `ui::setup::create_agent_input_modal` helper).
    ///
    /// The operation is idempotent – if the DOM already exists we skip the
    /// heavy work and only make sure the listeners are attached once.
    pub fn init(document: &Document) -> Result<Self, JsValue> {
        // Build DOM on-demand
        if document.get_element_by_id("agent-modal").is_none() {
            Self::build_dom(document)?;
        }

        // Attach local event listeners only once – use a data attribute on
        // the modal root to mark completion.
        if let Some(modal) = document.get_element_by_id("agent-modal") {
            if modal.get_attribute("data-listeners-attached").is_none() {
                Self::attach_listeners(document)?;
                let _ = modal.set_attribute("data-listeners-attached", "true");
            }

            // -----------------------------------------------------------------
            // HOT-FIX: legacy HTML templates might already ship a modal DOM that
            // predates the new *Triggers* tab.  When such an element exists we
            // *augment* it in-place so users don't need to fully clear cache /
            // hard-refresh.  The check is idempotent – if the tab already
            // exists we skip all work.
            // -----------------------------------------------------------------

            if document.get_element_by_id("agent-triggers-tab").is_none() {
                // Add button next to Main tab (after any History tab if present)
                if let Some(tab_container) = document
                    .query_selector("#agent-modal .tab-container")
                    .ok()
                    .flatten()
                {
                    let triggers_tab = document.create_element("button")?;
                    triggers_tab.set_attribute("type", "button")?;
                    triggers_tab.set_class_name("tab-button");
                    triggers_tab.set_id("agent-triggers-tab");
                    triggers_tab.set_inner_html("Triggers");
                    tab_container.append_child(&triggers_tab)?;
                }

                // Add content wrapper equivalent to build_dom() variant
                if document
                    .get_element_by_id("agent-triggers-content")
                    .is_none()
                {
                    let triggers_content = document.create_element("div")?;
                    triggers_content.set_class_name("tab-content");
                    triggers_content.set_id("agent-triggers-content");
                    dom_utils::hide(&triggers_content);

                    // ----- empty wrapper with CTA -----
                    let empty_div = document.create_element("div")?;
                    empty_div.set_id("agent-triggers-empty");
                    let p = document.create_element("p")?;
                    p.set_inner_html(
                        "Connect external events to your agent – create a trigger to get started.",
                    );
                    empty_div.append_child(&p)?;

                    // Gmail connect row (only visible when not yet connected)
                    let gmail_row = document.create_element("div")?;
                    gmail_row.set_id("agent-gmail-connect-row");
                    gmail_row.set_class_name(CSS_FORM_ROW_SM);

                    // Button placeholder – we toggle visibility later in
                    // `render_gmail_connect_status`.
                    let connect_btn = document.create_element("button")?;
                    connect_btn.set_attribute("type", "button")?;
                    connect_btn.set_id("agent-connect-gmail-btn");
                    connect_btn.set_class_name("btn-primary");
                    connect_btn.set_inner_html("Connect Gmail");
                    gmail_row.append_child(&connect_btn)?;

                    let connected_span = document.create_element("span")?;
                    connected_span.set_id("agent-gmail-connected-span");
                    connected_span.set_inner_html("Gmail connected ✓");
                    // Static styling (colour + margin) – visibility handled via helper
                    connected_span.set_class_name("text-success ml-4");
                    dom_utils::hide(&connected_span);
                    gmail_row.append_child(&connected_span)?;

                    empty_div.append_child(&gmail_row)?;
                    let add_trigger_btn = document.create_element("button")?;
                    add_trigger_btn.set_attribute("type", "button")?;
                    add_trigger_btn.set_id("agent-add-trigger-btn");
                    add_trigger_btn.set_class_name("btn-primary");
                    add_trigger_btn.set_inner_html("Add Trigger");
                    empty_div.append_child(&add_trigger_btn)?;
                    triggers_content.append_child(&empty_div)?;

                    // ----- form card (hidden) -----
                    let form_card = document.create_element("div")?;
                    form_card.set_id("agent-add-trigger-form");
                    form_card.set_class_name("card");
                    form_card.set_class_name("form-card mt-12");
                    dom_utils::hide(&form_card);

                    let lbl = document.create_element("label")?;
                    lbl.set_inner_html("Type");
                    lbl.set_attribute("for", "agent-trigger-type-select")?;
                    form_card.append_child(&lbl)?;

                    let sel = document.create_element("select")?;
                    sel.set_id("agent-trigger-type-select");
                    sel.set_class_name("input-select");
                    let opt1 = document.create_element("option")?;
                    opt1.set_attribute("value", "webhook")?;
                    opt1.set_inner_html("Webhook – send POST requests");
                    sel.append_child(&opt1)?;
                    let opt2 = document.create_element("option")?;
                    opt2.set_attribute("value", "email")?;
                    opt2.set_inner_html("Email (Gmail)");
                    // Disable until gmail_connected flag is true – we toggle
                    // dynamically in `render_gmail_connect_status`.
                    opt2.set_attribute("data-gmail-option", "true")?;
                    sel.append_child(&opt2)?;
                    form_card.append_child(&sel)?;

                    let act = document.create_element("div")?;
                    act.set_class_name(CSS_ACTIONS_ROW);
                    let cancel_btn = document.create_element("button")?;
                    cancel_btn.set_attribute("type", "button")?;
                    cancel_btn.set_id("agent-cancel-add-trigger");
                    cancel_btn.set_class_name("btn");
                    cancel_btn.set_inner_html("Cancel");
                    act.append_child(&cancel_btn)?;
                    let create_btn = document.create_element("button")?;
                    create_btn.set_attribute("type", "button")?;
                    create_btn.set_id("agent-create-trigger");
                    create_btn.set_class_name("btn-primary");
                    create_btn.set_inner_html("Create Trigger");
                    act.append_child(&create_btn)?;
                    form_card.append_child(&act)?;
                    triggers_content.append_child(&form_card)?;

                    // ----- list -----
                    let list_ul = document.create_element("ul")?;
                    list_ul.set_id("agent-triggers-list");
                    list_ul.set_class_name("triggers-list");
                    list_ul.set_class_name("mt-12");
                    triggers_content.append_child(&list_ul)?;

                    // Append after existing tab-content containers
                    if let Some(modal_content) = document
                        .query_selector("#agent-modal .modal-content")
                        .ok()
                        .flatten()
                    {
                        modal_content.append_child(&triggers_content)?;
                    }
                }

                // Newly injected pieces need listeners.
                Self::attach_listeners(document)?;
            }
        }
        // Ensure Gmail connect status elements reflect current app state.
        let _ = render_gmail_connect_status(document);

        Ok(Self)
    }

    // ---------------------------------------------------------------------
    // DOM construction – migrated verbatim (with tiny tweaks) from
    // `ui::setup::create_agent_input_modal` so that the modal component is
    // now fully self-contained.
    // ---------------------------------------------------------------------
    fn build_dom(document: &Document) -> Result<(), JsValue> {
        use wasm_bindgen::closure::Closure;
        use wasm_bindgen::JsCast;

        // -----------------------------------------------------------------
        // Use shared helper so backdrop + content wrapper follow the same
        // conventions (`hidden` attribute for visibility, CSS classes, …)
        // -----------------------------------------------------------------
        let (modal, modal_content) =
            crate::components::modal::ensure_modal(document, "agent-modal")?;

        // Add data-testid for E2E testing
        let _ = modal.set_attribute("data-testid", "agent-modal");

        // Header
        let modal_header = document.create_element("div")?;
        modal_header.set_class_name("modal-header");

        let modal_title = document.create_element("h2")?;
        modal_title.set_id("modal-title");
        modal_title.set_inner_html("Agent Configuration");

        modal_header.append_child(&modal_title)?;

        // Tabs: Main / Triggers / Tools – built via shared helper ------------------
        let tab_container = tab_bar::build_tab_bar(
            document,
            &[("Main", true), ("Triggers", false), ("Tools", false)],
        )?;
        tab_container.set_class_name("modal-tabs");

        // -----------------------------------------------------------------
        // Attach tab-switch click handlers *immediately* so we do not rely on
        // querying by id later.  The helper guarantees the child order matches
        // the slice passed above.
        // -----------------------------------------------------------------
        {
            use wasm_bindgen::closure::Closure;
            use web_sys::Event;

            let dispatch = |tab| {
                crate::state::dispatch_global_message(crate::messages::Message::SetAgentTab(tab))
            };

            if let Some(first_btn) = tab_container.first_element_child() {
                // Retain stable id for update.rs active-state toggling.
                first_btn.set_id("agent-main-tab");
                let cb = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                    dispatch(crate::state::AgentConfigTab::Main);
                }));
                first_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }

            // Handle the second tab (Triggers)
            if let Some(triggers_btn) = tab_container
                .first_element_child()
                .and_then(|el| el.next_element_sibling())
            {
                triggers_btn.set_id("agent-triggers-tab");
                let cb = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                    dispatch(crate::state::AgentConfigTab::Triggers);
                }));
                triggers_btn
                    .add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }

            // Handle the third tab (Tools)
            if let Some(tools_btn) = tab_container.last_element_child() {
                tools_btn.set_id("agent-tools-tab");
                let cb = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                    dispatch(crate::state::AgentConfigTab::ToolsIntegrations);
                }));
                tools_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }
        }

        // Main content
        let main_content = document.create_element("div")?;
        main_content.set_class_name("tab-content");
        main_content.set_id("agent-main-content");

        // --------------------------------------------------------------
        // Triggers content wrapper (hidden by default)
        // --------------------------------------------------------------

        let triggers_content = document.create_element("div")?;
        triggers_content.set_class_name("tab-content");
        triggers_content.set_id("agent-triggers-content");
        // Hidden until the user clicks the tab button.
        dom_utils::hide(&triggers_content);

        // Placeholder message while we build out the real UI – avoids an
        // empty pane.
        // -----------------------------------------------------------------
        // Empty-state wrapper with CTA button
        // -----------------------------------------------------------------

        let empty_wrapper = document.create_element("div")?;
        empty_wrapper.set_id("agent-triggers-empty");

        let empty_p = document.create_element("p")?;
        empty_p.set_inner_html(
            "Connect external events to your agent – create a trigger to get started.",
        );
        empty_wrapper.append_child(&empty_p)?;

        let add_trigger_btn = document.create_element("button")?;
        add_trigger_btn.set_attribute("type", "button")?;
        add_trigger_btn.set_id("agent-add-trigger-btn");
        add_trigger_btn.set_class_name("btn-primary");
        add_trigger_btn.set_inner_html("Add Trigger");
        empty_wrapper.append_child(&add_trigger_btn)?;

        triggers_content.append_child(&empty_wrapper)?;

        // -----------------------------------------------------------------
        // Inline *Add Trigger* form (hidden until CTA clicked)
        // -----------------------------------------------------------------

        let form_card = document.create_element("div")?;
        form_card.set_id("agent-add-trigger-form");
        form_card.set_class_name("card");
        form_card.set_class_name("form-card mt-12");
        dom_utils::hide(&form_card);

        // Type label + select
        let type_label = document.create_element("label")?;
        type_label.set_inner_html("Type");
        type_label.set_attribute("for", "agent-trigger-type-select")?;

        let type_select = document.create_element("select")?;
        type_select.set_id("agent-trigger-type-select");
        type_select.set_class_name("input-select");

        // Option – webhook (enabled)
        let opt_webhook = document.create_element("option")?;
        opt_webhook.set_attribute("value", "webhook")?;
        opt_webhook.set_inner_html("Webhook – send POST requests");
        type_select.append_child(&opt_webhook)?;

        // Option – email (disabled until connected)
        let opt_gmail = document.create_element("option")?;
        opt_gmail.set_attribute("value", "email")?;
        opt_gmail.set_inner_html("Email (Gmail)");
        opt_gmail.set_attribute("disabled", "true")?;
        type_select.append_child(&opt_gmail)?;

        form_card.append_child(&type_label)?;
        form_card.append_child(&type_select)?;

        // Actions row
        let actions_div = document.create_element("div")?;
        actions_div.set_class_name("actions-row");
        actions_div.set_class_name("actions-row mt-12");

        let cancel_btn = document.create_element("button")?;
        cancel_btn.set_attribute("type", "button")?;
        cancel_btn.set_id("agent-cancel-add-trigger");
        cancel_btn.set_class_name("btn");
        cancel_btn.set_inner_html("Cancel");

        let create_btn = document.create_element("button")?;
        create_btn.set_attribute("type", "button")?;
        create_btn.set_id("agent-create-trigger");
        create_btn.set_class_name("btn-primary");
        create_btn.set_inner_html("Create Trigger");

        actions_div.append_child(&cancel_btn)?;
        actions_div.append_child(&create_btn)?;
        form_card.append_child(&actions_div)?;

        triggers_content.append_child(&form_card)?;

        // -----------------------------------------------------------------
        // Triggers list – populated dynamically
        // -----------------------------------------------------------------

        let list_el = document.create_element("ul")?;
        list_el.set_id("agent-triggers-list");
        list_el.set_class_name("triggers-list");
        list_el.set_class_name("mt-12");
        triggers_content.append_child(&list_el)?;

        // --------------------------------------------------------------
        // Tools content wrapper (hidden by default)
        // --------------------------------------------------------------

        let tools_content = document.create_element("div")?;
        tools_content.set_class_name("tab-content");
        tools_content.set_id("agent-tools-content");
        // Hidden until the user clicks the tab button.
        dom_utils::hide(&tools_content);

        // MCP Server Manager will be rendered here dynamically
        let mcp_container = document.create_element("div")?;
        mcp_container.set_id("agent-mcp-container");
        tools_content.append_child(&mcp_container)?;

        // --- Agent name ---
        let name_label = document.create_element("label")?;
        name_label.set_inner_html("Agent Name:");
        name_label.set_attribute("for", "agent-name")?;

        let name_input = document.create_element("input")?;
        name_input.set_id("agent-name");
        name_input.set_attribute("type", "text")?;
        name_input.set_attribute("placeholder", "Enter agent name...")?;
        name_input.set_attribute(ATTR_DATA_TESTID, "agent-name-input")?;

        // --- System instructions ---
        let system_label = document.create_element("label")?;
        system_label.set_inner_html("System Instructions:");
        system_label.set_attribute("for", "system-instructions")?;

        let system_textarea = document.create_element("textarea")?;
        system_textarea.set_id("system-instructions");
        system_textarea.set_attribute("rows", "6")?;
        system_textarea.set_attribute(
            "placeholder",
            "Enter system-level instructions for this agent...",
        )?;
        system_textarea.set_attribute(ATTR_DATA_TESTID, "system-instructions-textarea")?;

        // --- Task instructions ---
        let default_task_label = document.create_element("label")?;
        default_task_label.set_inner_html("Task Instructions:");
        default_task_label.set_attribute("for", "default-task-instructions")?;

        let default_task_textarea = document.create_element("textarea")?;
        default_task_textarea.set_id("default-task-instructions");
        default_task_textarea.set_attribute("rows", "4")?;
        default_task_textarea.set_attribute("placeholder", "Optional task instructions that will be used when running this agent. If empty, a default 'begin' prompt will be used.")?;
        default_task_textarea.set_attribute(ATTR_DATA_TESTID, "task-instructions-textarea")?;

        // Append to main_content
        main_content.append_child(&name_label)?;
        main_content.append_child(&name_input)?;
        main_content.append_child(&system_label)?;
        main_content.append_child(&system_textarea)?;
        main_content.append_child(&default_task_label)?;
        main_content.append_child(&default_task_textarea)?;

        // --- NEW: Schedule controls (Frequency builder) ---
        let sched_container = document.create_element("div")?;
        sched_container.set_id("schedule-controls");

        // Frequency dropdown
        let freq_label = document.create_element("label")?;
        freq_label.set_inner_html("Frequency:");
        freq_label.set_attribute("for", "sched-frequency")?;

        let freq_select = document.create_element("select")?;
        freq_select.set_id("sched-frequency");

        let frequencies = [
            ("none", "Not scheduled"),
            ("minutes", "Every N minutes"),
            ("hourly", "Hourly"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
            ("monthly", "Monthly"),
        ];

        for (value, label) in &frequencies {
            let option = document.create_element("option")?;
            option.set_attribute("value", value)?;
            option.set_inner_html(label);
            freq_select.append_child(&option)?;
        }

        // Create labeled containers for each input
        let create_input_container = |id: &str,
                                      label_text: &str,
                                      min: &str,
                                      max: &str,
                                      default_val: &str|
         -> Result<Element, JsValue> {
            let container = document.create_element("div")?;
            container.set_id(&format!("{}-container", id));
            container.set_class_name("schedule-input-container");
            crate::dom_utils::hide(&container);

            let label = document.create_element("label")?;
            label.set_inner_html(label_text);
            label.set_attribute("for", id)?;

            let input = document.create_element("input")?;
            input.set_id(id);
            input.set_attribute("type", "number")?;
            input.set_attribute("min", min)?;
            input.set_attribute("max", max)?;
            input.set_attribute("placeholder", default_val)?;
            input.set_attribute("value", default_val)?; // Set actual default value

            container.append_child(&label)?;
            container.append_child(&input)?;
            Ok(container)
        };

        // Special function for weekday select
        let create_weekday_container =
            |id: &str, label_text: &str, default_day: u8| -> Result<Element, JsValue> {
                let container = document.create_element("div")?;
                container.set_id(&format!("{}-container", id));
                container.set_class_name("schedule-input-container");
                crate::dom_utils::hide(&container);

                let label = document.create_element("label")?;
                label.set_inner_html(label_text);
                label.set_attribute("for", id)?;

                let select = document.create_element("select")?;
                select.set_id(id);

                let days = [
                    (0, "Sunday"),
                    (1, "Monday"),
                    (2, "Tuesday"),
                    (3, "Wednesday"),
                    (4, "Thursday"),
                    (5, "Friday"),
                    (6, "Saturday"),
                ];

                for (value, name) in &days {
                    let option = document.create_element("option")?;
                    option.set_attribute("value", &value.to_string())?;
                    if *value == default_day {
                        option.set_attribute("selected", "true")?;
                    }
                    option.set_inner_html(name);
                    select.append_child(&option)?;
                }

                container.append_child(&label)?;
                container.append_child(&select)?;
                Ok(container)
            };

        // Create containers for each input type
        let interval_container = create_input_container(
            "sched-interval",
            "Run every N minutes (1-59):",
            "1",
            "59",
            "15",
        )?;

        let hour_container = create_input_container("sched-hour", "Hour (0-23):", "0", "23", "9")?;

        let minute_container =
            create_input_container("sched-minute", "Minute (0-59):", "0", "59", "0")?;

        let weekday_container = create_weekday_container(
            "sched-weekday",
            "Day of Week:",
            1, // Default to Monday (1)
        )?;

        let day_container =
            create_input_container("sched-day", "Day of Month (1-31):", "1", "31", "15")?;

        // Summary line
        let summary_div = document.create_element("div")?;
        summary_div.set_id("sched-summary");
        summary_div.set_class_name("summary-text mt-6");
        summary_div.set_inner_html("No schedule set");

        // Assemble schedule container
        sched_container.append_child(&freq_label)?;
        sched_container.append_child(&freq_select)?;
        sched_container.append_child(&interval_container)?;
        sched_container.append_child(&hour_container)?;
        sched_container.append_child(&minute_container)?;
        sched_container.append_child(&weekday_container)?;
        sched_container.append_child(&day_container)?;
        sched_container.append_child(&summary_div)?;

        // Set default value "none"
        if let Some(select_el) = freq_select.dyn_ref::<web_sys::HtmlSelectElement>() {
            select_el.set_value("none");
        }

        // Append schedule container to main_content
        main_content.append_child(&sched_container)?;

        // --------------------------------------------------------------
        // Final assembly – add both tab contents to modal
        // --------------------------------------------------------------

        // --- Buttons ---
        let button_container = document.create_element("div")?;
        button_container.set_class_name("modal-buttons");

        let save_button = document.create_element("button")?;
        save_button.set_attribute("type", "button")?;
        save_button.set_id("save-agent");
        save_button.set_class_name("btn-primary");
        save_button.set_inner_html("Save");

        button_container.append_child(&save_button)?;

        // Assemble content hierarchy
        modal_content.append_child(&modal_header)?;
        modal_content.append_child(&tab_container)?;
        modal_content.append_child(&main_content)?;
        modal_content.append_child(&triggers_content)?;
        modal_content.append_child(&tools_content)?;
        modal_content.append_child(&button_container)?;

        // ------------------------------------------------------------------
        // UX: Close when clicking on backdrop (outside modal-content)
        // ------------------------------------------------------------------
        if modal.get_attribute("data-overlay-listener").is_none() {
            let modal_clone = modal.clone();
            let cb = Closure::<dyn FnMut(web_sys::MouseEvent)>::wrap(Box::new(
                move |evt: web_sys::MouseEvent| {
                    if let Some(target) = evt.target() {
                        if let Ok(target_node) = target.dyn_into::<web_sys::Node>() {
                            if modal_clone.is_same_node(Some(&target_node)) {
                                if let Some(window) = web_sys::window() {
                                    if let Some(doc) = window.document() {
                                        let _ = Self::close(&doc);
                                    }
                                }
                            }
                        }
                    }
                },
            ));
            modal.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
            modal.set_attribute("data-overlay-listener", "true")?;
        }

        // Stop propagation inside content box
        if modal_content
            .get_attribute("data-stop-propagation")
            .is_none()
        {
            let stopper = Closure::<dyn FnMut(web_sys::MouseEvent)>::wrap(Box::new(
                |e: web_sys::MouseEvent| {
                    e.stop_propagation();
                },
            ));
            modal_content
                .add_event_listener_with_callback("click", stopper.as_ref().unchecked_ref())?;
            stopper.forget();
            modal_content.set_attribute("data-stop-propagation", "true")?;
        }

        Ok(())
    }

    /// Internal helper – adds event listeners to modal controls.  Calling it
    /// multiple times is safe because we guard with `data-listeners-attached`.
    fn attach_listeners(document: &Document) -> Result<(), JsValue> {
        use wasm_bindgen::closure::Closure;
        use web_sys::{Event, HtmlInputElement, HtmlTextAreaElement};

        // Helper: dispatch via global function
        let dispatch = |msg| crate::state::dispatch_global_message(msg);

        // Tab switching click handlers are now attached immediately after
        // `build_tab_bar()` so we no longer wire them up here.

        // -------- Add Trigger flow UI ----------
        // Show form
        if let Some(add_btn) = document.get_element_by_id("agent-add-trigger-btn") {
            let cb_show = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    if let Some(empty_div) = doc.get_element_by_id("agent-triggers-empty") {
                        dom_utils::hide(&empty_div);
                    }
                    if let Some(form) = doc.get_element_by_id("agent-add-trigger-form") {
                        dom_utils::show(&form);
                    }
                }
            }));
            add_btn.add_event_listener_with_callback("click", cb_show.as_ref().unchecked_ref())?;
            cb_show.forget();
        }

        // Cancel form
        if let Some(cancel_btn) = document.get_element_by_id("agent-cancel-add-trigger") {
            let cb_cancel = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    if let Some(form) = doc.get_element_by_id("agent-add-trigger-form") {
                        dom_utils::hide(&form);
                    }
                    if let Some(empty_div) = doc.get_element_by_id("agent-triggers-empty") {
                        dom_utils::show(&empty_div);
                    }
                }
            }));
            cancel_btn
                .add_event_listener_with_callback("click", cb_cancel.as_ref().unchecked_ref())?;
            cb_cancel.forget();
        }

        // Create trigger
        if let Some(create_btn) = document.get_element_by_id("agent-create-trigger") {
            let cb_create = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    // Determine agent_id
                    let agent_id_opt = doc
                        .get_element_by_id("agent-modal")
                        .and_then(|m| m.get_attribute("data-agent-id"))
                        .and_then(|s| s.parse::<u32>().ok());
                    if agent_id_opt.is_none() {
                        return;
                    }
                    let agent_id = agent_id_opt.unwrap();

                    // Read type value
                    let type_value = doc
                        .get_element_by_id("agent-trigger-type-select")
                        .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
                        .map(|sel| sel.value())
                        .unwrap_or_else(|| "webhook".to_string());

                    let payload_json = if type_value == "email" {
                        let (connected, connector_id) = crate::state::APP_STATE.with(|st| {
                            let st = st.borrow();
                            (st.gmail_connected, st.gmail_connector_id)
                        });
                        if !connected {
                            return; // not connected
                        }
                        let cid = match connector_id {
                            Some(v) => v,
                            None => {
                                // Connector id unknown – UI cannot proceed yet
                                return;
                            }
                        };
                        format!(
                            "{{\"agent_id\": {}, \"type\": \"email\", \"config\": {{\"connector_id\": {}}}}}",
                            agent_id, cid
                        )
                    } else {
                        format!(
                            "{{\"agent_id\": {}, \"type\": \"{}\"}}",
                            agent_id, type_value
                        )
                    };
                    dispatch_global_message(crate::messages::Message::RequestCreateTrigger {
                        payload_json,
                    });

                    // Hide form, show empty again – list will refresh on success
                    if let Some(form) = doc.get_element_by_id("agent-add-trigger-form") {
                        dom_utils::hide(&form);
                    }
                    if let Some(empty_div) = doc.get_element_by_id("agent-triggers-empty") {
                        dom_utils::show(&empty_div);
                    }
                }
            }));
            create_btn
                .add_event_listener_with_callback("click", cb_create.as_ref().unchecked_ref())?;
            cb_create.forget();
        }

        // Gmail Connect button
        if let Some(conn_btn) = document.get_element_by_id("agent-connect-gmail-btn") {
            let cb_conn = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                // Kick off OAuth flow (frontend stub for now)
                crate::auth::google_code_flow::initiate_gmail_connect();
            }));
            conn_btn.add_event_listener_with_callback("click", cb_conn.as_ref().unchecked_ref())?;
            cb_conn.forget();
        }

        // Legacy prompt-based handler removed.

        // Save
        if let Some(save_btn) = document.get_element_by_id("save-agent") {
            let cb = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                let window = web_sys::window().unwrap();
                let document = window.document().unwrap();

                let name_value = document
                    .get_element_by_id("agent-name")
                    .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                    .map(|i| i.value())
                    .unwrap_or_default();

                let system_instructions = document
                    .get_element_by_id("system-instructions")
                    .and_then(|e| e.dyn_into::<HtmlTextAreaElement>().ok())
                    .map(|t| t.value())
                    .unwrap_or_default();

                let task_instructions = document
                    .get_element_by_id("default-task-instructions")
                    .and_then(|e| e.dyn_into::<HtmlTextAreaElement>().ok())
                    .map(|t| t.value())
                    .unwrap_or_default();

                // NEW schedule builder: read controls and convert to cron
                let schedule_value_opt = {
                    let freq_sel_val = document
                        .get_element_by_id("sched-frequency")
                        .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
                        .map(|i| i.value());

                    match freq_sel_val.as_deref() {
                        Some("minutes") => document
                            .get_element_by_id("sched-interval")
                            .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                            .and_then(|i| i.value().parse::<u8>().ok())
                            .map(|n| crate::scheduling::Frequency::EveryNMinutes(n).to_cron()),
                        Some("hourly") => document
                            .get_element_by_id("sched-minute")
                            .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                            .and_then(|i| i.value().parse::<u8>().ok())
                            .map(|m| crate::scheduling::Frequency::Hourly { minute: m }.to_cron()),
                        Some("daily") => {
                            let hour_opt = document
                                .get_element_by_id("sched-hour")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            let minute_opt = document
                                .get_element_by_id("sched-minute")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            match (hour_opt, minute_opt) {
                                (Some(h), Some(m)) => Some(
                                    crate::scheduling::Frequency::Daily { hour: h, minute: m }
                                        .to_cron(),
                                ),
                                _ => None,
                            }
                        }
                        Some("weekly") => {
                            let weekday_opt = document
                                .get_element_by_id("sched-weekday")
                                .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
                                .and_then(|s| s.value().parse::<u8>().ok());
                            let hour_opt = document
                                .get_element_by_id("sched-hour")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            let minute_opt = document
                                .get_element_by_id("sched-minute")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            match (weekday_opt, hour_opt, minute_opt) {
                                (Some(w), Some(h), Some(m)) => Some(
                                    crate::scheduling::Frequency::Weekly {
                                        weekday: w,
                                        hour: h,
                                        minute: m,
                                    }
                                    .to_cron(),
                                ),
                                _ => None,
                            }
                        }
                        Some("monthly") => {
                            let day_opt = document
                                .get_element_by_id("sched-day")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            let hour_opt = document
                                .get_element_by_id("sched-hour")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            let minute_opt = document
                                .get_element_by_id("sched-minute")
                                .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                                .and_then(|i| i.value().parse::<u8>().ok());
                            match (day_opt, hour_opt, minute_opt) {
                                (Some(day), Some(h), Some(m)) => Some(
                                    crate::scheduling::Frequency::Monthly {
                                        day,
                                        hour: h,
                                        minute: m,
                                    }
                                    .to_cron(),
                                ),
                                _ => None,
                            }
                        }
                        Some("none") | _ => None,
                    }
                };

                let selected_model =
                    crate::state::APP_STATE.with(|s| s.borrow().selected_model.clone());

                dispatch(crate::messages::Message::SaveAgentDetails {
                    name: name_value,
                    system_instructions,
                    task_instructions,
                    model: selected_model,
                    schedule: schedule_value_opt,
                });

                // ------------------------------------------------------------------
                // UX feedback: immediately disable the Save button and change its
                // label to “Saving…”.  The button (and modal) will be restored or
                // closed by the success / error handlers triggered once the
                // Command::UpdateAgent network call completes.
                // ------------------------------------------------------------------

                if let Some(btn) = document.get_element_by_id("save-agent") {
                    btn.set_inner_html("Saving…");
                    let _ = btn.set_attribute("disabled", "true");
                }
            }));
            save_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }

        // Send task
        if let Some(send_btn) = document.get_element_by_id("send-task") {
            let cb = Closure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                dispatch(crate::messages::Message::SendTaskToAgent);
            }));
            send_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
            cb.forget();
        }

        // ----------------- Scheduling UI handlers --------------------
        use wasm_bindgen::closure::Closure as WasmClosure;
        use web_sys::HtmlSelectElement;

        // Helper to toggle visibility of input fields
        let toggle_inputs = |freq: &str, doc: &Document| {
            let set_vis = |id: &str, show: bool| {
                if let Some(el) = doc.get_element_by_id(&format!("{}-container", id)) {
                    if show {
                        dom_utils::show(&el);
                    } else {
                        dom_utils::hide(&el);
                    }
                }
            };

            match freq {
                "minutes" => {
                    set_vis("sched-interval", true);
                    set_vis("sched-hour", false);
                    set_vis("sched-minute", false);
                    set_vis("sched-weekday", false);
                    set_vis("sched-day", false);
                }
                "hourly" => {
                    set_vis("sched-interval", false);
                    set_vis("sched-hour", false);
                    set_vis("sched-minute", true);
                    set_vis("sched-weekday", false);
                    set_vis("sched-day", false);
                }
                "daily" => {
                    set_vis("sched-interval", false);
                    set_vis("sched-hour", true);
                    set_vis("sched-minute", true);
                    set_vis("sched-weekday", false);
                    set_vis("sched-day", false);
                }
                "weekly" => {
                    set_vis("sched-interval", false);
                    set_vis("sched-hour", true);
                    set_vis("sched-minute", true);
                    set_vis("sched-weekday", true);
                    set_vis("sched-day", false);
                }
                "monthly" => {
                    set_vis("sched-interval", false);
                    set_vis("sched-hour", true);
                    set_vis("sched-minute", true);
                    set_vis("sched-weekday", false);
                    set_vis("sched-day", true);
                }
                "none" | _ => {
                    // hide all
                    set_vis("sched-interval", false);
                    set_vis("sched-hour", false);
                    set_vis("sched-minute", false);
                    set_vis("sched-weekday", false);
                    set_vis("sched-day", false);
                }
            }
        };

        // Function to update summary line
        let update_summary = |doc: &Document| {
            // Acquire Frequency from current UI state via helper inside Save logic.
            let freq_sel_val = doc
                .get_element_by_id("sched-frequency")
                .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
                .map(|i| i.value());

            let frequency_opt = match freq_sel_val.as_deref() {
                Some("minutes") => doc
                    .get_element_by_id("sched-interval")
                    .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                    .and_then(|i| i.value().parse::<u8>().ok())
                    .map(crate::scheduling::Frequency::EveryNMinutes),
                Some("hourly") => doc
                    .get_element_by_id("sched-minute")
                    .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                    .and_then(|i| i.value().parse::<u8>().ok())
                    .map(|m| crate::scheduling::Frequency::Hourly { minute: m }),
                Some("daily") => {
                    let hour_opt = doc
                        .get_element_by_id("sched-hour")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    let minute_opt = doc
                        .get_element_by_id("sched-minute")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    match (hour_opt, minute_opt) {
                        (Some(h), Some(m)) => {
                            Some(crate::scheduling::Frequency::Daily { hour: h, minute: m })
                        }
                        _ => None,
                    }
                }
                Some("weekly") => {
                    let weekday_opt = doc
                        .get_element_by_id("sched-weekday")
                        .and_then(|e| e.dyn_into::<web_sys::HtmlSelectElement>().ok())
                        .and_then(|s| s.value().parse::<u8>().ok());
                    let hour_opt = doc
                        .get_element_by_id("sched-hour")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    let minute_opt = doc
                        .get_element_by_id("sched-minute")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    match (weekday_opt, hour_opt, minute_opt) {
                        (Some(w), Some(h), Some(m)) => Some(crate::scheduling::Frequency::Weekly {
                            weekday: w,
                            hour: h,
                            minute: m,
                        }),
                        _ => None,
                    }
                }
                Some("monthly") => {
                    let day_opt = doc
                        .get_element_by_id("sched-day")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    let hour_opt = doc
                        .get_element_by_id("sched-hour")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    let minute_opt = doc
                        .get_element_by_id("sched-minute")
                        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
                        .and_then(|i| i.value().parse::<u8>().ok());
                    match (day_opt, hour_opt, minute_opt) {
                        (Some(d), Some(h), Some(m)) => {
                            Some(crate::scheduling::Frequency::Monthly {
                                day: d,
                                hour: h,
                                minute: m,
                            })
                        }
                        _ => None,
                    }
                }
                Some("none") | _ => None,
            };

            if let Some(summary_el) = doc.get_element_by_id("sched-summary") {
                let text = frequency_opt
                    .map(|f| f.to_string())
                    .unwrap_or_else(|| "No schedule set".to_string());
                summary_el.set_inner_html(&text);
            }
        };

        // Attach change listeners to frequency select
        if let Some(freq_sel) = document.get_element_by_id("sched-frequency") {
            if let Ok(freq_select) = freq_sel.dyn_into::<HtmlSelectElement>() {
                let doc_clone = document.clone();
                let select_for_listener = freq_select.clone();
                let cb = WasmClosure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                    let val = select_for_listener.value();
                    toggle_inputs(&val, &doc_clone);
                    update_summary(&doc_clone);
                }));
                freq_select
                    .add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }
        }

        // Attach input listeners to all schedule inputs to refresh summary
        let input_ids = [
            "sched-interval",
            "sched-hour",
            "sched-minute",
            "sched-weekday",
            "sched-day",
        ];
        for id in &input_ids {
            if let Some(inp) = document.get_element_by_id(id) {
                let doc_clone = document.clone();
                let cb = WasmClosure::<dyn FnMut(Event)>::wrap(Box::new(move |_e: Event| {
                    update_summary(&doc_clone);
                }));
                inp.add_event_listener_with_callback("input", cb.as_ref().unchecked_ref())?;
                cb.forget();
            }
        }

        // --- Initial state: ensure correct inputs visibility & summary ---
        if let Some(freq_sel) = document.get_element_by_id("sched-frequency") {
            if let Some(select) = freq_sel.dyn_ref::<web_sys::HtmlSelectElement>() {
                let val = select.value();
                toggle_inputs(&val, document);
                update_summary(document);
            }
        }

        Ok(())
    }

    /// Open the modal for the given `agent_id`.
    pub fn open(document: &Document, agent_id: u32) -> Result<(), JsValue> {
        // Ensure DOM exists first
        let _ = Self::init(document);

        // The logic below is largely copied from the former
        // `ui::modals::open_agent_modal` implementation with only minimal
        // changes (namespacing, error handling).

        // --------------------------------------------------------------
        // 1. Gather data from AppState
        // --------------------------------------------------------------
        let (agent_name, system_instructions, task_instructions, schedule_value) =
            APP_STATE.with(|state| {
                let state = state.borrow();
                if let Some(agent) = state.agents.get(&agent_id) {
                    (
                        agent.name.clone(),
                        agent.system_instructions.clone().unwrap_or_default(),
                        agent.task_instructions.clone().unwrap_or_default(),
                        agent.schedule.clone(),
                    )
                } else {
                    (
                        "Unnamed Agent".to_string(),
                        String::new(),
                        String::new(),
                        None,
                    )
                }
            });

        // --------------------------------------------------------------
        // 2. Populate DOM elements
        // --------------------------------------------------------------

        // --------------------------------------------------------------
        // Ensure *model selector* exists and is populated once.
        // --------------------------------------------------------------

        if document.get_element_by_id("model-select").is_none() {
            if let Some(main_tab) = document.get_element_by_id("agent-main-content") {
                // Create wrapper div with same small-row CSS as other fields
                let model_row = document.create_element("div")?;
                model_row.set_class_name(crate::constants::CSS_FORM_ROW_SM);

                let label = document.create_element("label")?;
                label.set_inner_html("Model");
                label.set_attribute("for", "model-select")?;
                model_row.append_child(&label)?;

                let select_el = document.create_element("select")?;
                select_el.set_id("model-select");
                model_row.append_child(&select_el)?;

                main_tab.append_child(&model_row)?;
            }
        }

        // Populate options + attach change handler (idempotent).
        let _ = crate::components::model_selector::update_model_dropdown(document);
        let _ = crate::components::model_selector::setup_model_selector_handlers(document);

        // Store current agent id for later
        if let Some(modal) = document.get_element_by_id("agent-modal") {
            let _ = modal.set_attribute("data-agent-id", &agent_id.to_string());
        }

        // Title
        if let Some(modal_title) = document.get_element_by_id("modal-title") {
            modal_title.set_inner_html(&format!("Agent: {}", agent_name));
        }

        // Name input
        if let Some(name_elem) = document.get_element_by_id("agent-name") {
            if let Some(input) = name_elem.dyn_ref::<web_sys::HtmlInputElement>() {
                input.set_value(&agent_name);
            }
        }

        // System instructions (with default fallback)
        let sys_val = if system_instructions.trim().is_empty() {
            DEFAULT_SYSTEM_INSTRUCTIONS.to_string()
        } else {
            system_instructions
        };

        if let Some(elem) = document.get_element_by_id("system-instructions") {
            if let Some(txt) = elem.dyn_ref::<web_sys::HtmlTextAreaElement>() {
                txt.set_value(&sys_val);
            }
        }

        // Task instructions (with default fallback)
        let task_val = if task_instructions.trim().is_empty() {
            DEFAULT_TASK_INSTRUCTIONS.to_string()
        } else {
            task_instructions
        };

        if let Some(elem) = document.get_element_by_id("default-task-instructions") {
            if let Some(txt) = elem.dyn_ref::<web_sys::HtmlTextAreaElement>() {
                txt.set_value(&task_val);
            }
        }

        // NEW schedule UI – pre-fill controls based on existing cron string
        if let Some(schedule_str) = schedule_value {
            if !schedule_str.trim().is_empty() {
                if let Ok(freq) = crate::scheduling::Frequency::try_from(schedule_str.as_str()) {
                    // ------------------------------------------------------------------
                    // Apply the parsed schedule directly to the DOM.  No read-back from
                    // the <select> necessary – this avoids timing races and keeps the UI
                    // a pure function of our Rust state.
                    // ------------------------------------------------------------------

                    fn apply_frequency_ui(
                        freq: &crate::scheduling::Frequency,
                        document: &web_sys::Document,
                    ) {
                        // Helper: set input/select value to numeric u8
                        let set_input_value = |id: &str, val: u8| {
                            if let Some(el) = document.get_element_by_id(id) {
                                if let Some(i) = el.dyn_ref::<web_sys::HtmlInputElement>() {
                                    i.set_value(&val.to_string());
                                } else if let Some(sel) = el.dyn_ref::<web_sys::HtmlSelectElement>()
                                {
                                    sel.set_value(&val.to_string());
                                }
                            }
                        };

                        // Determine key used by dropdown & visibility matcher
                        let key = match *freq {
                            crate::scheduling::Frequency::EveryNMinutes(_) => "minutes",
                            crate::scheduling::Frequency::Hourly { .. } => "hourly",
                            crate::scheduling::Frequency::Daily { .. } => "daily",
                            crate::scheduling::Frequency::Weekly { .. } => "weekly",
                            crate::scheduling::Frequency::Monthly { .. } => "monthly",
                        };

                        // --- Dropdown ---
                        if let Some(sel_el) = document.get_element_by_id("sched-frequency") {
                            if let Some(sel) = sel_el.dyn_ref::<web_sys::HtmlSelectElement>() {
                                sel.set_value(key);
                            }
                        }

                        // --- Individual inputs ---
                        match *freq {
                            crate::scheduling::Frequency::EveryNMinutes(n) => {
                                set_input_value("sched-interval", n);
                            }
                            crate::scheduling::Frequency::Hourly { minute } => {
                                set_input_value("sched-minute", minute);
                            }
                            crate::scheduling::Frequency::Daily { hour, minute } => {
                                set_input_value("sched-hour", hour);
                                set_input_value("sched-minute", minute);
                            }
                            crate::scheduling::Frequency::Weekly {
                                weekday,
                                hour,
                                minute,
                            } => {
                                set_input_value("sched-weekday", weekday);
                                set_input_value("sched-hour", hour);
                                set_input_value("sched-minute", minute);
                            }
                            crate::scheduling::Frequency::Monthly { day, hour, minute } => {
                                set_input_value("sched-day", day);
                                set_input_value("sched-hour", hour);
                                set_input_value("sched-minute", minute);
                            }
                        }

                        // --- Visibility toggles ---
                        let set_vis = |id: &str, show: bool| {
                            if let Some(el) =
                                document.get_element_by_id(&format!("{}-container", id))
                            {
                                if show {
                                    dom_utils::show(&el);
                                } else {
                                    dom_utils::hide(&el);
                                }
                            }
                        };

                        match key {
                            "minutes" => {
                                set_vis("sched-interval", true);
                                set_vis("sched-hour", false);
                                set_vis("sched-minute", false);
                                set_vis("sched-weekday", false);
                                set_vis("sched-day", false);
                            }
                            "hourly" => {
                                set_vis("sched-interval", false);
                                set_vis("sched-hour", false);
                                set_vis("sched-minute", true);
                                set_vis("sched-weekday", false);
                                set_vis("sched-day", false);
                            }
                            "daily" => {
                                set_vis("sched-interval", false);
                                set_vis("sched-hour", true);
                                set_vis("sched-minute", true);
                                set_vis("sched-weekday", false);
                                set_vis("sched-day", false);
                            }
                            "weekly" => {
                                set_vis("sched-interval", false);
                                set_vis("sched-hour", true);
                                set_vis("sched-minute", true);
                                set_vis("sched-weekday", true);
                                set_vis("sched-day", false);
                            }
                            "monthly" => {
                                set_vis("sched-interval", false);
                                set_vis("sched-hour", true);
                                set_vis("sched-minute", true);
                                set_vis("sched-weekday", false);
                                set_vis("sched-day", true);
                            }
                            _ => {
                                set_vis("sched-interval", false);
                                set_vis("sched-hour", false);
                                set_vis("sched-minute", false);
                                set_vis("sched-weekday", false);
                                set_vis("sched-day", false);
                            }
                        }

                        // --- Summary line ---
                        if let Some(summary_el) = document.get_element_by_id("sched-summary") {
                            summary_el.set_inner_html(&freq.to_string());
                        }
                    }

                    apply_frequency_ui(&freq, &document);
                }
            } else {
                // Empty string schedule - reset to "none"
                if let Some(sel_el) = document.get_element_by_id("sched-frequency") {
                    if let Some(sel) = sel_el.dyn_ref::<web_sys::HtmlSelectElement>() {
                        sel.set_value("none");
                    }
                }
                // Hide all input containers
                let set_vis = |id: &str, show: bool| {
                    if let Some(el) = document.get_element_by_id(&format!("{}-container", id)) {
                        if show {
                            dom_utils::show(&el);
                        } else {
                            dom_utils::hide(&el);
                        }
                    }
                };
                set_vis("sched-interval", false);
                set_vis("sched-hour", false);
                set_vis("sched-minute", false);
                set_vis("sched-weekday", false);
                set_vis("sched-day", false);
                // Reset summary
                if let Some(summary_el) = document.get_element_by_id("sched-summary") {
                    summary_el.set_inner_html("No schedule set");
                }
            }
        } else {
            // No schedule at all (None) - reset to "none"
            if let Some(sel_el) = document.get_element_by_id("sched-frequency") {
                if let Some(sel) = sel_el.dyn_ref::<web_sys::HtmlSelectElement>() {
                    sel.set_value("none");
                }
            }
            // Hide all input containers
            let set_vis = |id: &str, show: bool| {
                if let Some(el) = document.get_element_by_id(&format!("{}-container", id)) {
                    if show {
                        dom_utils::show(&el);
                    } else {
                        dom_utils::hide(&el);
                    }
                }
            };
            set_vis("sched-interval", false);
            set_vis("sched-hour", false);
            set_vis("sched-minute", false);
            set_vis("sched-weekday", false);
            set_vis("sched-day", false);
            // Reset summary
            if let Some(summary_el) = document.get_element_by_id("sched-summary") {
                summary_el.set_inner_html("No schedule set");
            }
        }

        // Finally, show the modal
        if let Some(modal) = document.get_element_by_id("agent-modal") {
            crate::components::modal::show(&modal);
        }

        Ok(())
    }

    /// Hide the modal.
    pub fn close(document: &Document) -> Result<(), JsValue> {
        if let Some(modal) = document.get_element_by_id("agent-modal") {
            crate::components::modal::hide(&modal);
        }
        Ok(())
    }
}

// -----------------------------------------------------------------------------
// Public helpers – rendered from update.rs when gmail_connected flag changes
// -----------------------------------------------------------------------------

/// Refresh visibility of the Gmail connect UI row depending on
/// `APP_STATE.gmail_connected`.
pub fn render_gmail_connect_status(document: &Document) -> Result<(), JsValue> {
    let connected = crate::state::APP_STATE.with(|st| st.borrow().gmail_connected);

    if let Some(btn) = document.get_element_by_id("agent-connect-gmail-btn") {
        if connected {
            dom_utils::hide(&btn);
        } else {
            dom_utils::show(&btn);
        }
    }
    if let Some(span) = document.get_element_by_id("agent-gmail-connected-span") {
        // Ensure colour always applied
        span.set_class_name("text-success ml-4");
        if connected {
            dom_utils::show(&span);
        } else {
            dom_utils::hide(&span);
        }
    }

    // Enable/disable <option data-gmail-option>
    if let Some(sel) = document.get_element_by_id("agent-trigger-type-select") {
        if let Ok(select_el) = sel.dyn_into::<web_sys::HtmlSelectElement>() {
            for i in 0..select_el.length() {
                if let Some(opt) = select_el.item(i) {
                    if opt.get_attribute("data-gmail-option").is_some() {
                        if connected {
                            let _ = opt.remove_attribute("disabled");
                        } else {
                            let _ = opt.set_attribute("disabled", "true");
                        }
                    }
                }
            }
        }
    }
    Ok(())
}

// -----------------------------------------------------------------------------
// Public helpers – trigger list rendering & interactivity
// -----------------------------------------------------------------------------

/// Re-render the `<ul id="agent-triggers-list">` element for the given agent.
/// The function is idempotent – the list is cleared on every call so we can
/// safely call after every state change.  It attaches new event listeners for
/// copy / delete buttons on each row.
pub fn render_triggers_list(document: &Document, agent_id: u32) -> Result<(), JsValue> {
    use wasm_bindgen::JsCast;

    // Resolve list element.
    let list_el = match document.get_element_by_id("agent-triggers-list") {
        Some(el) => el,
        None => return Ok(()), // Not visible (tab closed)
    };

    // Fetch triggers for this agent from global state.
    let triggers: Vec<Trigger> = APP_STATE.with(|state_rc| {
        state_rc
            .borrow()
            .triggers
            .get(&agent_id)
            .cloned()
            .unwrap_or_default()
    });

    // Toggle empty wrapper visibility
    if let Some(empty_div) = document.get_element_by_id("agent-triggers-empty") {
        if triggers.is_empty() {
            dom_utils::show(&empty_div);
        } else {
            dom_utils::hide(&empty_div);
        }
    }

    // Clear current list.
    list_el.set_inner_html("");

    for trig in triggers.iter() {
        // <li class="trigger-item">
        let li = document.create_element("li")?;
        li.set_class_name("trigger-item");
        li.set_attribute("data-trigger-id", &trig.id.to_string())?;

        // Type badge
        let badge = document.create_element("span")?;
        badge.set_class_name("trigger-type-badge");
        badge.set_inner_html(match trig.r#type.as_str() {
            "webhook" => "Webhook",
            "email" => "Email",
            other => other,
        });
        li.append_child(&badge)?;

        // Secret code (click to copy as well)
        let secret_code = document.create_element("code")?;
        secret_code.set_class_name("trigger-secret");
        secret_code.set_inner_html(&trig.secret);
        li.append_child(&secret_code)?;

        // Show secret in a <code> element – user can manually select & copy.

        // Delete (trash) button
        let del_btn = document.create_element("button")?;
        del_btn.set_attribute("type", "button")?;
        del_btn.set_class_name("delete-trigger-btn");
        del_btn.set_inner_html("🗑");

        {
            let trig_id = trig.id;
            let cb_del = Closure::<dyn FnMut(web_sys::MouseEvent)>::wrap(Box::new(
                move |_e: web_sys::MouseEvent| {
                    dispatch_global_message(crate::messages::Message::RequestDeleteTrigger {
                        trigger_id: trig_id,
                    });
                },
            ));
            del_btn.add_event_listener_with_callback("click", cb_del.as_ref().unchecked_ref())?;
            cb_del.forget();
        }

        li.append_child(&del_btn)?;

        list_el.append_child(&li)?;
    }

    Ok(())
}
