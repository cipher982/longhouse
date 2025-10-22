//! MCP Server Management Component
//!
//! Provides UI for managing MCP servers attached to agents - both preset
//! integrations (GitHub, Linear, etc.) and custom server URLs.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlInputElement};

use crate::dom_utils;
use crate::network::api_client::ApiClient;
use crate::state::dispatch_global_message;

// MCP Server configuration types
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MCPServerConfig {
    pub name: String,
    pub url: Option<String>,
    pub preset: Option<String>,
    pub auth_token: String,
    pub status: Option<ConnectionStatus>,
    pub tools_count: Option<usize>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum ConnectionStatus {
    Untested,
    Testing,
    Success,
    Failed { error: String },
    Timeout,
}

#[derive(Clone, Debug)]
pub struct MCPPreset {
    pub id: String,
    pub name: String,
    pub description: String,
    pub icon: String,
    pub auth_type: String, // "oauth" | "token" | "none"
}

pub struct MCPServerManager {
    pub agent_id: u32,
    pub servers: Vec<MCPServerConfig>,
    pub presets: Vec<MCPPreset>,
    pub current_tab: MCPTab,
    pub connection_tests: HashMap<String, ConnectionStatus>,
    pub loading_states: HashMap<String, bool>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum MCPTab {
    QuickConnect,
    Custom,
}

impl MCPServerManager {
    pub fn new(agent_id: u32) -> Self {
        Self {
            agent_id,
            servers: Vec::new(),
            presets: Self::get_default_presets(),
            current_tab: MCPTab::QuickConnect,
            connection_tests: HashMap::new(),
            loading_states: HashMap::new(),
        }
    }

    fn get_default_presets() -> Vec<MCPPreset> {
        vec![
            MCPPreset {
                id: "github".to_string(),
                name: "GitHub".to_string(),
                description: "Issues, PRs, repositories".to_string(),
                icon: "🔗".to_string(),
                auth_type: "token".to_string(),
            },
            MCPPreset {
                id: "linear".to_string(),
                name: "Linear".to_string(),
                description: "Issues, projects, teams".to_string(),
                icon: "📋".to_string(),
                auth_type: "token".to_string(),
            },
            MCPPreset {
                id: "slack".to_string(),
                name: "Slack".to_string(),
                description: "Messages, channels".to_string(),
                icon: "💬".to_string(),
                auth_type: "token".to_string(),
            },
            MCPPreset {
                id: "notion".to_string(),
                name: "Notion".to_string(),
                description: "Pages, databases".to_string(),
                icon: "📝".to_string(),
                auth_type: "token".to_string(),
            },
        ]
    }

    /// Build the MCP server management UI and attach it to the specified container
    pub fn build_ui(&self, document: &Document, container: &Element) -> Result<(), JsValue> {
        // Clear existing content
        container.set_inner_html("");

        // Header
        let header = document.create_element("div")?;
        header.set_class_name("mcp-header");
        header.set_inner_html("🛠️ Tool Configuration");

        // Built-in tools section
        let builtin_section = self.build_builtin_tools_section(document)?;

        // Tab container
        let tab_container = document.create_element("div")?;
        tab_container.set_class_name("mcp-tab-container");

        // Tab buttons
        let tabs_wrapper = document.create_element("div")?;
        tabs_wrapper.set_class_name("mcp-tabs");

        let quick_tab = self.create_tab_button(document, "Quick Connect", &MCPTab::QuickConnect)?;
        let custom_tab = self.create_tab_button(document, "Custom Server", &MCPTab::Custom)?;

        tabs_wrapper.append_child(&quick_tab)?;
        tabs_wrapper.append_child(&custom_tab)?;

        // Tab content
        let quick_content = self.build_quick_connect_content(document)?;
        let custom_content = self.build_custom_server_content(document)?;

        // Show appropriate content based on current tab
        match self.current_tab {
            MCPTab::QuickConnect => {
                dom_utils::show(&quick_content);
                dom_utils::hide(&custom_content);
                quick_tab.set_class_name("mcp-tab active");
                custom_tab.set_class_name("mcp-tab");
            }
            MCPTab::Custom => {
                dom_utils::hide(&quick_content);
                dom_utils::show(&custom_content);
                quick_tab.set_class_name("mcp-tab");
                custom_tab.set_class_name("mcp-tab active");
            }
        }

        // Connected servers section
        let servers_section = self.build_connected_servers_section(document)?;

        // Assemble everything
        container.append_child(&header)?;
        container.append_child(&builtin_section)?;
        tab_container.append_child(&tabs_wrapper)?;
        tab_container.append_child(&quick_content)?;
        tab_container.append_child(&custom_content)?;
        container.append_child(&tab_container)?;
        container.append_child(&servers_section)?;

        Ok(())
    }

    fn build_builtin_tools_section(&self, document: &Document) -> Result<Element, JsValue> {
        let section = document.create_element("div")?;
        section.set_class_name("builtin-tools-section");

        let title = document.create_element("h4")?;
        title.set_inner_html("Built-in Tools ✓");
        title.set_class_name("builtin-tools-title");

        let tools_list = document.create_element("ul")?;
        tools_list.set_class_name("builtin-tools-list");

        let builtin_tools = [
            "Date/Time (get_current_time)",
            "Math (math_eval)",
            "HTTP Requests (http_request)",
            "Container Exec (container_exec)",
        ];

        for tool in &builtin_tools {
            let li = document.create_element("li")?;
            li.set_inner_html(tool);
            tools_list.append_child(&li)?;
        }

        section.append_child(&title)?;
        section.append_child(&tools_list)?;

        Ok(section)
    }

    fn create_tab_button(
        &self,
        document: &Document,
        label: &str,
        tab_type: &MCPTab,
    ) -> Result<Element, JsValue> {
        let button = document.create_element("button")?;
        button.set_attribute("type", "button")?;
        button.set_class_name("mcp-tab");
        button.set_inner_html(label);
        // Visual styling moved to CSS (.mcp-tab)

        // Add click handler
        let tab_clone = tab_type.clone();
        let agent_id = self.agent_id;
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            dispatch_global_message(crate::messages::Message::SetMCPTab {
                agent_id,
                tab: tab_clone.clone(),
            });
        }));
        button.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();

        Ok(button)
    }

    fn build_quick_connect_content(&self, document: &Document) -> Result<Element, JsValue> {
        let content = document.create_element("div")?;
        content.set_id("mcp-quick-connect-content");
        content.set_class_name("mcp-tab-content");

        let intro = document.create_element("p")?;
        intro.set_inner_html("🚀 Connect to popular services with one click:");
        intro.set_class_name("mcp-intro-text");

        let grid = document.create_element("div")?;
        grid.set_class_name("mcp-presets-grid");
        grid.set_class_name("mcp-presets-grid");

        for preset in &self.presets {
            let card = self.create_preset_card(document, preset)?;
            grid.append_child(&card)?;
        }

        content.append_child(&intro)?;
        content.append_child(&grid)?;

        Ok(content)
    }

    fn create_preset_card(
        &self,
        document: &Document,
        preset: &MCPPreset,
    ) -> Result<Element, JsValue> {
        let card = document.create_element("div")?;
        card.set_class_name("mcp-preset-card");
        card.set_class_name("mcp-preset-card");

        let header = document.create_element("div")?;
        header.set_class_name("mcp-preset-card-header");

        let icon = document.create_element("span")?;
        icon.set_inner_html(&preset.icon);
        icon.set_class_name("mcp-preset-icon");

        let title = document.create_element("strong")?;
        title.set_inner_html(&preset.name);

        let description = document.create_element("div")?;
        description.set_inner_html(&preset.description);
        description.set_class_name("mcp-preset-description");

        let button = document.create_element("button")?;
        button.set_attribute("type", "button")?;
        button.set_class_name("btn-primary");
        button.set_inner_html(&format!("Connect {}", preset.name));
        button.set_class_name("mcp-preset-connect-btn");

        // Add click handler for preset connection
        let preset_id = preset.id.clone();
        let agent_id = self.agent_id;
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            dispatch_global_message(crate::messages::Message::ConnectMCPPreset {
                agent_id,
                preset_id: preset_id.clone(),
            });
        }));
        button.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();

        header.append_child(&icon)?;
        header.append_child(&title)?;
        card.append_child(&header)?;
        card.append_child(&description)?;
        card.append_child(&button)?;

        Ok(card)
    }

    fn build_custom_server_content(&self, document: &Document) -> Result<Element, JsValue> {
        let content = document.create_element("div")?;
        content.set_id("mcp-custom-server-content");
        content.set_class_name("mcp-tab-content");

        let intro = document.create_element("p")?;
        intro.set_inner_html("➕ Add your own MCP server:");
        intro.set_class_name("mcp-custom-intro");

        let form = document.create_element("div")?;
        form.set_class_name("mcp-custom-form");
        form.set_class_name("mcp-custom-form");

        // Server URL input
        let url_label = document.create_element("label")?;
        url_label.set_inner_html("Server URL:");
        url_label.set_attribute("for", "mcp-custom-url")?;

        let url_input = document.create_element("input")?;
        url_input.set_id("mcp-custom-url");
        url_input.set_attribute("type", "url")?;
        url_input.set_attribute("placeholder", "https://example.com/mcp/sse")?;
        url_input.set_class_name("input-field");

        // Server name input
        let name_label = document.create_element("label")?;
        name_label.set_inner_html("Name:");
        name_label.set_attribute("for", "mcp-custom-name")?;

        let name_input = document.create_element("input")?;
        name_input.set_id("mcp-custom-name");
        name_input.set_attribute("type", "text")?;
        name_input.set_attribute("placeholder", "my-server")?;
        name_input.set_class_name("input-field");

        // Auth token input
        let token_label = document.create_element("label")?;
        token_label.set_inner_html("Auth Token:");
        token_label.set_attribute("for", "mcp-custom-token")?;

        let token_input = document.create_element("input")?;
        token_input.set_id("mcp-custom-token");
        token_input.set_attribute("type", "password")?;
        token_input.set_attribute("placeholder", "bearer_xxx or api_key")?;
        token_input.set_class_name("input-field");

        // Buttons
        let buttons = document.create_element("div")?;
        buttons.set_class_name("mcp-custom-buttons");

        let test_btn = document.create_element("button")?;
        test_btn.set_attribute("type", "button")?;
        test_btn.set_class_name("btn");
        test_btn.set_inner_html("Test Connection");
        test_btn.set_id("mcp-test-connection");

        let add_btn = document.create_element("button")?;
        add_btn.set_attribute("type", "button")?;
        add_btn.set_class_name("btn-primary");
        add_btn.set_inner_html("Add Server");
        add_btn.set_id("mcp-add-server");

        // Add event listeners
        let agent_id = self.agent_id;

        let test_cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                let url = Self::get_input_value(&doc, "mcp-custom-url").unwrap_or_default();
                let name = Self::get_input_value(&doc, "mcp-custom-name").unwrap_or_default();
                let token = Self::get_input_value(&doc, "mcp-custom-token").unwrap_or_default();

                if !url.is_empty() && !name.is_empty() {
                    dispatch_global_message(crate::messages::Message::TestMCPConnection {
                        agent_id,
                        url,
                        name,
                        auth_token: token,
                    });
                }
            }
        }));
        test_btn.add_event_listener_with_callback("click", test_cb.as_ref().unchecked_ref())?;
        test_cb.forget();

        let add_cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                let url = Self::get_input_value(&doc, "mcp-custom-url").unwrap_or_default();
                let name = Self::get_input_value(&doc, "mcp-custom-name").unwrap_or_default();
                let token = Self::get_input_value(&doc, "mcp-custom-token").unwrap_or_default();

                if !url.is_empty() && !name.is_empty() {
                    dispatch_global_message(crate::messages::Message::AddMCPServer {
                        agent_id,
                        url: Some(url),
                        name,
                        preset: None,
                        auth_token: token,
                    });
                }
            }
        }));
        add_btn.add_event_listener_with_callback("click", add_cb.as_ref().unchecked_ref())?;
        add_cb.forget();

        buttons.append_child(&test_btn)?;
        buttons.append_child(&add_btn)?;

        form.append_child(&url_label)?;
        form.append_child(&url_input)?;
        form.append_child(&name_label)?;
        form.append_child(&name_input)?;
        form.append_child(&token_label)?;
        form.append_child(&token_input)?;
        form.append_child(&buttons)?;

        content.append_child(&intro)?;
        content.append_child(&form)?;

        Ok(content)
    }

    fn build_connected_servers_section(&self, document: &Document) -> Result<Element, JsValue> {
        let section = document.create_element("div")?;
        section.set_class_name("mcp-connected-servers");
        section.set_class_name("mcp-connected-section");

        let separator = document.create_element("hr")?;
        separator.set_class_name("mcp-section-separator");

        let title = document.create_element("h4")?;
        title.set_inner_html("📊 Connected MCP Servers");
        title.set_class_name("mcp-connected-title");

        let list = document.create_element("div")?;
        list.set_id("mcp-servers-list");
        list.set_class_name("mcp-servers-list");

        if self.servers.is_empty() {
            let empty = document.create_element("p")?;
            empty.set_inner_html("No MCP servers connected yet.");
            empty.set_class_name("mcp-connected-empty");
            list.append_child(&empty)?;
        } else {
            for server in &self.servers {
                let card = self.create_server_card(document, server)?;
                list.append_child(&card)?;
            }
        }

        section.append_child(&separator)?;
        section.append_child(&title)?;
        section.append_child(&list)?;

        Ok(section)
    }

    fn create_server_card(
        &self,
        document: &Document,
        server: &MCPServerConfig,
    ) -> Result<Element, JsValue> {
        let card = document.create_element("div")?;
        card.set_class_name("mcp-server-card");
        card.set_class_name("mcp-server-card");

        let header = document.create_element("div")?;
        header.set_class_name("mcp-server-card-header");

        let info = document.create_element("div")?;

        let name = document.create_element("strong")?;
        name.set_inner_html(&server.name);

        let details = document.create_element("div")?;
        details.set_class_name("mcp-server-details");

        let tools_text = match server.tools_count {
            Some(count) => format!("{} tools", count),
            None => "? tools".to_string(),
        };

        let status_text = match &server.status {
            Some(ConnectionStatus::Success) => "✅ Online",
            Some(ConnectionStatus::Failed { .. }) => "❌ Failed",
            Some(ConnectionStatus::Testing) => "🔄 Testing",
            Some(ConnectionStatus::Timeout) => "⏱️ Slow",
            _ => "❓ Unknown",
        };

        details.set_inner_html(&format!("{} • {}", tools_text, status_text));

        let actions = document.create_element("div")?;
        actions.set_class_name("mcp-server-actions");

        let remove_btn = document.create_element("button")?;
        remove_btn.set_attribute("type", "button")?;
        remove_btn.set_class_name("btn btn-danger");
        remove_btn.set_inner_html("Remove");
        remove_btn.set_class_name("mcp-remove-btn");

        // Add remove handler
        let server_name = server.name.clone();
        let agent_id = self.agent_id;
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: web_sys::Event| {
            dispatch_global_message(crate::messages::Message::RemoveMCPServer {
                agent_id,
                server_name: server_name.clone(),
            });
        }));
        remove_btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();

        info.append_child(&name)?;
        info.append_child(&details)?;
        actions.append_child(&remove_btn)?;
        header.append_child(&info)?;
        header.append_child(&actions)?;
        card.append_child(&header)?;

        Ok(card)
    }

    fn get_input_value(document: &Document, id: &str) -> Option<String> {
        document
            .get_element_by_id(id)
            .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
            .map(|i| i.value())
    }

    /// Load MCP servers for this agent from the API
    pub async fn load_servers(&mut self) -> Result<(), JsValue> {
        let response = ApiClient::list_mcp_servers(self.agent_id).await?;
        let servers: Vec<MCPServerConfig> = serde_json::from_str(&response)
            .map_err(|e| JsValue::from_str(&format!("Failed to parse MCP servers: {:?}", e)))?;

        self.servers = servers;
        Ok(())
    }

    /// Add a new MCP server
    pub async fn add_server(&mut self, config: MCPServerConfig) -> Result<(), JsValue> {
        let config_json = serde_json::to_string(&config)
            .map_err(|e| JsValue::from_str(&format!("Failed to serialize config: {:?}", e)))?;

        let response = ApiClient::add_mcp_server(self.agent_id, &config_json).await?;
        let server: MCPServerConfig = serde_json::from_str(&response)
            .map_err(|e| JsValue::from_str(&format!("Failed to parse server response: {:?}", e)))?;

        self.servers.push(server);
        Ok(())
    }

    /// Remove an MCP server
    pub async fn remove_server(&mut self, server_name: &str) -> Result<(), JsValue> {
        ApiClient::remove_mcp_server(self.agent_id, server_name).await?;
        self.servers.retain(|s| s.name != server_name);
        Ok(())
    }

    /// Test connection to an MCP server
    pub async fn test_connection(
        &mut self,
        url: &str,
        name: &str,
        auth_token: &str,
    ) -> Result<ConnectionStatus, JsValue> {
        let test_config = serde_json::json!({
            "url": url,
            "name": name,
            "auth_token": auth_token
        });

        let test_json = test_config.to_string();
        let response = ApiClient::test_mcp_connection(self.agent_id, &test_json).await?;

        // Parse test result
        let result: serde_json::Value = serde_json::from_str(&response)
            .map_err(|e| JsValue::from_str(&format!("Failed to parse test result: {:?}", e)))?;

        let status = if result["success"].as_bool().unwrap_or(false) {
            ConnectionStatus::Success
        } else {
            let error = result["error"]
                .as_str()
                .unwrap_or("Unknown error")
                .to_string();
            ConnectionStatus::Failed { error }
        };

        self.connection_tests
            .insert(name.to_string(), status.clone());
        Ok(status)
    }
}
