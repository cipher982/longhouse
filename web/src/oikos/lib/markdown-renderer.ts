/**
 * Markdown Renderer for Chat Messages
 *
 * Handles rendering LLM output with proper markdown support.
 * Uses marked for parsing and DOMPurify for XSS protection.
 */

import { marked, type MarkedOptions } from 'marked';
import DOMPurify, { type Config as DOMPurifyConfig } from 'dompurify';

// Configure marked options
const markedOptions: MarkedOptions = {
  breaks: true,      // Convert \n to <br> (GFM-style)
  gfm: true,         // GitHub Flavored Markdown
};

// Apply configuration
marked.setOptions(markedOptions);

// Configure DOMPurify to allow safe HTML elements
const purifyConfig: DOMPurifyConfig = {
  ALLOWED_TAGS: [
    'p', 'br', 'strong', 'em', 'b', 'i', 'u', 's', 'del',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li',
    'blockquote',
    'pre', 'code',
    'a',
    'hr',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'span', 'div'
  ],
  ALLOWED_ATTR: ['href', 'target', 'rel', 'class', 'id'],
  // Open links in new tab and add security attributes
  ADD_ATTR: ['target', 'rel'],
  // Return string instead of TrustedHTML
  RETURN_TRUSTED_TYPE: false,
};

// Hook to add target="_blank" and rel="noopener" to links
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});

/**
 * Render markdown to sanitized HTML
 * Use for completed messages (not streaming)
 */
export function renderMarkdown(text: string): string {
  if (!text) return '';

  try {
    const html = marked.parse(text, { async: false }) as string;
    return DOMPurify.sanitize(html, purifyConfig) as string;
  } catch (error) {
    console.error('Markdown parsing error:', error);
    // Fallback to escaped plain text
    return escapeHtml(text);
  }
}

/**
 * Render streaming content with preserved whitespace
 * During streaming, we use plain text with whitespace preservation
 * to avoid incomplete markdown artifacts (unclosed code blocks, etc.)
 */
export function renderStreamingContent(text: string): string {
  if (!text) return '';
  return escapeHtml(text);
}

/**
 * Escape HTML to prevent XSS (for streaming/plain text mode)
 */
export function escapeHtml(text: string): string {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Check if content looks like it has incomplete markdown
 * Useful for deciding when to switch from streaming to rendered mode
 */
export function hasIncompleteMarkdown(text: string): boolean {
  // Check for unclosed code blocks
  const codeBlockMatches = text.match(/```/g);
  if (codeBlockMatches && codeBlockMatches.length % 2 !== 0) {
    return true;
  }

  // Check for unclosed inline code
  const inlineCodeMatches = text.match(/(?<!`)`(?!`)/g);
  if (inlineCodeMatches && inlineCodeMatches.length % 2 !== 0) {
    return true;
  }

  return false;
}
