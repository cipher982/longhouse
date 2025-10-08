/* ------------------------------------------------------------------
   Global Ripple Interaction – applies to both navigation tabs and any
   element with the `data-ripple` attribute or the class `.btn` (plus
   variants) so we don’t need separate scripts per component.
------------------------------------------------------------------ */

function attachRipples(selector) {
  document.querySelectorAll(selector).forEach((el) => {
    // Skip if the element already has a listener (idempotent init)
    if (el.__rippleAttached) return;
    el.__rippleAttached = true;

    el.style.position = el.style.position || 'relative';
    el.style.overflow = 'hidden';

    el.addEventListener('click', function (e) {
      const rect = this.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);

      const ripple = document.createElement('span');
      ripple.classList.add('ripple');
      ripple.style.width = ripple.style.height = `${size}px`;
      ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
      ripple.style.top = `${e.clientY - rect.top - size / 2}px`;

      this.appendChild(ripple);
      ripple.addEventListener('animationend', () => ripple.remove());
    });
  });
}

// Initialise after DOM ready
document.addEventListener('DOMContentLoaded', () => {
  attachRipples('.tabs-container .tab-button');
  attachRipples('.btn, .btn-primary, .btn-secondary, .btn-ghost');
  attachRipples('[data-ripple]');
});
