use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement};

use crate::toast;
use crate::debug_log;
use crate::ui_components::set_button_loading;

use crate::models::{OpsSeriesPoint, OpsSummary, OpsTopAgent};
use crate::state::APP_STATE;

// ---------------------------------------------------------------
// HUD – compact header widget
// ---------------------------------------------------------------

/// Mount a compact Ops HUD into the header. Admin-gated by attempting a
/// summary fetch; on 403 the HUD stays hidden.
pub fn mount_ops_hud(document: &Document) -> Result<(), JsValue> {
    // Insert container once
    let header = document
        .get_element_by_id("header")
        .ok_or(JsValue::from_str("Header not found"))?;

    let hud = if let Some(el) = document.get_element_by_id("ops-hud") {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_id("ops-hud");
        el.set_class_name("ops-hud");
        el.set_attribute("role", "button")?;
        el.set_attribute("title", "Open Ops Dashboard")?;
        // Click navigates to Ops page
        {
            let doc = document.clone();
            let onclick = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
                crate::state::dispatch_global_message(crate::messages::Message::ToggleView(
                    crate::storage::ActiveView::AdminOps,
                ));
            }));
            el.add_event_listener_with_callback("click", onclick.as_ref().unchecked_ref())?;
            onclick.forget();
        }
        header.append_child(&el)?;
        el
    };

    // Initial content skeleton
    hud.set_inner_html("<span class=\"ops-hud-skel\">Ops: …</span>");

    // Kick off initial fetch and polling every ~25s
    wasm_bindgen_futures::spawn_local(async move {
        // Attempt fetch; hide HUD if forbidden
        match crate::network::api_client::ApiClient::get_ops_summary().await {
            Ok(json) => {
                if let Ok(summary) = serde_json::from_str::<OpsSummary>(&json) {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::OpsSummaryLoaded(summary),
                    );
                    // Render immediately
                    if let Some(win) = web_sys::window() {
                        if let Some(doc) = win.document() {
                            let _ = render_ops_hud(&doc);
                        }
                    }
                }
            }
            Err(e) => {
                let err = format!("{:?}", e);
                // Permission denied → non-admin: hide HUD silently
                if err.contains("Permission denied") {
                    if let Some(win) = web_sys::window() {
                        if let Some(doc) = win.document() {
                            if let Some(el) = doc.get_element_by_id("ops-hud") {
                                crate::dom_utils::hide(&el);
                            }
                        }
                    }
                    return; // Don't start poller
                } else {
                    web_sys::console::warn_1(&format!("Ops HUD fetch error: {}", err).into());
                }
            }
        }

        // Poller every ~25s – light touch, summary only
        let window = web_sys::window().unwrap();
        let closure = Closure::<dyn FnMut()>::wrap(Box::new(move || {
            let fut = async {
                if let Ok(json) = crate::network::api_client::ApiClient::get_ops_summary().await {
                    if let Ok(summary) = serde_json::from_str::<OpsSummary>(&json) {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::OpsSummaryLoaded(summary),
                        );
                        if let Some(win) = web_sys::window() {
                            if let Some(doc) = win.document() {
                                let _ = render_ops_hud(&doc);
                            }
                        }
                    }
                }
            };
            wasm_bindgen_futures::spawn_local(fut);
        }));
        let _ = window.set_interval_with_callback_and_timeout_and_arguments(
            closure.as_ref().unchecked_ref(),
            25_000,
            &js_sys::Array::new(),
        );
        closure.forget();
    });

    Ok(())
}

fn pct_color(percent_opt: Option<f64>) -> &'static str {
    let p = percent_opt.unwrap_or(0.0);
    if p >= 100.0 {
        "red"
    } else if p >= 80.0 {
        "amber"
    } else {
        "green"
    }
}

