# Design Token Architecture (2025 Refresh)

## Goals

- Establish a single source of truth for colors, typography, spacing, radii, shadows, motion, and z-index values.
- Emit framework-agnostic assets (CSS variables today, TypeScript manifest later) that can be consumed by legacy global styles and future component-scoped styles.
- Preserve backward compatibility for existing CSS variables while introducing semantic, tokenised names.

## Token Source

- Package: `packages/design-tokens/` (npm: `@swarmlet/design-tokens`)
- Format: [Design Tokens Community Group 1.0 schema](https://design-tokens.org/) for forward compatibility with industry tooling (Style Dictionary, Token Studio, Specify, etc.).
- File: `tokens.json` containing nested categories (color, font, space, radius, shadow, motion, zIndex, component).

## Build Pipeline

- Script: `packages/design-tokens/build.mjs`
  - Reads `tokens.json`.
  - Emits to `dist/`:
    - `core.css` - Core token CSS variables wrapped in `@layer tokens`
    - `legacy-aliases.css` - Backward-compatible aliases for existing CSS variables
    - `theme-solid.css`, `theme-glass.css` - Theme variants
    - `tokens.ts` - TypeScript manifest for consumption in TS/JS
- Build command: `cd packages/design-tokens && bun run build`

## Typography System

- Base font size: `font.size.base = 0.875rem` (14px) to honour current layout density.
- Scale: `xs 0.75rem`, `sm 0.8125rem`, `md 0.9375rem`, `base 0.875rem`, `lg 1rem`, `xl 1.125rem`, `2xl 1.25rem`, `3xl 1.5rem`, `4xl 2rem`, `5xl 4rem`.
- Line heights: tight (1.2), default (1.4), relaxed (1.6).
- Component aliases: `component.button.fontSize = {font.size.sm}`, `component.toolbar.fontSize = {font.size.sm}`, etc., easing future changes per component family.

## Migration Plan

1. Generate `tokens.css` and update global entry point (`legacy.css`) to import it before other layers.
2. Replace hard-coded values in high-impact components (chat toolbar buttons, forms, dashboard headings) with new CSS variables.
3. Introduce cascade layers: `@layer tokens`, `@layer base`, `@layer components`, `@layer utilities`.
4. Phase out direct usage of legacy variables by updating components to semantic ones.
5. Update `scripts/validate-css-classes.js` (future work) to understand the new layers or migrate to module-aware validation.

## Future Enhancements

- Generate JSON for backend/marketing sites to ensure cross-platform consistency.
- Wire tokens into Storybook or visual regression tests to catch accidental token drift.
- Explore component-level styling (CSS Modules or variant-driven styling) once tokens are widely adopted.
