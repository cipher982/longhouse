import React from 'react';
import clsx from 'clsx';

interface CardProps {
  variant?: 'default' | 'glass';
  className?: string;
  style?: React.CSSProperties;
  onClick?: (e: React.MouseEvent) => void;
  children: React.ReactNode;
}

export const Card: React.FC<CardProps> & {
  Header: React.FC<{ children: React.ReactNode; className?: string }>;
  Body: React.FC<{ children: React.ReactNode; className?: string }>;
} = ({ variant = 'glass', className, style, onClick, children }) => {
  const interactive = !!onClick;
  return (
    <div
      className={clsx('ui-card', `ui-card--${variant}`, interactive && 'ui-card--interactive', className)}
      style={style}
      onClick={onClick}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      onKeyDown={interactive ? (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick?.(e as unknown as React.MouseEvent);
        }
      } : undefined}
    >
      {children}
    </div>
  );
};

Card.Header = ({ children, className }) => (
  <div className={clsx('ui-card__header', className)}>{children}</div>
);

Card.Body = ({ children, className }) => (
  <div className={clsx('ui-card__body', className)}>{children}</div>
);