/// Render HUD from state summary
pub fn render_ops_hud(document: &Document) -> Result<(), JsValue> {
    let hud = match document.get_element_by_id("ops-hud") {
        Some(el) => el,
        None => return Ok(()),
    };

    let summary_opt = APP_STATE.with(|s| s.borrow().ops_summary.clone());
    if let Some(sum) = summary_opt {
        let runs = sum.runs_today;
        let cost = sum
            .cost_today_usd
            .map(|v| format!("${:.2}", v))
            .unwrap_or("—".to_string());
        let errs = sum.errors_last_hour;
        let pct = sum
            .budget_global
            .as_ref()
            .and_then(|b| b.percent)
            .unwrap_or(0.0);
        let color = pct_color(sum.budget_global.as_ref().and_then(|b| b.percent));
        let html = format!(
            "<div class=\"ops-hud-inner {color}\"><span>Runs {runs}</span><span>Cost {cost}</span><span>Err {errs}</span><span>Budget {pct:.0}%</span></div>",
            color=color, runs=runs, cost=cost, errs=errs, pct=pct
        );
        hud.set_inner_html(&html);
        crate::dom_utils::show(&hud);
    }

    Ok(())
}

// ---------------------------------------------------------------
// Ops Dashboard – page renderer
// ---------------------------------------------------------------

pub fn render_ops_dashboard(document: &Document) -> Result<(), JsValue> {
    let root = match document.get_element_by_id("ops-dashboard") {
        Some(el) => el,
        None => return Ok(()),
    };

    // Apply TV mode if ?tv=1 is present
    apply_tv_mode(document);

    // If we don't have admin access (determined by hiding HUD earlier), we still try REST once
    let doc_clone = document.clone();
    wasm_bindgen_futures::spawn_local(async move {
        match crate::network::api_client::ApiClient::get_ops_summary().await {
            Ok(json) => match serde_json::from_str::<OpsSummary>(&json) {
                Ok(summary) => {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::OpsSummaryLoaded(summary.clone()),
                    );
                    // Default to 30d view on initial load
                    let _ = render_ops_dashboard_for_window(&doc_clone, &summary, "30d").await;

                    // Subscribe to WS ops:events (admin-only; let server enforce)
                    subscribe_ops_events();
                }
                Err(e) => {
                    web_sys::console::error_1(&format!("Bad /ops/summary JSON: {:?}", e).into());
                    show_no_access(&doc_clone, Some("Malformed server response"));
                }
            },
            Err(e) => {
                let err = format!("{:?}", e);
                if err.contains("Permission denied") {
                    show_no_access(&doc_clone, None);
                } else {
                    web_sys::console::error_1(&format!("Ops summary error: {}", err).into());
                    show_no_access(&doc_clone, Some("Failed to load data"));
                }
            }
        }
    });

    Ok(())
}

async fn render_ops_dashboard_for_window(
    document: &Document,
    summary: &OpsSummary,
    window: &str,
) -> Result<(), JsValue> {
    // Decide metrics by window
    let (runs_metric, errs_metric, cost_metric) = match window {
        "today" => ("runs_by_hour", "errors_by_hour", "cost_by_hour"),
        "7d" => ("runs_by_day", "errors_by_day", "cost_by_day"),
        _ => ("runs_by_day", "errors_by_day", "cost_by_day"),
    };

    let runs_js = crate::network::api_client::ApiClient::get_ops_timeseries(runs_metric, window).await;
    let errs_js = crate::network::api_client::ApiClient::get_ops_timeseries(errs_metric, window).await;
    let cost_js = crate::network::api_client::ApiClient::get_ops_timeseries(cost_metric, window).await;
    let top_js = crate::network::api_client::ApiClient::get_ops_top("agents", window, 5).await;

    let mut runs_series: Vec<OpsSeriesPoint> = Vec::new();
    let mut errs_series: Vec<OpsSeriesPoint> = Vec::new();
    let mut cost_series: Vec<OpsSeriesPoint> = Vec::new();
    let mut top_agents: Vec<OpsTopAgent> = summary.top_agents_today.clone();
    if let Ok(j) = runs_js { let _ = serde_json::from_str::<Vec<OpsSeriesPoint>>(&j).map(|v| runs_series = v); }
    if let Ok(j) = errs_js { let _ = serde_json::from_str::<Vec<OpsSeriesPoint>>(&j).map(|v| errs_series = v); }
    if let Ok(j) = cost_js { let _ = serde_json::from_str::<Vec<OpsSeriesPoint>>(&j).map(|v| cost_series = v); }
    if let Ok(j) = top_js { let _ = serde_json::from_str::<Vec<OpsTopAgent>>(&j).map(|v| top_agents = v); }

    build_ops_dashboard_ui(document, summary, &runs_series, &errs_series, &cost_series, &top_agents, window)
}

