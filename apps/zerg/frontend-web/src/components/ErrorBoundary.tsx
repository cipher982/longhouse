import React from 'react';

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
  errorInfo?: React.ErrorInfo;
}

interface ErrorBoundaryProps {
  children: React.ReactNode;
  fallback?: React.ComponentType<{ error?: Error; retry: () => void }>;
}

// Default error fallback component
function DefaultErrorFallback({
  error,
  retry,
}: {
  error?: Error;
  retry: () => void;
}) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '400px',
      padding: '32px',
      backgroundColor: 'var(--dark-card, #2a2a3a)',
      border: '1px solid var(--border-color, #3d3d5c)',
      borderRadius: 'var(--radius-lg, 8px)',
      margin: '32px',
      textAlign: 'center',
    }}>
      <div style={{
        fontSize: '48px',
        marginBottom: '16px',
        opacity: 0.6,
      }}>
        ⚠️
      </div>
      <h2 style={{
        color: '#ef4444',
        fontSize: '20px',
        fontWeight: '600',
        margin: '0 0 12px 0',
      }}>
        Something went wrong
      </h2>
      <p style={{
        color: 'var(--text-secondary, #e0e0e0)',
        fontSize: '16px',
        margin: '0 0 24px 0',
        maxWidth: '500px',
        lineHeight: 1.5,
      }}>
        {error?.message || 'An unexpected error occurred. Please try refreshing the page or contact support if the problem persists.'}
      </p>
      <div style={{ display: 'flex', gap: '12px' }}>
        <button
          onClick={retry}
          style={{
            background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)',
            border: 'none',
            color: 'white',
            padding: '10px 20px',
            borderRadius: 'var(--radius-sm, 4px)',
            fontSize: '14px',
            fontWeight: '500',
            cursor: 'pointer',
            transition: 'all 0.2s ease',
          }}
          onMouseOver={(e) => {
            e.currentTarget.style.background = 'linear-gradient(135deg, #059669 0%, #047857 100%)';
            e.currentTarget.style.transform = 'translateY(-1px)';
          }}
          onMouseOut={(e) => {
            e.currentTarget.style.background = 'linear-gradient(135deg, #10b981 0%, #059669 100%)';
            e.currentTarget.style.transform = 'translateY(0)';
          }}
        >
          Try Again
        </button>
        <button
          onClick={() => window.location.reload()}
          style={{
            background: 'var(--dark-lighter, #33334a)',
            border: '1px solid var(--border-color, #3d3d5c)',
            color: 'var(--text-secondary, #e0e0e0)',
            padding: '10px 20px',
            borderRadius: 'var(--radius-sm, 4px)',
            fontSize: '14px',
            fontWeight: '500',
            cursor: 'pointer',
            transition: 'all 0.2s ease',
          }}
          onMouseOver={(e) => {
            e.currentTarget.style.background = 'var(--dark-card, #2a2a3a)';
            e.currentTarget.style.color = 'var(--text, #ffffff)';
          }}
          onMouseOut={(e) => {
            e.currentTarget.style.background = 'var(--dark-lighter, #33334a)';
            e.currentTarget.style.color = 'var(--text-secondary, #e0e0e0)';
          }}
        >
          Reload Page
        </button>
      </div>
      {import.meta.env.MODE === 'development' && error && (
        <details style={{
          marginTop: '24px',
          padding: '16px',
          background: 'rgba(0, 0, 0, 0.3)',
          borderRadius: 'var(--radius-sm, 4px)',
          border: '1px solid var(--border-color, #3d3d5c)',
          fontSize: '12px',
          fontFamily: 'Monaco, Menlo, monospace',
          color: 'var(--text-secondary, #e0e0e0)',
          textAlign: 'left',
          maxWidth: '600px',
        }}>
          <summary style={{ cursor: 'pointer', marginBottom: '8px' }}>
            Error Details (Development Mode)
          </summary>
          <pre style={{
            margin: 0,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            maxHeight: '200px',
            overflow: 'auto',
          }}>
            {error.stack}
          </pre>
        </details>
      )}
    </div>
  );
}

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return {
      hasError: true,
      error,
    };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);

    // Log error to external service in production
    if (import.meta.env.MODE === 'production') {
      // Log to console for now - add Sentry/LogRocket when needed
      console.error('Production error:', {
        error: error.message,
        stack: error.stack,
        componentStack: errorInfo.componentStack,
        timestamp: new Date().toISOString(),
      });
    }

    this.setState({
      hasError: true,
      error,
      errorInfo,
    });
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: undefined, errorInfo: undefined });
  };

  render() {
    if (this.state.hasError) {
      const FallbackComponent = this.props.fallback || DefaultErrorFallback;
      return <FallbackComponent error={this.state.error} retry={this.handleRetry} />;
    }

    return this.props.children;
  }
}

// Higher-order component for easy error boundary wrapping
export function withErrorBoundary<P extends object>(
  Component: React.ComponentType<P>,
  fallback?: React.ComponentType<{ error?: Error; retry: () => void }>
) {
  const WrappedComponent = (props: P) => (
    <ErrorBoundary fallback={fallback}>
      <Component {...props} />
    </ErrorBoundary>
  );

  WrappedComponent.displayName = `withErrorBoundary(${Component.displayName || Component.name})`;
  return WrappedComponent;
}
