#!/usr/bin/env node

/**
 * CSS Class Validator
 *
 * Scans React/TypeScript files for className usage and validates that
 * corresponding CSS rules exist. Prevents unstyled elements from making
 * it to production.
 *
 * Usage: node scripts/validate-css-classes.js
 */

const fs = require('fs');
const path = require('path');
const glob = require('glob');

// Configuration
const FRONTEND_DIR = path.join(__dirname, '../apps/zerg/frontend-web');
const SRC_DIR = path.join(FRONTEND_DIR, 'src');
const STYLES_DIR = path.join(SRC_DIR, 'styles');

// Classes to ignore (from external libraries, dynamic classes, etc.)
const IGNORE_PATTERNS = [
  /^clsx$/,
  /^data-/,
  /^aria-/,
  /^test-/,
  /^\$/,  // Template literals
  /^[A-Z]/,  // Component names (PascalCase)
];

// Utility classes that don't need explicit definitions (from Tailwind-like systems)
const UTILITY_CLASSES = new Set([
  'active', 'disabled', 'selected', 'open', 'closed', 'visible', 'hidden',
  'pending', 'loading', 'error', 'success', 'warning',
]);

/**
 * Extract className values from JSX/TSX files
 */
function extractClassNames(filePath) {
  const content = fs.readFileSync(filePath, 'utf-8');
  const classNames = new Set();

  // Match className="..." and className={...}
  const patterns = [
    /className=["']([^"']+)["']/g,  // Static: className="foo bar"
    /className=\{clsx\(([^)]+)\)\}/g,  // clsx usage
    /className=\{["']([^"']+)["']\}/g,  // Interpolated: className={"foo"}
    /className=\{`([^`]+)`\}/g,  // Template literals
  ];

  patterns.forEach(pattern => {
    let match;
    while ((match = pattern.exec(content)) !== null) {
      const classString = match[1];

      // Handle clsx with object syntax: { "foo": condition, "bar": condition }
      if (classString.includes(':')) {
        const objectClasses = classString.match(/["']([^"']+)["']\s*:/g);
        if (objectClasses) {
          objectClasses.forEach(cls => {
            const cleaned = cls.replace(/["':\s]/g, '');
            if (cleaned) classNames.add(cleaned);
          });
        }
      }

      // Split by spaces for multi-class strings
      classString.split(/\s+/).forEach(cls => {
        const cleaned = cls.replace(/[{}"'`]/g, '').trim();
        if (cleaned && !cleaned.includes('${') && !cleaned.includes('?')) {
          classNames.add(cleaned);
        }
      });
    }
  });

  return classNames;
}

/**
 * Extract CSS class selectors from CSS files
 */
function extractCSSClasses(filePath) {
  const content = fs.readFileSync(filePath, 'utf-8');
  const classes = new Set();

  // Match .className, .className:hover, .className::before, etc.
  const pattern = /\.([a-zA-Z_-][a-zA-Z0-9_-]*)/g;
  let match;

  while ((match = pattern.exec(content)) !== null) {
    classes.add(match[1]);
  }

  return classes;
}

/**
 * Check if a className should be validated
 */
function shouldValidate(className) {
  if (UTILITY_CLASSES.has(className)) return false;
  if (IGNORE_PATTERNS.some(pattern => pattern.test(className))) return false;
  if (className.includes('-')) return true;  // Kebab-case classes are likely custom
  return true;
}

/**
 * Main validation logic
 */
function validateCSSClasses() {
  console.log('🔍 Scanning for CSS class usage...\n');

  // Find all TSX/JSX files
  const sourceFiles = glob.sync('**/*.{tsx,jsx}', {
    cwd: SRC_DIR,
    absolute: true,
    ignore: ['**/node_modules/**', '**/*.test.{tsx,jsx}', '**/*.spec.{tsx,jsx}']
  });

  // Find all CSS files
  const cssFiles = glob.sync('**/*.css', {
    cwd: STYLES_DIR,
    absolute: true,
  });

  console.log(`📄 Found ${sourceFiles.length} source files`);
  console.log(`🎨 Found ${cssFiles.length} CSS files\n`);

  // Extract all CSS classes
  const definedClasses = new Set();
  cssFiles.forEach(file => {
    const classes = extractCSSClasses(file);
    classes.forEach(cls => definedClasses.add(cls));
  });

  console.log(`✅ Found ${definedClasses.size} defined CSS classes\n`);

  // Extract all used classes and validate
  const usedClasses = new Map(); // className -> [files using it]
  const missingClasses = new Map(); // className -> [files using it]

  sourceFiles.forEach(file => {
    const classes = extractClassNames(file);
    const relativePath = path.relative(SRC_DIR, file);

    classes.forEach(cls => {
      if (!shouldValidate(cls)) return;

      if (!usedClasses.has(cls)) {
        usedClasses.set(cls, []);
      }
      usedClasses.get(cls).push(relativePath);

      if (!definedClasses.has(cls)) {
        if (!missingClasses.has(cls)) {
          missingClasses.set(cls, []);
        }
        missingClasses.get(cls).push(relativePath);
      }
    });
  });

  // Report results
  if (missingClasses.size === 0) {
    console.log('✅ All CSS classes are defined!\n');
    return 0;
  }

  console.log(`❌ Found ${missingClasses.size} undefined CSS classes:\n`);

  // Sort by number of files using the class (most used first)
  const sorted = Array.from(missingClasses.entries())
    .sort((a, b) => b[1].length - a[1].length);

  sorted.forEach(([className, files]) => {
    console.log(`  .${className}`);
    files.forEach(file => {
      console.log(`    - ${file}`);
    });
    console.log('');
  });

  console.log('💡 Fix: Add CSS rules for these classes or add them to IGNORE_PATTERNS\n');
  return 1;
}

// Run validation
const exitCode = validateCSSClasses();
process.exit(exitCode);
