#![allow(clippy::redundant_closure_call)]
#![allow(clippy::needless_lifetimes)]
#![allow(clippy::match_single_binding)]
#![allow(clippy::clone_on_copy)]

// Type alias for compatibility
type WorkflowNodeType = NodeType;

#[doc = r" Error types."]
pub mod error {
    #[doc = r" Error from a `TryFrom` or `FromStr` implementation."]
    pub struct ConversionError(::std::borrow::Cow<'static, str>);
    impl ::std::error::Error for ConversionError {}
    impl ::std::fmt::Display for ConversionError {
        fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> Result<(), ::std::fmt::Error> {
            ::std::fmt::Display::fmt(&self.0, f)
        }
    }
    impl ::std::fmt::Debug for ConversionError {
        fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> Result<(), ::std::fmt::Error> {
            ::std::fmt::Debug::fmt(&self.0, f)
        }
    }
    impl From<&'static str> for ConversionError {
        fn from(value: &'static str) -> Self {
            Self(value.into())
        }
    }
    impl From<String> for ConversionError {
        fn from(value: String) -> Self {
            Self(value.into())
        }
    }
}
#[doc = "Node type configuration"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Node Type\","]
#[doc = "  \"description\": \"Node type configuration\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"object\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum NodeType {
    Variant0(::std::string::String),
    Variant1(::serde_json::Map<::std::string::String, ::serde_json::Value>),
}
impl ::std::convert::From<&Self> for NodeType {
    fn from(value: &NodeType) -> Self {
        value.clone()
    }
}
impl ::std::convert::From<::serde_json::Map<::std::string::String, ::serde_json::Value>>
    for NodeType
{
    fn from(value: ::serde_json::Map<::std::string::String, ::serde_json::Value>) -> Self {
        Self::Variant1(value)
    }
}
#[doc = "Canonical representation of complete workflow canvas data."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"WorkflowCanvas\","]
#[doc = "  \"description\": \"Canonical representation of complete workflow canvas data.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"edges\": {"]
#[doc = "      \"title\": \"Edges\","]
#[doc = "      \"description\": \"List of workflow edges\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"$ref\": \"#/$defs/WorkflowEdge\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"title\": \"Metadata\","]
#[doc = "      \"description\": \"Canvas metadata\","]
#[doc = "      \"type\": \"object\""]
#[doc = "    },"]
#[doc = "    \"nodes\": {"]
#[doc = "      \"title\": \"Nodes\","]
#[doc = "      \"description\": \"List of workflow nodes\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"$ref\": \"#/$defs/WorkflowNode\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct WorkflowCanvas {
    #[doc = "List of workflow edges"]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub edges: ::std::vec::Vec<WorkflowEdge>,
    #[doc = "Canvas metadata"]
    #[serde(default, skip_serializing_if = "::serde_json::Map::is_empty")]
    pub metadata: ::serde_json::Map<::std::string::String, ::serde_json::Value>,
    #[doc = "List of workflow nodes"]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub nodes: ::std::vec::Vec<WorkflowNode>,
}
impl ::std::convert::From<&WorkflowCanvas> for WorkflowCanvas {
    fn from(value: &WorkflowCanvas) -> Self {
        value.clone()
    }
}
impl ::std::default::Default for WorkflowCanvas {
    fn default() -> Self {
        Self {
            edges: Default::default(),
            metadata: Default::default(),
            nodes: Default::default(),
        }
    }
}
impl WorkflowCanvas {
    pub fn builder() -> builder::WorkflowCanvas {
        Default::default()
    }
}
#[doc = "Canonical representation of a workflow edge."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"WorkflowEdge\","]
#[doc = "  \"description\": \"Canonical representation of a workflow edge.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"required\": ["]
#[doc = "    \"from_node_id\","]
#[doc = "    \"to_node_id\""]
#[doc = "  ],"]
#[doc = "  \"properties\": {"]
#[doc = "    \"config\": {"]
#[doc = "      \"title\": \"Config\","]
#[doc = "      \"description\": \"Edge-specific configuration\","]
#[doc = "      \"type\": \"object\""]
#[doc = "    },"]
#[doc = "    \"from_node_id\": {"]
#[doc = "      \"title\": \"From Node Id\","]
#[doc = "      \"description\": \"Source node ID\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"to_node_id\": {"]
#[doc = "      \"title\": \"To Node Id\","]
#[doc = "      \"description\": \"Target node ID\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct WorkflowEdge {
    #[doc = "Edge-specific configuration"]
    #[serde(default, skip_serializing_if = "::serde_json::Map::is_empty")]
    pub config: ::serde_json::Map<::std::string::String, ::serde_json::Value>,
    #[doc = "Source node ID"]
    pub from_node_id: ::std::string::String,
    #[doc = "Target node ID"]
    pub to_node_id: ::std::string::String,
}
impl ::std::convert::From<&WorkflowEdge> for WorkflowEdge {
    fn from(value: &WorkflowEdge) -> Self {
        value.clone()
    }
}
impl WorkflowEdge {
    pub fn builder() -> builder::WorkflowEdge {
        Default::default()
    }
}
#[doc = "Canonical representation of a workflow node."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"WorkflowNode\","]
#[doc = "  \"description\": \"Canonical representation of a workflow node.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"required\": ["]
#[doc = "    \"node_id\","]
#[doc = "    \"node_type\""]
#[doc = "  ],"]
#[doc = "  \"properties\": {"]
#[doc = "    \"config\": {"]
#[doc = "      \"title\": \"Config\","]
#[doc = "      \"description\": \"Node-specific configuration\","]
#[doc = "      \"type\": \"object\""]
#[doc = "    },"]
#[doc = "    \"node_id\": {"]
#[doc = "      \"title\": \"Node Id\","]
#[doc = "      \"description\": \"Unique node identifier\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"node_type\": {"]
#[doc = "      \"title\": \"Node Type\","]
#[doc = "      \"description\": \"Node type configuration\","]
#[doc = "      \"anyOf\": ["]
#[doc = "        {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"object\""]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"position\": {"]
#[doc = "      \"title\": \"Position\","]
#[doc = "      \"description\": \"Node position on canvas\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"type\": \"number\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct WorkflowNode {
    #[doc = "Node-specific configuration"]
    #[serde(default)]
    pub config: crate::models::NodeConfig,
    #[doc = "Unique node identifier"]
    #[serde(rename = "id")]
    pub node_id: ::std::string::String,
    #[doc = "Node type configuration"]
    #[serde(rename = "type")]
    pub node_type: WorkflowNodeType,
    #[doc = "Node position on canvas"]
    #[serde(default)]
    pub position: crate::network::generated_client::PositionContract,
}
impl ::std::convert::From<&WorkflowNode> for WorkflowNode {
    fn from(value: &WorkflowNode) -> Self {
        value.clone()
    }
}
impl WorkflowNode {
    pub fn builder() -> builder::WorkflowNode {
        Default::default()
    }
}
#[doc = r" Types for composing complex structures."]
pub mod builder {
    #[derive(Clone, Debug)]
    pub struct WorkflowCanvas {
        edges: ::std::result::Result<::std::vec::Vec<super::WorkflowEdge>, ::std::string::String>,
        metadata: ::std::result::Result<
            ::serde_json::Map<::std::string::String, ::serde_json::Value>,
            ::std::string::String,
        >,
        nodes: ::std::result::Result<::std::vec::Vec<super::WorkflowNode>, ::std::string::String>,
    }
    impl ::std::default::Default for WorkflowCanvas {
        fn default() -> Self {
            Self {
                edges: Ok(Default::default()),
                metadata: Ok(Default::default()),
                nodes: Ok(Default::default()),
            }
        }
    }
    impl WorkflowCanvas {
        pub fn edges<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<::std::vec::Vec<super::WorkflowEdge>>,
            T::Error: ::std::fmt::Display,
        {
            self.edges = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for edges: {}", e));
            self
        }
        pub fn metadata<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<
                ::serde_json::Map<::std::string::String, ::serde_json::Value>,
            >,
            T::Error: ::std::fmt::Display,
        {
            self.metadata = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for metadata: {}", e));
            self
        }
        pub fn nodes<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<::std::vec::Vec<super::WorkflowNode>>,
            T::Error: ::std::fmt::Display,
        {
            self.nodes = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for nodes: {}", e));
            self
        }
    }
    impl ::std::convert::TryFrom<WorkflowCanvas> for super::WorkflowCanvas {
        type Error = super::error::ConversionError;
        fn try_from(
            value: WorkflowCanvas,
        ) -> ::std::result::Result<Self, super::error::ConversionError> {
            Ok(Self {
                edges: value.edges?,
                metadata: value.metadata?,
                nodes: value.nodes?,
            })
        }
    }
    impl ::std::convert::From<super::WorkflowCanvas> for WorkflowCanvas {
        fn from(value: super::WorkflowCanvas) -> Self {
            Self {
                edges: Ok(value.edges),
                metadata: Ok(value.metadata),
                nodes: Ok(value.nodes),
            }
        }
    }
    #[derive(Clone, Debug)]
    pub struct WorkflowEdge {
        config: ::std::result::Result<
            ::serde_json::Map<::std::string::String, ::serde_json::Value>,
            ::std::string::String,
        >,
        from_node_id: ::std::result::Result<::std::string::String, ::std::string::String>,
        to_node_id: ::std::result::Result<::std::string::String, ::std::string::String>,
    }
    impl ::std::default::Default for WorkflowEdge {
        fn default() -> Self {
            Self {
                config: Ok(Default::default()),
                from_node_id: Err("no value supplied for from_node_id".to_string()),
                to_node_id: Err("no value supplied for to_node_id".to_string()),
            }
        }
    }
    impl WorkflowEdge {
        pub fn config<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<
                ::serde_json::Map<::std::string::String, ::serde_json::Value>,
            >,
            T::Error: ::std::fmt::Display,
        {
            self.config = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for config: {}", e));
            self
        }
        pub fn from_node_id<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<::std::string::String>,
            T::Error: ::std::fmt::Display,
        {
            self.from_node_id = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for from_node_id: {}", e));
            self
        }
        pub fn to_node_id<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<::std::string::String>,
            T::Error: ::std::fmt::Display,
        {
            self.to_node_id = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for to_node_id: {}", e));
            self
        }
    }
    impl ::std::convert::TryFrom<WorkflowEdge> for super::WorkflowEdge {
        type Error = super::error::ConversionError;
        fn try_from(
            value: WorkflowEdge,
        ) -> ::std::result::Result<Self, super::error::ConversionError> {
            Ok(Self {
                config: value.config?,
                from_node_id: value.from_node_id?,
                to_node_id: value.to_node_id?,
            })
        }
    }
    impl ::std::convert::From<super::WorkflowEdge> for WorkflowEdge {
        fn from(value: super::WorkflowEdge) -> Self {
            Self {
                config: Ok(value.config),
                from_node_id: Ok(value.from_node_id),
                to_node_id: Ok(value.to_node_id),
            }
        }
    }
    #[derive(Clone, Debug)]
    pub struct WorkflowNode {
        config: ::std::result::Result<crate::models::NodeConfig, ::std::string::String>,
        node_id: ::std::result::Result<::std::string::String, ::std::string::String>,
        node_type: ::std::result::Result<super::NodeType, ::std::string::String>,
        position: ::std::result::Result<
            crate::network::generated_client::PositionContract,
            ::std::string::String,
        >,
    }
    impl ::std::default::Default for WorkflowNode {
        fn default() -> Self {
            Self {
                config: Ok(Default::default()),
                node_id: Err("no value supplied for node_id".to_string()),
                node_type: Err("no value supplied for node_type".to_string()),
                position: Ok(Default::default()),
            }
        }
    }
    impl WorkflowNode {
        pub fn config<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<crate::models::NodeConfig>,
            T::Error: ::std::fmt::Display,
        {
            self.config = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for config: {}", e));
            self
        }
        pub fn node_id<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<::std::string::String>,
            T::Error: ::std::fmt::Display,
        {
            self.node_id = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for node_id: {}", e));
            self
        }
        pub fn node_type<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<super::NodeType>,
            T::Error: ::std::fmt::Display,
        {
            self.node_type = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for node_type: {}", e));
            self
        }
        pub fn position<T>(mut self, value: T) -> Self
        where
            T: ::std::convert::TryInto<crate::network::generated_client::PositionContract>,
            T::Error: ::std::fmt::Display,
        {
            self.position = value
                .try_into()
                .map_err(|e| format!("error converting supplied value for position: {}", e));
            self
        }
    }
    impl ::std::convert::TryFrom<WorkflowNode> for super::WorkflowNode {
        type Error = super::error::ConversionError;
        fn try_from(
            value: WorkflowNode,
        ) -> ::std::result::Result<Self, super::error::ConversionError> {
            Ok(Self {
                config: value.config?,
                node_id: value.node_id?,
                node_type: value.node_type?,
                position: value.position?,
            })
        }
    }
    impl ::std::convert::From<super::WorkflowNode> for WorkflowNode {
        fn from(value: super::WorkflowNode) -> Self {
            Self {
                config: Ok(value.config),
                node_id: Ok(value.node_id),
                node_type: Ok(value.node_type),
                position: Ok(value.position),
            }
        }
    }
}
