/**
 * Sticky banner shown at the top of the page in demo mode.
 * Informs visitors they're viewing a read-only demo.
 */
export default function DemoBanner() {
  return (
    <div
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 9999,
        background: 'linear-gradient(90deg, #C9A66B, #D4B87A)',
        color: '#120B09',
        textAlign: 'center',
        padding: '8px 16px',
        fontSize: '14px',
        fontWeight: 500,
      }}
    >
      You're viewing a live demo &mdash;{' '}
      <a
        href="https://longhouse.ai/pricing"
        style={{ color: '#120B09', textDecoration: 'underline', fontWeight: 600 }}
      >
        Sign up for free
      </a>
    </div>
  );
}
