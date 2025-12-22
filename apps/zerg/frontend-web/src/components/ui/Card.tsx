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
  return (
    <div
      className={clsx('ui-card', `ui-card--${variant}`, className)}
      style={style}
      onClick={onClick}
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
