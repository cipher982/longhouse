#!/bin/bash

echo "🔧 Applying surgical fixes to critical import errors..."

# Fix only the most critical import errors that cause widespread issues

# 1. Fix state.rs import
echo "📝 Fixing state.rs UiNodeState import..."
sed -i '' '17a\
    UiNodeState,\
' src/state.rs

# 2. Fix storage.rs imports
echo "📝 Fixing storage.rs imports..."
sed -i '' 's/use crate::models::Node/use crate::models::WorkflowNode/g' src/storage.rs
sed -i '' 's/use crate::models::CanvasNode/use crate::models::WorkflowNode/g' src/storage.rs

# 3. Fix component imports only
echo "📝 Fixing component imports..."
sed -i '' 's/use crate::models::Node/use crate::models::WorkflowNode/g' src/components/node_palette.rs
sed -i '' 's/use crate::models::Node/use crate::models::WorkflowNode/g' src/components/tool_config_modal.rs  
sed -i '' 's/use crate::models::Node/use crate::models::WorkflowNode/g' src/components/trigger_config_modal.rs

# 4. Add the missing generated mod export
echo "📝 Adding workflow module export to generated/mod.rs..."
cat >> src/generated/mod.rs << 'EOF'
pub mod workflow;
pub use workflow::{WorkflowCanvas, WorkflowNode, WorkflowEdge, NodeType};
EOF

echo "✅ Surgical fixes complete! Checking compilation..."
cargo check 2>&1 | head -20