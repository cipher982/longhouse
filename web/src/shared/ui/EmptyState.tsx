import React from 'react';
import clsx from 'clsx';

interface EmptyStateProps {
  variant?: 'default' | 'error';
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  /** Technical detail for a bug report, collapsed under "Details". Never the headline. */
  details?: string;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  variant = 'default',
  icon,
  title,
  description,
  action,
  details,
  className,
}) => {
  return (
    <div className={clsx('ui-empty-state', variant === 'error' && 'ui-empty-state--error', className)}>
      {icon && <div className="ui-empty-state__icon">{icon}</div>}
      <h3 className="ui-empty-state__title">{title}</h3>
      {description && (
        <p className="ui-empty-state__description">{description}</p>
      )}
      {action && <div className="ui-empty-state__action">{action}</div>}
      {details && (
        <details className="ui-empty-state__details">
          <summary>Details</summary>
          <code>{details}</code>
        </details>
      )}
    </div>
  );
};
