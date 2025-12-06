# CSS Class Validation

## Problem

We encountered unstyled UI elements in production where developers added `className` attributes to JSX elements but forgot to add corresponding CSS rules. This led to:

- **Message action buttons** (copy button) appearing as plain text
- **Tool buttons** (workflow, export) lacking proper styling
- Poor user experience and unprofessional appearance

## Solution

We've implemented a multi-layered validation system to catch missing CSS classes before they reach production:

### 1. Static Analysis Script (`validate-css-classes.js`)

A Node.js script that scans the codebase for className usage and validates against defined CSS rules.

**Usage:**

```bash
npm run validate:css
```

**What it does:**

- Scans all `.tsx` and `.jsx` files in `apps/zerg/frontend-web/src`
- Extracts className values (including from `clsx` calls)
- Parses all `.css` files in `apps/zerg/frontend-web/src/styles`
- Reports any className that's used but not defined

**Exit codes:**

- `0` - All classes are defined ✅
- `1` - Found undefined classes ❌

### 2. Runtime E2E Tests (`styling-validation.spec.ts`)

Playwright tests that validate styling at runtime in a real browser environment.

**Usage:**

```bash
cd apps/zerg/e2e
npx playwright test tests/styling-validation.spec.ts
```

**What it validates:**

- Interactive elements have proper cursor styles
- Buttons have visible backgrounds and borders
- Hover states work correctly
- Design tokens are properly applied
- No elements use default browser styling

### 3. Combined Validation

Run all validation checks together:

```bash
npm run validate:all
```

This runs:

1. CSS class validation
2. Type contract checks
3. Other validation scripts

## Adding to CI/CD

To prevent this issue in the future, add to your CI pipeline:

```yaml
# .github/workflows/ci.yml
- name: Validate CSS classes
  run: npm run validate:css

- name: Run styling tests
  run: |
    cd apps/zerg/e2e
    npx playwright test tests/styling-validation.spec.ts
```

## Development Workflow

### Before committing:

```bash
npm run validate:all
```

### When adding new UI elements:

1. Add the JSX with className
2. Add corresponding CSS rules to appropriate stylesheet
3. Run `npm run validate:css` to verify
4. Test in browser to ensure styling looks correct
5. E2E tests will catch any issues during CI

## Design System Integration

All new UI elements should use design tokens from `styles/tokens.css`:

```css
/* ✅ Good - uses design tokens */
.my-button {
  padding: var(--spacing-md);
  border-radius: var(--radius-md);
  background-color: var(--bg-button);
  transition: background-color var(--transition-fast);
}

/* ❌ Bad - hardcoded values */
.my-button {
  padding: 16px;
  border-radius: 8px;
  background-color: #2c2c40;
  transition: background-color 150ms;
}
```

## Stylesheet Organization

Follow the existing pattern:

- **Component-specific styles**: Add to relevant file in `styles/css/`
- **Page-specific styles**: Add to page CSS file (e.g., `chat.css`, `dashboard.css`)
- **Reusable components**: Consider adding to `styles/css/components/`
- **Button variants**: Add to `styles/css/buttons.css`

## Common Patterns

### Button styling

```css
.my-action-btn {
  background: transparent;
  border: 1px solid var(--border-color);
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast);
}

.my-action-btn:hover {
  background-color: var(--bg-hover);
}

.my-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

### Interactive elements

Always include:

- `cursor: pointer` for clickable elements
- Hover states with `transition` for smooth animations
- Disabled states with reduced opacity
- Proper spacing using design tokens

## Troubleshooting

### Script reports false positives

Add className patterns to `IGNORE_PATTERNS` in `validate-css-classes.js`:

```javascript
const IGNORE_PATTERNS = [
  /^clsx$/,
  /^data-/,
  /^aria-/,
  /^my-dynamic-class-/, // Add your pattern
];
```

### Test failures

1. Check browser console for styling warnings
2. Inspect element to see computed styles
3. Verify CSS file is imported in the component
4. Check for CSS specificity issues (use more specific selectors if needed)

## Related Files

- `/scripts/validate-css-classes.js` - Static analysis script
- `/apps/zerg/e2e/tests/styling-validation.spec.ts` - Runtime validation tests
- `/apps/zerg/frontend-web/src/styles/` - All CSS stylesheets
- `/apps/zerg/frontend-web/src/styles/tokens.css` - Design system tokens

## Benefits

✅ **Catches issues early** - Before code review
✅ **Prevents regressions** - CI blocks PRs with unstyled elements
✅ **Maintains consistency** - Enforces use of design tokens
✅ **Improves DX** - Clear error messages with file locations
✅ **Self-documenting** - Test suite shows expected styling patterns

## History

**Fixed Elements (2025-10-26):**

- `.message-action-btn` - Copy message button in chat interface (ChatPage.tsx:744)
- `.tool-btn` - Workflow and export buttons in chat tools (ChatPage.tsx:782, 790)
- `.thread-title-input` - Thread title editing input
- `.workflow-panel`, `.workflow-panel-header`, `.workflow-panel-content` - Workflow execution panel
- `.execute-workflow-btn`, `.close-panel-btn` - Workflow panel buttons

These elements were rendering with no styles, appearing as unstyled HTML elements.
