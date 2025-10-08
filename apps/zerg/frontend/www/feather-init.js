/* ------------------------------------------------------------------
   Initialise Feather icons once the DOM and external script are loaded.
   The library automatically replaces any element with [data-feather]
   attribute with an inline SVG that inherits `currentColor`.
------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {
  if (window.feather && typeof window.feather.replace === 'function') {
    window.feather.replace({
      'stroke-width': 1.5,
    });

    // Auto-upgrade any icons injected later (e.g. via WASM)
    const observer = new MutationObserver(() => {
      window.feather.replace({ 'stroke-width': 1.5 });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
});
