import React from 'react';
import clsx from 'clsx';

interface BadgeProps {
  variant?: 'neutral' | 'success' | 'warning' | 'error';
  className?: string;
  children: React.ReactNode;
}

export const Badge: React.FC<BadgeProps> = ({
  variant = 'neutral',
  className,
  children,
}) => {
  return (
    <span className={clsx('ui-badge', `ui-badge--${variant}`, className)}>
      {children}
    </span>
  );
};
