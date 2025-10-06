#!/bin/bash

echo "🚀 Bulk fixing property access patterns..."

# Fix state.rs property access patterns
sed -i '' \
    -e 's/\.config\.x\b/.get_x()/g' \
    -e 's/\.config\.y\b/.get_y()/g' \
    -e 's/\.config\.width\b/.get_width()/g' \
    -e 's/\.config\.height\b/.get_height()/g' \    
    -e 's/\.config\.text\b/.get_text()/g' \
    -e 's/\.config\.color\b/.get_color()/g' \
    src/state.rs

# Fix storage.rs
sed -i '' \
    -e 's/\.config\.x\b/.get_x()/g' \
    -e 's/\.config\.y\b/.get_y()/g' \
    -e 's/\.config\.width\b/.get_width()/g' \
    -e 's/\.config\.height\b/.get_height()/g' \
    -e 's/\.config\.text\b/.get_text()/g' \
    -e 's/\.config\.color\b/.get_color()/g' \
    src/storage.rs

# Fix canvas renderer
sed -i '' \
    -e 's/\.config\.x\b/.get_x()/g' \
    -e 's/\.config\.y\b/.get_y()/g' \
    -e 's/\.config\.width\b/.get_width()/g' \
    -e 's/\.config\.height\b/.get_height()/g' \
    -e 's/\.config\.text\b/.get_text()/g' \
    -e 's/\.config\.color\b/.get_color()/g' \
    src/canvas/renderer.rs

echo "✅ Bulk property fixes applied!"
cargo check 2>&1 | tail -3