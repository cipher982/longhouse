#!/bin/bash

echo "🎯 Fixing specific compilation error patterns..."

# Get list of files with errors
FILES_WITH_ERRORS=$(cargo check 2>&1 | grep -o "src/[^:]*\.rs" | sort | uniq)

for file in $FILES_WITH_ERRORS; do
    echo "🔧 Processing $file"
    
    # Fix import statements - most common errors
    sed -i.bak \
        -e 's/use crate::models::{[^}]*Node[^}]*}/use crate::models::{WorkflowNode, WorkflowEdge, UiNodeState}/g' \
        -e 's/use crate::models::Node/use crate::models::WorkflowNode/g' \
        -e 's/use crate::models::CanvasNode/use crate::models::WorkflowNode/g' \
        -e 's/use crate::models::Edge/use crate::models::WorkflowEdge/g' \
        "$file"
    
    # Fix type references in function signatures
    sed -i.bak \
        -e 's/HashMap<String, Node>/HashMap<String, WorkflowNode>/g' \
        -e 's/HashMap<String, CanvasNode>/HashMap<String, WorkflowNode>/g' \
        -e 's/Vec<Edge>/Vec<WorkflowEdge>/g' \
        -e 's/: Node/: WorkflowNode/g' \
        -e 's/: CanvasNode/: WorkflowNode/g' \
        "$file"
    
    # Fix state field references 
    sed -i.bak \
        -e 's/state\.nodes/state.workflow_nodes/g' \
        -e 's/\.nodes\.get/\.workflow_nodes.get/g' \
        -e 's/\.nodes\.insert/\.workflow_nodes.insert/g' \
        -e 's/\.nodes\.remove/\.workflow_nodes.remove/g' \
        -e 's/\.nodes\.iter/\.workflow_nodes.iter/g' \
        "$file"
    
    # Fix struct construction
    sed -i.bak \
        -e 's/CanvasNode {/WorkflowNode { node_id: node_id.clone(), node_type: crate::models::GeneratedNodeType::Variant0("Generic".to_string()), config: { let mut config = serde_json::Map::new();/g' \
        "$file"
done

echo "✅ Targeted fixes applied!"
echo "🔍 Checking compilation status..."
cargo check 2>&1 | tail -10