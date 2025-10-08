use web_sys::Document;
use crate::state::AppState;
use crate::models::NodeExecStatus;

pub fn create_results_panel(document: &Document) -> Result<(), wasm_bindgen::JsValue> {
    // Create the results panel using native disclosure pattern
    let panel = document.create_element("details")?;
    panel.set_id("execution-results-panel");
    panel.set_class_name("execution-results-panel disclosure");

    // Create the summary header (replaces custom toggle button)
    let summary = document.create_element("summary")?;
    summary.set_class_name("results-panel-header disclosure__summary");

    let header_content = document.create_element("div")?;
    header_content.set_class_name("results-panel-header-content");

    let title = document.create_element("span")?;
    title.set_class_name("results-panel-title");
    title.set_inner_html("📋 Results");

    let status_text = document.create_element("span")?;
    status_text.set_class_name("results-panel-status");
    status_text.set_inner_html("Ready");

    header_content.append_child(&title)?;
    header_content.append_child(&status_text)?;
    summary.append_child(&header_content)?;

    // Create the collapsible content area
    let content_wrapper = document.create_element("div")?;
    content_wrapper.set_class_name("disclosure__content");

    let content = document.create_element("div")?;
    content.set_class_name("results-panel-content");
    content.set_id("results-panel-content");

    let results_list = document.create_element("div")?;
    results_list.set_class_name("results-list");
    results_list.set_id("results-list");
    results_list.set_inner_html("<div class='no-results'>No execution results yet</div>");

    content.append_child(&results_list)?;
    content_wrapper.append_child(&content)?;

    // Assemble the panel
    panel.append_child(&summary)?;
    panel.append_child(&content_wrapper)?;

    // Add to canvas container
    if let Some(canvas_container) = document.get_element_by_id("canvas-container") {
        canvas_container.append_child(&panel)?;
    }

    Ok(())
}


pub fn update_results_panel(document: &Document, state: &AppState) -> Result<(), wasm_bindgen::JsValue> {
    let results_list = match document.get_element_by_id("results-list") {
        Some(list) => list,
        None => return Ok(()), // Panel not created yet
    };

    let status_element = document.get_element_by_id("results-panel-status")
        .or_else(|| document.query_selector(".results-panel-status").ok().flatten());

    // Update overall status
    if let Some(execution) = &state.current_execution {
        let (status_text, status_class) = match execution.status {
            crate::state::ExecPhase::Starting => ("Starting...", "starting"),
            crate::state::ExecPhase::Running => ("Running...", "running"),
            crate::state::ExecPhase::Success => ("Complete", "success"),
            crate::state::ExecPhase::Failed => ("Failed", "failed"),
        };

        if let Some(status_el) = status_element {
            status_el.set_inner_html(status_text);
            status_el.set_class_name(&format!("results-panel-status {}", status_class));
        }
    }

    // Clear existing results
    results_list.set_inner_html("");

    // Get node execution results
    let mut has_results = false;
    let mut executing_nodes = Vec::new();
    let mut completed_nodes = Vec::new();

    for (node_id, node) in &state.nodes {
        if let Some(exec_status) = &node.exec_status {
            has_results = true;
            match exec_status {
                NodeExecStatus::Running => {
                    executing_nodes.push((node_id.clone(), node));
                }
                NodeExecStatus::Completed | NodeExecStatus::Failed => {
                    completed_nodes.push((node_id.clone(), node, exec_status));
                }
                _ => {}
            }
        }
    }

    if !has_results {
        let no_results = document.create_element("div")?;
        no_results.set_class_name("no-results");
        no_results.set_inner_html("No execution results yet");
        results_list.append_child(&no_results)?;
        return Ok(());
    }

    // Add currently executing nodes
    for (node_id, node) in executing_nodes {
        let result_item = create_result_item(document, &node_id, node, &NodeExecStatus::Running, None, &state.agents)?;
        results_list.append_child(&result_item)?;
    }

    // Add completed nodes
    for (node_id, node, status) in completed_nodes {
        // Try to find output from execution logs
        let output = state.execution_logs.iter()
            .filter(|log| log.node_id == node_id && log.stream == "stdout")
            .map(|log| log.text.clone())
            .collect::<Vec<_>>()
            .join("\n");

        let output = if output.is_empty() { None } else { Some(output) };
        let result_item = create_result_item(document, &node_id, node, status, output.as_ref(), &state.agents)?;
        results_list.append_child(&result_item)?;
    }

    Ok(())
}

fn create_result_item(
    document: &Document,
    _node_id: &str,
    node: &crate::models::CanvasNode,
    status: &NodeExecStatus,
    output: Option<&String>,
    agents: &std::collections::HashMap<u32, crate::models::ApiAgent>
) -> Result<web_sys::Element, wasm_bindgen::JsValue> {
    let item = document.create_element("div")?;
    item.set_class_name("result-item");

    let header = document.create_element("div")?;
    header.set_class_name("result-item-header");

    // Status icon
    let (icon, status_class) = match status {
        NodeExecStatus::Running => ("⏳", "running"),
        NodeExecStatus::Completed => ("✅", "success"),
        NodeExecStatus::Failed => ("❌", "failed"),
        NodeExecStatus::Idle => ("⚪", "idle"),
    };

    let node_name = match &node.node_type {
        crate::models::NodeType::AgentIdentity => {
            node.agent_id.and_then(|id| {
                agents.get(&id).map(|agent| agent.name.clone())
            }).unwrap_or_else(|| "Agent".to_string())
        }
        crate::models::NodeType::Tool { tool_name, .. } => tool_name.clone(),
        _ => node.text.clone(),
    };

    header.set_inner_html(&format!(
        "<span class='result-icon {}'>{}</span><span class='result-node-name'>{}</span>",
        status_class, icon, node_name
    ));

    item.append_child(&header)?;

    // Add output if available
    if let Some(output_text) = output {
        let output_container = document.create_element("div")?;
        output_container.set_class_name("result-output");

        let output_pre = document.create_element("pre")?;
        output_pre.set_inner_html(&html_escape(output_text));
        output_container.append_child(&output_pre)?;

        item.append_child(&output_container)?;
    } else if *status == NodeExecStatus::Running {
        let running_text = document.create_element("div")?;
        running_text.set_class_name("result-output running");
        running_text.set_inner_html("Executing...");
        item.append_child(&running_text)?;
    }

    Ok(item)
}

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

pub fn refresh_results_panel() -> Result<(), wasm_bindgen::JsValue> {
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            crate::state::APP_STATE.with(|state| {
                let state = state.borrow();
                let _ = update_results_panel(&document, &state);
            });
        }
    }
    Ok(())
}