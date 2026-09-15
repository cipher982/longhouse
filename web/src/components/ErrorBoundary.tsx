import React from 'react';
import { AlertTriangleIcon } from './icons';

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
      backgroundColor: 'var(--color-surface-card)',
      border: '1px solid var(--color-border-primary)',
      borderRadius: 'var(--radius-lg, 8px)',
      margin: '32px',
      textAlign: 'center',
    }}>
      <div style={{
        marginBottom: '16px',
        opacity: 0.85,
        color: 'var(--color-intent-error)',
      }}>
        <AlertTriangleIcon width={48} height={48} />
      </div>
      <h2 style={{
        color: 'var(--color-text-primary)',
        fontSize: '20px',
        fontWeight: '600',
        margin: '0 0 12px 0',
      }}>
        Something went wrong
      </h2>
      <p style={{
        color: 'var(--color-text-secondary)',
        fontSize: '16px',
        margin: '0 0 24px 0',
        maxWidth: '500px',
        lineHeight: 1.5,
      }}>
        {error?.message || 'An unexpected error occurred. Please try refreshing the page or contact support if the problem persists.'}
      </p>
      <div style={{ display: 'flex', gap: '12px' }}>
        <button onClick={retry} className="ui-button ui-button--primary">
          Try Again
        </button>
        <button onClick={() => window.location.reload()} className="ui-button ui-button--secondary">
          Reload Page
        </button>
      </div>
      {import.meta.env.MODE === 'development' && error && (
        <details style={{
          marginTop: '24px',
          padding: '16px',
          background: 'var(--color-surface-well)',
          borderRadius: 'var(--radius-sm, 4px)',
          border: '1px solid var(--color-border-primary)',
          fontSize: '12px',
          fontFamily: 'var(--font-family-mono, Monaco, Menlo, monospace)',
          color: 'var(--color-text-muted)',
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
