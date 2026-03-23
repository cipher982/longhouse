import React from 'react';
import clsx from 'clsx';

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'glass';
  children: React.ReactNode;
}

export const Card: React.FC<CardProps> & {
  Header: React.FC<{ children: React.ReactNode; className?: string }>;
  Body: React.FC<{ children: React.ReactNode; className?: string }>;
} = ({ variant = 'glass', className, style, onClick, onKeyDown, children, ...rest }) => {
  const interactive = !!onClick;
  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (interactive && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      onClick?.(e as unknown as React.MouseEvent<HTMLDivElement>);
    }
    onKeyDown?.(e);
  };

  return (
    <div
      {...rest}
      className={clsx('ui-card', `ui-card--${variant}`, interactive && 'ui-card--interactive', className)}
      style={style}
      onClick={onClick}
      role={interactive ? 'button' : rest.role}
      tabIndex={interactive ? rest.tabIndex ?? 0 : rest.tabIndex}
      onKeyDown={interactive || onKeyDown ? handleKeyDown : undefined}
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
