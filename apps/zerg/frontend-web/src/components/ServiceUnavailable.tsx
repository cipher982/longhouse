import React from 'react';

interface ServiceUnavailableProps {
  retryCount: number;
  onRetry: () => void;
}

/**
 * Friendly overlay shown when the backend service is unavailable.
 * This replaces error messages during deployment/restart scenarios.
 */
export function ServiceUnavailable({ retryCount, onRetry }: ServiceUnavailableProps) {
  const isFirstAttempt = retryCount <= 1;

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: 'linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 9999,
      }}
    >
      <div
        style={{
          textAlign: 'center',
          padding: '3rem',
          maxWidth: '400px',
        }}
      >
        {/* Pulsing dot indicator */}
        <div
          style={{
            marginBottom: '1.5rem',
            display: 'flex',
            justifyContent: 'center',
            gap: '8px',
          }}
        >
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                width: '12px',
                height: '12px',
                borderRadius: '50%',
                background: '#10b981',
                animation: 'pulse 1.4s ease-in-out infinite',
                animationDelay: `${i * 0.2}s`,
              }}
            />
          ))}
        </div>

        <h2
          style={{
            color: '#fff',
            fontSize: '1.5rem',
            fontWeight: 600,
            marginBottom: '0.75rem',
            fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
          }}
        >
          {isFirstAttempt ? 'Connecting to server...' : 'Reconnecting...'}
        </h2>

        <p
          style={{
            color: 'rgba(255, 255, 255, 0.7)',
            fontSize: '1rem',
            lineHeight: 1.6,
            marginBottom: '1.5rem',
            fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
          }}
        >
          {isFirstAttempt
            ? 'The server is starting up. This usually takes a few seconds.'
            : `Still connecting... (attempt ${retryCount})`}
        </p>

        {retryCount > 3 && (
          <button
            onClick={onRetry}
            style={{
              background: 'rgba(255, 255, 255, 0.1)',
              border: '1px solid rgba(255, 255, 255, 0.2)',
              color: '#fff',
              padding: '0.75rem 1.5rem',
              borderRadius: '8px',
              fontSize: '0.875rem',
              fontWeight: 500,
              cursor: 'pointer',
              transition: 'all 0.2s ease',
              fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.background = 'rgba(255, 255, 255, 0.15)';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.background = 'rgba(255, 255, 255, 0.1)';
            }}
          >
            Retry Now
          </button>
        )}

        {/* Pulse animation styles */}
        <style>{`
          @keyframes pulse {
            0%, 100% { opacity: 0.4; transform: scale(0.8); }
            50% { opacity: 1; transform: scale(1); }
          }
        `}</style>
      </div>
    </div>
  );
}
