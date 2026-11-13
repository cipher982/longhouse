/**
 * Lightweight syntax highlighter setup
 *
 * Uses PrismLight instead of full Prism to avoid bundling ALL 200+ languages.
 * Only registers languages actually used in the app.
 *
 * This reduces bundle size by ~800KB.
 *
 * Import PrismLight from its own file, not from the package root: the root
 * re-exports the full Prism build, and Vite's dev server then loads every
 * refractor language (about 300 modules) as soon as a page uses this file.
 */
/// <reference types="react-syntax-highlighter" />
import type { CSSProperties } from 'react';
import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-light';

// Import only the languages we need
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust';
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go';
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml';
import diff from 'react-syntax-highlighter/dist/esm/languages/prism/diff';
import swift from 'react-syntax-highlighter/dist/esm/languages/prism/swift';
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup';
import ini from 'react-syntax-highlighter/dist/esm/languages/prism/ini';

// Register languages
SyntaxHighlighter.registerLanguage('javascript', javascript);
SyntaxHighlighter.registerLanguage('js', javascript);
SyntaxHighlighter.registerLanguage('typescript', typescript);
SyntaxHighlighter.registerLanguage('ts', typescript);
SyntaxHighlighter.registerLanguage('python', python);
SyntaxHighlighter.registerLanguage('py', python);
SyntaxHighlighter.registerLanguage('bash', bash);
SyntaxHighlighter.registerLanguage('sh', bash);
SyntaxHighlighter.registerLanguage('shell', bash);
SyntaxHighlighter.registerLanguage('zsh', bash);
SyntaxHighlighter.registerLanguage('console', bash);
SyntaxHighlighter.registerLanguage('json', json);
SyntaxHighlighter.registerLanguage('jsonc', json);
SyntaxHighlighter.registerLanguage('yaml', yaml);
SyntaxHighlighter.registerLanguage('yml', yaml);
SyntaxHighlighter.registerLanguage('markdown', markdown);
SyntaxHighlighter.registerLanguage('md', markdown);
SyntaxHighlighter.registerLanguage('css', css);
SyntaxHighlighter.registerLanguage('sql', sql);
SyntaxHighlighter.registerLanguage('jsx', jsx);
SyntaxHighlighter.registerLanguage('tsx', tsx);
SyntaxHighlighter.registerLanguage('rust', rust);
SyntaxHighlighter.registerLanguage('rs', rust);
SyntaxHighlighter.registerLanguage('go', go);
SyntaxHighlighter.registerLanguage('golang', go);
SyntaxHighlighter.registerLanguage('toml', toml);
SyntaxHighlighter.registerLanguage('diff', diff);
SyntaxHighlighter.registerLanguage('swift', swift);
SyntaxHighlighter.registerLanguage('markup', markup);
SyntaxHighlighter.registerLanguage('html', markup);
SyntaxHighlighter.registerLanguage('xml', markup);
SyntaxHighlighter.registerLanguage('ini', ini);

const REGISTERED_LANGUAGES = new Set([
  'javascript', 'js', 'typescript', 'ts', 'python', 'py',
  'bash', 'sh', 'shell', 'zsh', 'console',
  'json', 'jsonc', 'yaml', 'yml', 'markdown', 'md',
  'css', 'sql', 'jsx', 'tsx',
  'rust', 'rs', 'go', 'golang', 'toml', 'diff', 'swift',
  'markup', 'html', 'xml', 'ini',
]);

/** Return the registered Prism language name if supported, else null. */
export function resolveHighlightLanguage(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const normalized = raw.trim().toLowerCase();
  return REGISTERED_LANGUAGES.has(normalized) ? normalized : null;
}

/** Warm Ember-tuned Prism theme matching Longhouse's #0B0908 / #171210 ground. */
export const emberPrismTheme: Record<string, CSSProperties> = {
  'code[class*="language-"]': {
    color: '#EAE0D0',
    background: 'transparent',
    fontFamily: 'var(--font-family-mono)',
    textAlign: 'left',
    whiteSpace: 'pre',
    wordSpacing: 'normal',
    wordBreak: 'normal',
    lineHeight: 1.5,
    tabSize: 2,
  },
  'pre[class*="language-"]': {
    color: '#EAE0D0',
    background: 'transparent',
    fontFamily: 'var(--font-family-mono)',
    textAlign: 'left',
    whiteSpace: 'pre',
    wordSpacing: 'normal',
    wordBreak: 'normal',
    lineHeight: 1.5,
    tabSize: 2,
    overflow: 'auto',
    margin: 0,
  },
  comment: { color: '#8A7862', fontStyle: 'italic' },
  prolog: { color: '#8A7862' },
  doctype: { color: '#8A7862' },
  cdata: { color: '#8A7862' },
  punctuation: { color: '#A8957E' },
  property: { color: '#D97757' },
  tag: { color: '#F5D078' },
  boolean: { color: '#F5A33B' },
  number: { color: '#F5A33B' },
  constant: { color: '#F5A33B' },
  symbol: { color: '#F5A33B' },
  deleted: { color: '#FF9B95' },
  selector: { color: '#E9B949' },
  'attr-name': { color: '#F5D078' },
  string: { color: '#9BC4A2' },
  char: { color: '#9BC4A2' },
  builtin: { color: '#F5D078' },
  inserted: { color: '#8EEDB9' },
  operator: { color: '#C4B096' },
  entity: { color: '#C4B096' },
  url: { color: '#C4B096' },
  variable: { color: '#D97757' },
  atrule: { color: '#E9B949' },
  'attr-value': { color: '#9BC4A2' },
  function: { color: '#F5D078' },
  'class-name': { color: '#F5D078' },
  keyword: { color: '#E9B949' },
  regex: { color: '#D97757' },
  important: { color: '#E9B949', fontWeight: 600 },
  bold: { fontWeight: 600 },
  italic: { fontStyle: 'italic' },
};

export { SyntaxHighlighter };