fn show_no_access(document: &Document, detail: Option<&str>) {
    if let Some(root) = document.get_element_by_id("ops-dashboard") {
        let mut msg = String::from("Ops Dashboard is admin-only.");
        if let Some(d) = detail { msg.push_str(" "); msg.push_str(d); }
        root.set_inner_html(&format!("<div class=\"empty\">{}</div>", msg));
    }
}

fn build_ops_dashboard_ui(
    document: &Document,
    summary: &OpsSummary,
    runs_series: &[OpsSeriesPoint],
    errs_series: &[OpsSeriesPoint],
    cost_series: &[OpsSeriesPoint],
    top_agents: &[OpsTopAgent],
    window: &str,
) -> Result<(), JsValue> {
    let root = document.get_element_by_id("ops-dashboard").unwrap();
    root.set_inner_html("");

    // Header – compact title + status chip
    let head = document.create_element("div")?;
    head.set_class_name("ops-head");
    let g_pct = summary
        .budget_global
        .as_ref()
        .and_then(|b| b.percent)
        .unwrap_or(0.0);
    let status_class = if g_pct >= 100.0 || summary.errors_last_hour > 50 {
        "down"
    } else if g_pct >= 80.0 || summary.errors_last_hour > 0 {
        "degraded"
    } else {
        "healthy"
    };
    let status_label = match status_class {
        "down" => "Down",
        "degraded" => "Degraded",
        _ => "Healthy",
    };
    let (window_label, a_today, a_7d, a_30d) = match window {
        "today" => ("Today", "active", "", ""),
        "7d" => ("Last 7 days", "", "active", ""),
        _ => ("Last 30 days", "", "", "active"),
    };
    let head_html = format!(
        "<div class=\"title\">Operational Overview</div>\
         <div class=\"ops-range\">\
           <button id=\"ops-range-today\" class=\"{}\">Today</button>\
           <button id=\"ops-range-7d\" class=\"{}\">7d</button>\
           <button id=\"ops-range-30d\" class=\"{}\">30d</button>\
         </div>\
         <div class=\"status-chip {cls}\" title=\"{window_label}\">{label}</div>",
        a_today, a_7d, a_30d,
        cls = status_class,
        window_label = window_label,
        label = status_label
    );
    head.set_inner_html(&head_html);
    root.append_child(&head)?;

    // Attach range toggle handlers
    let attach = |id: &str, w: &'static str, sum: OpsSummary| {
        if let Some(btn) = document.get_element_by_id(id) {
            let doc2 = document.clone();
            let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
                let doc3 = doc2.clone();
                let sum_clone = sum.clone();
                wasm_bindgen_futures::spawn_local(async move {
                    let _ = render_ops_dashboard_for_window(&doc3, &sum_clone, w).await;
                });
            }));
            let _ = btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
            cb.forget();
        }
    };
    attach("ops-range-today", "today", summary.clone());
    attach("ops-range-7d", "7d", summary.clone());
    attach("ops-range-30d", "30d", summary.clone());

    // KPI row ---------------------------------------------------------
    let kpis = document.create_element("div")?;
    kpis.set_class_name("kpi-row");
    // Compute aggregates for current window
    let runs_sum: f64 = runs_series.iter().map(|p| p.value).sum();
    let errs_sum: f64 = errs_series.iter().map(|p| p.value).sum();
    let cost_sum: f64 = cost_series.iter().map(|p| p.value).sum();
    let success_rate = if runs_sum > 0.0 { ((runs_sum - errs_sum) / runs_sum) * 100.0 } else { f64::NAN };
    let kpi_html = format!(
        "<div class=card><div class=label>Runs ({})</div><div class=value>{}</div></div>
         <div class=card><div class=label>Cost ({})</div><div class=value>{}</div></div>
         <div class=card><div class=label>Success Rate ({})</div><div class=value>{}</div></div>
         <div class=card><div class=label>Errors ({})</div><div class=value>{}</div></div>",
        window_label,
        runs_sum as i32,
        window_label,
        format!("${:.2}", cost_sum),
        window_label,
        if success_rate.is_nan() { "—".to_string() } else { format!("{:.0}%", success_rate) },
        window_label,
        errs_sum as i32,
    );
    kpis.set_inner_html(&kpi_html);
    root.append_child(&kpis)?;

    // Budget gauges ---------------------------------------------------
    let gauges = document.create_element("div")?;
    gauges.set_class_name("gauges-row");
    let (user_pct, global_pct) = (
        summary
            .budget_user
            .as_ref()
            .and_then(|b| b.percent)
            .unwrap_or(0.0),
        summary
            .budget_global
            .as_ref()
            .and_then(|b| b.percent)
            .unwrap_or(0.0),
    );
    let user_color = super::ops::pct_color(summary.budget_user.as_ref().and_then(|b| b.percent));
    let global_color = super::ops::pct_color(summary.budget_global.as_ref().and_then(|b| b.percent));
    let gauges_html = format!(
        "<div class=\"gauge {u_c}\"><div>User Budget</div><div class=bar><span style=\"width:{u_pct}%\"></span></div><div class=pct>{u_pct:.0}%</div></div>
         <div class=\"gauge {g_c}\"><div>Global Budget</div><div class=bar><span style=\"width:{g_pct}%\"></span></div><div class=pct>{g_pct:.0}%</div></div>",
        u_c = user_color, g_c = global_color, u_pct = user_pct, g_pct = global_pct
    );
    gauges.set_inner_html(&gauges_html);
    root.append_child(&gauges)?;

    // Sparklines ------------------------------------------------------
    let charts = document.create_element("div")?;
    charts.set_class_name("charts-row");
    charts.append_child(&sparkline(document, &format!("Runs ({})", window_label), runs_series)?.into())?;
    charts.append_child(&sparkline(document, &format!("Errors ({})", window_label), errs_series)?.into())?;
    charts.append_child(&sparkline(document, &format!("Cost ({})", window_label), cost_series)?.into())?;
    root.append_child(&charts)?;

    // Admin controls ---------------------------------------------------
    if crate::state::APP_STATE.with(|s| s.borrow().is_super_admin) {
        let admin = build_admin_controls(document)?;
        root.append_child(&admin)?;
    }

    // Top agents table -----------------------------------------------
    let table = document.create_element("table")?;
    table.set_class_name("top-agents");
    table.set_inner_html(
        "<thead><tr><th>Agent</th><th>Owner</th><th>Runs</th><th>p95 ms</th><th>Cost</th></tr></thead><tbody id=\"ops-top-body\"></tbody>",
    );
    root.append_child(&table)?;
    if let Some(tbody) = table.dyn_ref::<HtmlElement>().unwrap().owner_document().unwrap().get_element_by_id("ops-top-body") {
        let mut rows = String::new();
        for a in top_agents.iter() {
            let cost = a.cost_usd.map(|v| format!("${:.2}", v)).unwrap_or("—".to_string());
            let p95 = a.p95_ms.map(|v| v.to_string()).unwrap_or("-".to_string());
            rows.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                html_escape(&a.name),
                html_escape(a.owner_email.as_deref().unwrap_or("-")),
                a.runs,
                p95,
                cost
            ));
        }
        tbody.set_inner_html(&rows);
    }

    // Live ticker -----------------------------------------------------
    let ticker = document.create_element("div")?;
    ticker.set_id("ops-ticker");
    ticker.set_class_name("ops-ticker");
    root.append_child(&ticker)?;
    render_ticker(document)?;

    Ok(())
}

