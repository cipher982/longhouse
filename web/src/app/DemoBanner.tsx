/**
 * Banner shown under the app bar in demo mode.
 * Informs visitors they're viewing a shared read-only demo.
 *
 * It is a row of the app shell like the host-link banner and stacks like it:
 * above the page, below the app bar, so the menus the bar opens drop over it.
 */
export default function DemoBanner() {
  return (
    <div
      style={{
        zIndex: 10,
        background: 'var(--color-surface-elevated)',
        color: 'var(--color-text-secondary)',
        boxShadow: 'inset 0 1px 0 rgba(233, 185, 73, 0.45), 0 1px 0 var(--color-border-subtle)',
        textAlign: 'center',
        padding: '8px 16px',
        fontSize: '13.5px',
        fontWeight: 500,
      }}
    >
      You're viewing a shared read-only demo &mdash;{' '}
      <a
        href="https://longhouse.ai/#landing-install"
        style={{ color: 'var(--color-brand-primary)', textDecoration: 'underline', textUnderlineOffset: '2px', fontWeight: 600 }}
      >
        Sign up for free
      </a>
    </div>
  );
}
