import React from 'react';
import clsx from 'clsx';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  as?: React.ElementType;
  children: React.ReactNode;
  htmlFor?: string; // For label support
}

export const Button: React.FC<ButtonProps> = ({
  variant = 'secondary',
  size = 'md',
  as: Component = 'button',
  className,
  children,
  ...props
}) => {
  return (
    <Component
      className={clsx(
        'ui-button',
        `ui-button--${variant}`,
        `ui-button--${size}`,
        className
      )}
      {...props}
    >
      {children}
    </Component>
  );
};

