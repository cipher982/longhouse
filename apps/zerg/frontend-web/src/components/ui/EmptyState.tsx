import React from 'react';
import clsx from 'clsx';

interface EmptyStateProps {
  variant?: 'default' | 'error';
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  variant = 'default',
  icon,
  title,
  description,
  action,
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
    </div>
  );
};

