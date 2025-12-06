# Zerg Design System

_Status: ✅ COMPLETED_ · _Completed: June 2025_ · _Moved to completed: June 15, 2025_

This document introduces the shared visual language of the Zerg front-end – tokens, components and composition rules – so that anyone can ship consistent UI without memorising every hex value or spacing multiple.

## 1. Tokens

All primitive design decisions (colour, spacing, radius, shadow, motion) live in **`frontend/www/tokens.css`** under the `:root {}` selector.

Guidelines:

- **Never** hard-code colours – use `var(--primary)` etc.
- Need a value that doesn’t exist yet? Add it once to `tokens.css`, then reference.
- Spacing always comes from the 4-8-16-24-32 scale (`--spacing-sm`, `--spacing-md`…). When you’re tempted to invent 12 px – use 8 px or 16 px instead.

### Z-index scale _(July 2025)_

Overlay issues prompted a **formal z-index hierarchy**. The following tokens
now live in `tokens.css` and must be used instead of raw numbers:

```css
--z-canvas: 10; /* interactive canvas */
--z-toolbar: 20; /* workflow bar, header */
--z-overlay: 1000; /* dropdowns, modals, toasts */
```

_No component may set a higher value than `--z-overlay` without a design
review._ A Stylelint rule will flag any hard-coded `z-index` ≥ 30.

## 2. Components

### Buttons (`css/buttons.css`)

Variants: primary, secondary, ghost; plus size modifiers `btn-sm`, `btn-lg`.

### Form controls (`css/forms.css`)

All inputs share the dark-surface + focus-ring aesthetic. Add the class `form-control` or rely on native selectors (`input`, `select`, `textarea` already match).

### Toasts (`css/toast.css`)

Call `toast::success("…")` / `toast::error("…")` from Rust or use the stand-alone JS helper in the component gallery.

## 3. Layout utilities (`css/util.css`)

Helpers like `.flex`, `.flex-col`, `.items-center` – kept minimal; prefer semantic containers in Rust/Yew code when possible.

## 4. Motion

Use the variables `--transition-fast`, `--transition-normal`, `--transition-slow` in `transition:` / `animation:` declarations. Don’t invent ad-hoc cubic-beziers.

## 5. Accessibility

- All interactive elements must have a visible focus ring – check in keyboard nav. Our button, tab and form styles already include one.
- Text colour contrast against backgrounds must meet WCAG AA; the token palette was chosen accordingly but double-check unusual combos.

## 7. Overlay portal

All floating UI elements that must sit **above** the main app (dropdowns,
context-menus, tooltips, modals) are now rendered into a dedicated portal
`<div id="overlay-root">` injected near the end of `index.html`.

Rust helper:

```rust
dom_utils::mount_in_overlay(&element);
```

This prevents clipping by transformed or `overflow:hidden` ancestors and makes
the overlay layer predictable across the app.

## 6. Contributing

1. Add or update CSS in a dedicated file under `frontend/www/css/`.
2. Link it in `index.html` (order doesn’t matter – files have unique class scopes).
3. Run `pre-commit run --all-files` to satisfy stylelint & prettier (once we land the tooling task).
4. Open a PR; screenshots are welcome.

---

For live examples of every component visit `/dev_components.html` when running `trunk serve`. The page pulls in the exact same modules as production, so what you see _is_ what users will get.