/// Build the Super Admin controls panel, including the Reset Database action
fn build_admin_controls(document: &Document) -> Result<Element, JsValue> {
    let card = document.create_element("div")?;
    card.set_class_name("card admin-card");

    // Title
    let title = document.create_element("div")?;
    title.set_class_name("label");
    title.set_inner_html("Super Admin Tools");
    card.append_child(&title)?;

    // Simple button row - no complex styling
    let button_container = document.create_element("div")?;
    button_container.set_attribute("style", "
        display: flex;
        gap: 12px;
        margin: 16px 0;
        align-items: flex-start;
    ")?;

    // Clear Data button (primary, normal size)
    let clear_btn = document.create_element("button")?;
    clear_btn.set_id("ops-clear-data-btn");
    clear_btn.set_attribute("style", "
        background: #2563eb;
        color: white;
        border: none;
        padding: 8px 16px;
        border-radius: 6px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        min-width: 120px;
    ")?;
    clear_btn.set_inner_html("🧹 Clear Data");
    button_container.append_child(&clear_btn)?;

    // Description for clear data
    let clear_desc = document.create_element("div")?;
    clear_desc.set_attribute("style", "
        color: #9ca3af;
        font-size: 13px;
        line-height: 1.4;
        flex: 1;
        padding-top: 2px;
    ")?;
    clear_desc.set_inner_html("Removes agents & workflows. Keeps you logged in.");
    button_container.append_child(&clear_desc)?;

    card.append_child(&button_container)?;

    // Full Reset section (separate row)
    let reset_container = document.create_element("div")?;
    reset_container.set_attribute("style", "
        display: flex;
        gap: 12px;
        margin: 12px 0;
        align-items: flex-start;
        padding-top: 12px;
        border-top: 1px solid #374151;
    ")?;

    // Full Reset button (secondary, normal size)
    let reset_btn = document.create_element("button")?;
    reset_btn.set_id("ops-full-reset-btn");
    reset_btn.set_attribute("style", "
        background: transparent;
        color: #dc2626;
        border: 1px solid #dc2626;
        padding: 8px 16px;
        border-radius: 6px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        min-width: 120px;
    ")?;
    reset_btn.set_inner_html("⚠️ Full Reset");
    reset_container.append_child(&reset_btn)?;

    // Description for full reset
    let reset_desc = document.create_element("div")?;
    reset_desc.set_attribute("style", "
        color: #9ca3af;
        font-size: 13px;
        line-height: 1.4;
        flex: 1;
        padding-top: 2px;
    ")?;
    reset_desc.set_inner_html("Destroys everything, logs you out. Only use for major schema changes.");
    reset_container.append_child(&reset_desc)?;

    card.append_child(&reset_container)?;

    // Simple results area
    let summary = document.create_element("pre")?;
    summary.set_id("ops-reset-summary");
    summary.set_attribute("style", "
        margin-top: 16px;
        padding: 12px;
        background: #1f2937;
        border: 1px solid #374151;
        border-radius: 6px;
        font-size: 12px;
        color: #9ca3af;
        white-space: pre-wrap;
        overflow-x: auto;
    ")?;
    summary.set_inner_html("Operation results will appear here.");
    card.append_child(&summary)?;

    // Click handlers for both buttons
    let requires_password = crate::state::APP_STATE.with(|s| s.borrow().admin_requires_password);

    // Clear Data button handler (gentle)
    let clear_cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
        debug_log!("Ops: Clear Data clicked");
        if let Some(win) = web_sys::window() {
            if !win
                .confirm_with_message("Clear user data? This will remove agents, workflows, and chat history but keep your account.")
                .unwrap_or(false)
            {
                return;
            }

            let pwd_opt = if requires_password {
                match win.prompt_with_message("Enter database reset password:") {
                    Ok(Some(p)) if !p.is_empty() => Some(p),
                    _ => {
                        toast::error("Database reset password is required in production");
                        return;
                    }
                }
            } else { None };

            use crate::network::ApiClient;
            use wasm_bindgen_futures::spawn_local;
            spawn_local(async move {
                if let Some(el) = web_sys::window()
                    .and_then(|w| w.document())
                    .and_then(|d| d.get_element_by_id("ops-clear-data-btn"))
                { set_button_loading(&el, true); }

                let res = if let Some(pwd) = pwd_opt { ApiClient::clear_user_data_with_password(&pwd).await } else { ApiClient::clear_user_data().await };

                match res {
                    Ok(resp) => {
                        web_sys::console::log_1(&format!("Ops reset raw response: {}", resp).into());
                        let mut toast_done = false;
                        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&resp) {
                            let before = json.get("total_rows_before").and_then(|v| v.as_i64()).unwrap_or(-1);
                            let after = json.get("total_rows_after").and_then(|v| v.as_i64()).unwrap_or(-1);
                            let ms = json.get("drop_create_ms").and_then(|v| v.as_i64()).unwrap_or(-1);
                            let attempts = json.get("attempts_used").and_then(|v| v.as_i64()).unwrap_or(1);
                            let terminated = json.get("terminated_connections").and_then(|v| v.as_i64()).unwrap_or(0);
                            toast::success(&format!("Database reset: rows {} -> {}, attempts {}, {} ms (terminated {} conns)", before, after, attempts, ms, terminated));
                            toast_done = true;
                            // Pretty summary in pre element
                            if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                                if let Some(pre) = doc.get_element_by_id("ops-reset-summary") {
                                    let pretty = serde_json::to_string_pretty(&json).unwrap_or(resp);
                                    pre.set_inner_html(&pretty);
                                }
                            }
                        }
                        if !toast_done { toast::success("Database reset successfully"); }

                        // For clear data, just refresh the UI without page reload
                        crate::state::dispatch_global_message(crate::messages::Message::ResetDatabase);
                    }
                    Err(e) => {
                        let em = format!("{:?}", e);
                        if em.contains("Incorrect confirmation password") { toast::error("Incorrect database reset password"); }
                        else if em.contains("Super admin privileges required") { toast::error("Access denied: Super admin privileges required"); }
                        else { toast::error(&format!("Failed to reset database: {:?}", e)); }
                        web_sys::console::error_1(&format!("Ops reset error: {:?}", e).into());
                    }
                }

                if let Some(el) = web_sys::window()
                    .and_then(|w| w.document())
                    .and_then(|d| d.get_element_by_id("ops-clear-data-btn"))
                { set_button_loading(&el, false); }
            });
        }
    }));

    // Full Reset button handler (nuclear)
    let reset_cb = {
        let requires_password_flag = requires_password; // Clone for second closure
        Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::MouseEvent| {
            debug_log!("Ops: Full Reset clicked");
            if let Some(win) = web_sys::window() {
                if !win
                    .confirm_with_message("WARNING: This will DELETE EVERYTHING including your account. You'll be logged out. Cannot be undone. Proceed?")
                    .unwrap_or(false)
                {
                    return;
                }

                let pwd_opt = if requires_password_flag {
                    match win.prompt_with_message("Enter database reset password:") {
                        Ok(Some(p)) if !p.is_empty() => Some(p),
                        _ => {
                            toast::error("Database reset password is required in production");
                            return;
                        }
                    }
                } else { None };

                use crate::network::ApiClient;
                use wasm_bindgen_futures::spawn_local;
                spawn_local(async move {
                    if let Some(el) = web_sys::window()
                        .and_then(|w| w.document())
                        .and_then(|d| d.get_element_by_id("ops-full-reset-btn"))
                    { set_button_loading(&el, true); }

                    let res = if let Some(pwd) = pwd_opt { ApiClient::reset_database_full_with_password(&pwd).await } else { ApiClient::reset_database_full().await };

                    match res {
                        Ok(resp) => {
                            web_sys::console::log_1(&format!("Ops full reset raw response: {}", resp).into());
                            let mut toast_done = false;
                            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&resp) {
                                let before = json.get("total_rows_before").and_then(|v| v.as_i64()).unwrap_or(-1);
                                let after = json.get("total_rows_after").and_then(|v| v.as_i64()).unwrap_or(-1);
                                let ms = json.get("drop_create_ms").and_then(|v| v.as_i64()).unwrap_or(-1);
                                let attempts = json.get("attempts_used").and_then(|v| v.as_i64()).unwrap_or(1);
                                let terminated = json.get("terminated_connections").and_then(|v| v.as_i64()).unwrap_or(0);
                                toast::success(&format!("Full reset: rows {} -> {}, attempts {}, {} ms (terminated {} conns)", before, after, attempts, ms, terminated));
                                toast_done = true;
                                // Pretty summary in pre element
                                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                                    if let Some(pre) = doc.get_element_by_id("ops-reset-summary") {
                                        let pretty = serde_json::to_string_pretty(&json).unwrap_or(resp);
                                        pre.set_inner_html(&pretty);
                                    }
                                }
                            }
                            if !toast_done { toast::success("Full reset successfully"); }

                            // For full reset, reload page since user will be logged out
                            crate::state::dispatch_global_message(crate::messages::Message::ResetDatabase);
                            if let Some(win) = web_sys::window() {
                                let cb = Closure::<dyn FnMut()>::wrap(Box::new(move || {
                                    if let Some(w) = web_sys::window() { let _ = w.location().reload(); }
                                }));
                                let _ = win.set_timeout_with_callback_and_timeout_and_arguments(cb.as_ref().unchecked_ref(), 150, &js_sys::Array::new());
                                cb.forget();
                            }
                        }
                        Err(e) => {
                            let em = format!("{:?}", e);
                            if em.contains("Incorrect confirmation password") { toast::error("Incorrect database reset password"); }
                            else if em.contains("Super admin privileges required") { toast::error("Access denied: Super admin privileges required"); }
                            else { toast::error(&format!("Failed to reset database: {:?}", e)); }
                            web_sys::console::error_1(&format!("Ops reset error: {:?}", e).into());
                        }
                    }

                    if let Some(el) = web_sys::window()
                        .and_then(|w| w.document())
                        .and_then(|d| d.get_element_by_id("ops-full-reset-btn"))
                    { set_button_loading(&el, false); }
                });
            }
        }))
    };

    // Attach event listeners to buttons
    if let Some(btn_el) = clear_btn.dyn_ref::<HtmlElement>() {
        let _ = btn_el.add_event_listener_with_callback("click", clear_cb.as_ref().unchecked_ref());
    }
    if let Some(btn_el) = reset_btn.dyn_ref::<HtmlElement>() {
        let _ = btn_el.add_event_listener_with_callback("click", reset_cb.as_ref().unchecked_ref());
    }

    clear_cb.forget();
    reset_cb.forget();

    Ok(card)
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn sparkline(document: &Document, label: &str, series: &[OpsSeriesPoint]) -> Result<Element, JsValue> {
    let wrap = document.create_element("div")?;
    wrap.set_class_name("spark");
    let max = series
        .iter()
        .map(|p| p.value)
        .fold(0.0_f64, |m, v| if v > m { v } else { m })
        .max(1.0);
    // Build simple inline SVG polyline
    let w = 240.0_f64;
    let h = 40.0_f64;
    let n = series.len().max(1) as f64;
    let mut points = String::new();
    for (i, p) in series.iter().enumerate() {
        let x = if n <= 1.0 { 0.0 } else { (i as f64) * (w / (n - 1.0)) };
        let y = if max <= 0.0 { h } else { h - (p.value / max * h) };
        points.push_str(&format!("{:.2},{:.2} ", x, y));
    }
    // Use theme token for stroke via style attribute to allow CSS variables
    let svg = format!(
        "<div class=\"label\">{}</div>\
         <svg width=\"{}\" height=\"{}\">\
           <polyline fill=\"none\" style=\"stroke: var(--secondary)\" stroke-width=\"2\" points=\"{}\"/>\
         </svg>",
        label, w as i32, h as i32, points
    );
    wrap.set_inner_html(&svg);
    Ok(wrap)
}

