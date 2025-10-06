#!/bin/bash

# Canvas Persistence Schema Migration - Automated Fix Script
# This script performs systematic find/replace operations for the schema migration

echo "🔧 Starting automated schema migration fixes..."

# Function to perform safe replacements in a file
safe_replace() {
    local file="$1"
    local search="$2" 
    local replace="$3"
    local description="$4"
    
    if [[ -f "$file" ]]; then
        echo "  📝 $description in $file"
        # Use sed for safe in-place replacement
        sed -i.bak "s/$search/$replace/g" "$file"
    fi
}

# 1. Type name replacements
echo "📋 Step 1: Updating type names..."
find src -name "*.rs" -type f | while read file; do
    # Basic type replacements
    safe_replace "$file" "Node" "WorkflowNode" "Replace Node type"
    safe_replace "$file" "CanvasNode" "WorkflowNode" "Replace CanvasNode type" 
    safe_replace "$file" "crate::models::Edge" "crate::models::WorkflowEdge" "Replace Edge type"
    safe_replace "$file" "Vec<crate::models::Edge>" "Vec<crate::models::WorkflowEdge>" "Replace Edge vector"
done

# 2. Field access pattern replacements  
echo "📋 Step 2: Updating field access patterns..."
find src -name "*.rs" -type f | while read file; do
    # Property access updates - need to be careful with these
    safe_replace "$file" "\.agent_id" ".config.get(\"agent_id\").and_then(|v| v.as_u64().map(|id| id as u32))" "Update agent_id access"
    safe_replace "$file" "\.parent_id" ".config.get(\"parent_id\").and_then(|v| v.as_str()).map(|s| s.to_string())" "Update parent_id access"
    safe_replace "$file" "\.x" ".config.get(\"x\").and_then(|v| v.as_f64()).unwrap_or(0.0)" "Update x coordinate access"
    safe_replace "$file" "\.y" ".config.get(\"y\").and_then(|v| v.as_f64()).unwrap_or(0.0)" "Update y coordinate access"
    safe_replace "$file" "\.color" ".config.get(\"color\").and_then(|v| v.as_str()).unwrap_or(\"#000000\").to_string()" "Update color access"
done

# 3. State field replacements
echo "📋 Step 3: Updating state field references..."
find src -name "*.rs" -type f | while read file; do
    safe_replace "$file" "state\.nodes" "state.workflow_nodes" "Update nodes field reference"
    safe_replace "$file" "\.nodes\." ".workflow_nodes." "Update nodes method calls"
done

# 4. Import statement fixes
echo "📋 Step 4: Updating import statements..."
find src -name "*.rs" -type f | while read file; do
    # Add missing imports where needed
    if grep -q "UiNodeState::" "$file" && ! grep -q "use.*UiNodeState" "$file"; then
        echo "  📝 Adding UiNodeState import to $file"
        sed -i.bak '1i use crate::models::UiNodeState;' "$file"
    fi
    
    if grep -q "WorkflowEdge" "$file" && ! grep -q "use.*WorkflowEdge" "$file"; then
        echo "  📝 Adding WorkflowEdge import to $file"  
        sed -i.bak '1i use crate::models::WorkflowEdge;' "$file"
    fi
done

echo "✅ Automated fixes complete!"
echo "🔍 Running cargo check to see remaining issues..."
cargo check 2>&1 | head -20