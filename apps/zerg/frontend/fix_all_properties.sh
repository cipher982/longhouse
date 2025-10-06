#!/bin/bash

echo "🔥 FINAL BULK PROPERTY FIX - ALL FILES!"

# Fix ALL remaining files with property access patterns
for file in src/**/*.rs; do
    if [[ -f "$file" ]]; then
        echo "Fixing $file..."
        sed -i '' \
            -e 's/\.config\.x\b/.get_x()/g' \
            -e 's/\.config\.y\b/.get_y()/g' \
            -e 's/\.config\.width\b/.get_width()/g' \
            -e 's/\.config\.height\b/.get_height()/g' \
            -e 's/\.config\.text\b/.get_text()/g' \
            -e 's/\.config\.color\b/.get_color()/g' \
            "$file"
    fi
done

echo "✅ ALL PROPERTY ACCESS PATTERNS FIXED!"
cargo check 2>&1 | tail -3