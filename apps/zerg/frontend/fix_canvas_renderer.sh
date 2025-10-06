#!/bin/bash

echo "🎯 Bulk fixing canvas renderer property access..."

# Fix canvas renderer with helper methods  
sed -i '' \
    -e 's/node\.config\.x\b/node.get_x()/g' \
    -e 's/node\.config\.y\b/node.get_y()/g' \
    -e 's/node\.config\.width\b/node.get_width()/g' \
    -e 's/node\.config\.height\b/node.get_height()/g' \
    -e 's/node\.config\.text\b/node.get_text()/g' \
    -e 's/node\.config\.color\b/node.get_color()/g' \
    src/canvas/renderer.rs

# Also fix from_node and to_node patterns
sed -i '' \
    -e 's/from_node\.config\.x\b/from_node.get_x()/g' \
    -e 's/from_node\.config\.y\b/from_node.get_y()/g' \
    -e 's/from_node\.config\.width\b/from_node.get_width()/g' \
    -e 's/from_node\.config\.height\b/from_node.get_height()/g' \
    -e 's/to_node\.config\.x\b/to_node.get_x()/g' \
    -e 's/to_node\.config\.y\b/to_node.get_y()/g' \
    -e 's/to_node\.config\.width\b/to_node.get_width()/g' \
    -e 's/to_node\.config\.height\b/to_node.get_height()/g' \
    src/canvas/renderer.rs

echo "✅ Canvas renderer bulk fixes applied!"
cargo check 2>&1 | tail -3