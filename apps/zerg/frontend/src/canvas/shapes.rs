use web_sys::CanvasRenderingContext2d;
// Unused imports removed
use crate::constants::*;

pub fn draw_rounded_rect(
    context: &CanvasRenderingContext2d,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    color: &str,
) {
    context.save();

    // Shadow for depth
    context.set_shadow_color(SHADOW_COLOR);
    context.set_shadow_blur(8.0);
    context.set_shadow_offset_x(0.0);
    context.set_shadow_offset_y(2.0);

    // Background with gradient effect
    context.set_fill_style_str(color);

    draw_rounded_rect_path(context, x, y, width, height);

    context.fill();

    // Remove shadow for border
    context.set_shadow_blur(0.0);
    context.set_shadow_offset_x(0.0);
    context.set_shadow_offset_y(0.0);

    // Border
    context.set_line_width(1.5);
    context.set_stroke_style_str(NODE_BORDER_DEFAULT);

    // No longer handling selection here, it's handled in renderer.rs

    context.stroke();

    context.restore();
}

#[allow(dead_code)]
pub fn draw_arrow(context: &CanvasRenderingContext2d, x: f64, y: f64, dx: f64, dy: f64) {
    let head_len = 10.0; // length of arrow head
    let angle = f64::atan2(dy, dx);

    context.begin_path();
    context.move_to(x, y);
    context.line_to(
        x - head_len * f64::cos(angle - std::f64::consts::PI / 6.0),
        y - head_len * f64::sin(angle - std::f64::consts::PI / 6.0),
    );
    context.move_to(x, y);
    context.line_to(
        x - head_len * f64::cos(angle + std::f64::consts::PI / 6.0),
        y - head_len * f64::sin(angle + std::f64::consts::PI / 6.0),
    );
    context.set_stroke_style_str(CONNECTION_LINE_COLOR);
    context.set_line_width(2.0);
    context.stroke();
}

pub fn draw_thought_bubble(
    context: &CanvasRenderingContext2d,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    color: &str,
) {
    // Main bubble
    context.save();

    // Shadow
    context.set_shadow_color("rgba(0, 0, 0, 0.1)");
    context.set_shadow_blur(8.0);
    context.set_shadow_offset_x(2.0);
    context.set_shadow_offset_y(2.0);

    // Background
    context.set_fill_style_str(color);

    // Draw bubble path
    let radius = 15.0;

    context.begin_path();
    context.move_to(x + radius, y);
    context.line_to(x + width - radius, y);
    context.quadratic_curve_to(x + width, y, x + width, y + radius);
    context.line_to(x + width, y + height - radius);
    context.quadratic_curve_to(x + width, y + height, x + width - radius, y + height);
    context.line_to(x + radius + 40.0, y + height); // Leave space for tail

    // Add tail
    context.line_to(x + 20.0, y + height + 20.0);
    context.line_to(x + radius + 20.0, y + height);

    context.line_to(x + radius, y + height);
    context.quadratic_curve_to(x, y + height, x, y + height - radius);
    context.line_to(x, y + radius);
    context.quadratic_curve_to(x, y, x + radius, y);
    context.close_path();

    context.fill();

    // Border
    context.set_shadow_color("rgba(0, 0, 0, 0)"); // Remove shadow for border
    context.set_line_width(1.0);
    context.set_stroke_style_str("#000000");

    // No longer handling selection here

    context.stroke();

    context.restore();
}

pub fn draw_node_text(
    context: &CanvasRenderingContext2d,
    x: f64,
    y: f64,
    width: f64,
    _height: f64,
    text: &str,
) {
    // Removed unused import

    // Do not draw node.text for trigger nodes to avoid duplicated text
    // This check is now done in renderer.rs before calling this function

    context.save();

    // Text configuration
    context.set_font("13px system-ui, -apple-system, sans-serif");
    context.set_fill_style_str(NODE_TEXT_COLOR);
    context.set_text_align("left");
    context.set_text_baseline("top");

    let line_height = 16.0;

    // Split by words
    let words = text.split_whitespace().collect::<Vec<&str>>();
    let mut current_line = String::new();
    let mut current_y = y + 10.0; // Default offset

    // Adjust starting Y for agent nodes
    // This logic is now handled in renderer.rs before calling this function

    let max_width = width - 20.0; // Default padding

    for word in words {
        let test_line = if current_line.is_empty() {
            word.to_string()
        } else {
            format!("{} {}", current_line, word)
        };

        let test_metrics = context.measure_text(&test_line).unwrap();
        let test_width = test_metrics.width();

        if test_width > max_width && !current_line.is_empty() {
            // Draw the current line and start a new one
            context
                .fill_text(&current_line, x + 10.0, current_y)
                .unwrap(); // Default offset
            current_line = word.to_string();
            current_y += line_height;
        } else {
            current_line = test_line;
        }
    }

    // Draw the last line
    if !current_line.is_empty() {
        context
            .fill_text(&current_line, x + 10.0, current_y)
            .unwrap(); // Default offset
    }

    context.restore();
}

// Creates a rounded rectangle path without filling or stroking
pub fn draw_rounded_rect_path(
    context: &CanvasRenderingContext2d,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) {
    let radius = 15.0;

    // Draw rounded rectangle path
    context.begin_path();
    context.move_to(x + radius, y);
    context.line_to(x + width - radius, y);
    context.quadratic_curve_to(x + width, y, x + width, y + radius);
    context.line_to(x + width, y + height - radius);
    context.quadratic_curve_to(x + width, y + height, x + width - radius, y + height);
    context.line_to(x + radius, y + height);
    context.quadratic_curve_to(x, y + height, x, y + height - radius);
    context.line_to(x, y + radius);
    context.quadratic_curve_to(x, y, x + radius, y);
    context.close_path();
}