pub fn render_ticker(document: &Document) -> Result<(), JsValue> {
    let Some(el) = document.get_element_by_id("ops-ticker") else { return Ok(()); };
    // Read events from state
    let events = APP_STATE.with(|s| s.borrow().ops_ticker.clone());
    let mut html = String::new();
    html.push_str("<div class=\"ticker-list\">");
    for ev in events.iter() {
        let class = match ev.kind.as_str() {
            "run_failed" => "red",
            "budget_denied" => "red",
            "run_success" => "green",
            "run_started" => "amber",
            _ => "gray",
        };
        let text = &ev.text;
        html.push_str(&format!("<div class=\"tick {}\">{}</div>", class, html_escape(text)));
    }
    html.push_str("</div>");
    el.set_inner_html(&html);
    Ok(())
}

fn subscribe_ops_events() {
    use std::cell::RefCell;
    use std::rc::Rc;
    use crate::network::topic_manager::ITopicManager;
    let (tm_rc, already) = APP_STATE.with(|s| {
        let st = s.borrow();
        (st.topic_manager.clone() as Rc<RefCell<dyn ITopicManager>>, st.ops_ws_subscribed)
    });
    if already { return; }

    let handler: crate::network::topic_manager::TopicHandler = Rc::new(RefCell::new(move |payload: serde_json::Value| {
        // Parse envelope and only handle ops_event
        if let Ok(envelope) = serde_json::from_value::<crate::generated::ws_messages::Envelope>(payload.clone()) {
            if envelope.message_type == "ops_event" {
                let kind = envelope
                    .data
                    .get("type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("event")
                    .to_string();
                let text = humanize_ops_event(&envelope);
                crate::state::dispatch_global_message(crate::messages::Message::OpsAppendEvent {
                    ts: envelope.ts,
                    kind,
                    text,
                });
            }
        }
    }));

    // Subscribe
    {
        let mut tm = tm_rc.borrow_mut();
        let _ = tm.subscribe("ops:events".to_string(), handler);
    }
    // Mark as subscribed
    APP_STATE.with(|s| s.borrow_mut().ops_ws_subscribed = true);
}

fn humanize_ops_event(env: &crate::generated::ws_messages::Envelope) -> String {
    // Best-effort formatting using typed data
    if let Ok(d) = serde_json::from_value::<crate::generated::ws_messages::OpsEventData>(env.data.clone()) {
        match d.r#type.as_str() {
            "run_started" => format!("Run started – agent {} run {}", d.agent_id.unwrap_or(0), d.run_id.unwrap_or(0)),
            "run_success" => format!("Run success – agent {} in {} ms", d.agent_id.unwrap_or(0), d.duration_ms.unwrap_or(0)),
            "run_failed" => format!("Run failed – agent {}: {}", d.agent_id.unwrap_or(0), d.error.unwrap_or_else(|| "error".into())),
            "agent_created" => format!("Agent created – {}", d.agent_name.unwrap_or_else(|| "(unnamed)".into())),
            "agent_updated" => format!("Agent updated – {}", d.agent_name.unwrap_or_else(|| "(unknown)".into())),
            "thread_message_created" => format!("New message – thread {}", d.thread_id.unwrap_or(0)),
            "budget_denied" => format!("Budget denied – {} {}%", d.scope.unwrap_or_else(|| "user".into()), d.percent.unwrap_or(100.0)),
            other => other.to_string(),
        }
    } else {
        format!("{}", env.message_type)
    }
}

