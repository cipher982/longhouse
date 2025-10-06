use crate::models::{NodeType, ToolConfig, ToolVisibility, TriggerConfig, TriggerType, WorkflowNode};

/// Fluent builder for constructing WorkflowNode instances with compile-time semantics.
/// Ensures semantic type is set before visuals, preventing half-formed nodes.
pub struct NodeBuilder {
    id: Option<String>,
    semantic: NodeType,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    color: Option<String>,
    text: Option<String>,
}

impl NodeBuilder {
    /// Start a builder from an explicit semantic node type.
    pub fn from_semantic(semantic: NodeType) -> Self {
        Self {
            id: None,
            semantic,
            x: 0.0,
            y: 0.0,
            width: 200.0,
            height: 80.0,
            color: None,
            text: None,
        }
    }

    /// Convenience for triggers: NodeBuilder::trigger(TriggerType)
    pub fn trigger(tt: TriggerType) -> Self {
        Self::from_semantic(NodeType::Trigger {
            trigger_type: tt,
            config: TriggerConfig { params: Default::default(), enabled: true, filters: vec![] },
        })
    }

    /// Convenience for tools.
    pub fn tool(server_name: &str, tool_name: &str, config: ToolConfig, visibility: ToolVisibility) -> Self {
        Self::from_semantic(NodeType::Tool {
            server_name: server_name.to_string(),
            tool_name: tool_name.to_string(),
            config,
            visibility,
        })
    }

    pub fn id(mut self, id: String) -> Self {
        self.id = Some(id);
        self
    }

    pub fn at(mut self, x: f64, y: f64) -> Self {
        self.x = x;
        self.y = y;
        self
    }

    pub fn size(mut self, width: f64, height: f64) -> Self {
        self.width = width;
        self.height = height;
        self
    }

    pub fn color(mut self, color: &str) -> Self {
        self.color = Some(color.to_string());
        self
    }

    pub fn label(mut self, text: &str) -> Self {
        self.text = Some(text.to_string());
        self
    }

    /// Finalize node creation.
    pub fn build(self) -> WorkflowNode {
        let id = self.id.unwrap_or_else(|| format!("node-{}", js_sys::Date::now() as u64));

        let mut node = WorkflowNode::new_with_type(id, &self.semantic);
        // Apply visuals after semantics (non-destructive)
        let color = self.color.unwrap_or_else(|| match &self.semantic {
            NodeType::UserInput => "#3498db".to_string(),
            NodeType::ResponseOutput => "#9b59b6".to_string(),
            NodeType::AgentIdentity => "#2ecc71".to_string(),
            NodeType::GenericNode => "#95a5a6".to_string(),
            NodeType::Tool { .. } => "#f59e0b".to_string(),
            NodeType::Trigger { .. } => "#10b981".to_string(),
        });
        let text = self.text.unwrap_or_else(|| match &self.semantic {
            NodeType::Tool { tool_name, .. } => tool_name.clone(),
            NodeType::Trigger { .. } => "Trigger".to_string(),
            NodeType::AgentIdentity => "Agent".to_string(),
            NodeType::UserInput => "User Input".to_string(),
            NodeType::ResponseOutput => "Response".to_string(),
            NodeType::GenericNode => "Node".to_string(),
        });

        node.apply_visual(self.x, self.y, self.width, self.height, &color, &text);
        node
    }
}