fn apply_tv_mode(document: &Document) {
    if let Some(win) = web_sys::window() {
        if let Ok(search) = win.location().search() {
            if search.contains("tv=1") {
                // Hide non-essential chrome
                if let Some(el) = document.get_element_by_id("global-tabs-container") {
                    crate::dom_utils::hide(&el);
                }
                if let Some(el) = document.get_element_by_id("agent-shelf") {
                    crate::dom_utils::hide(&el);
                }
                if let Some(el) = document.get_element_by_id("header") {
                    // Keep header minimal (hide HUD)
                    if let Some(hud) = document.get_element_by_id("ops-hud") {
                        crate::dom_utils::hide(&hud);
                    }
                }

                // Fallback auto-refresh every 15s regardless of WS state
                let closure = Closure::<dyn FnMut()>::wrap(Box::new(move || {
                    let fut = async {
                        if let Ok(json) = crate::network::api_client::ApiClient::get_ops_summary().await {
                            if let Ok(summary) = serde_json::from_str::<OpsSummary>(&json) {
                                crate::state::dispatch_global_message(
                                    crate::messages::Message::OpsSummaryLoaded(summary.clone()),
                                );
                                if let Some(win) = web_sys::window() {
                                    if let Some(doc) = win.document() {
                                        let _ = crate::components::ops::render_ops_dashboard(&doc);
                                    }
                                }
                            }
                        }
                    };
                    wasm_bindgen_futures::spawn_local(fut);
                }));
                if let Some(win) = web_sys::window() {
                    let _ = win.set_interval_with_callback_and_timeout_and_arguments(
                        closure.as_ref().unchecked_ref(),
                        15_000,
                        &js_sys::Array::new(),
                    );
                }
                closure.forget();
            }
        }
    }
}
