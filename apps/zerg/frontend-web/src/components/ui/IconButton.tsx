import React from 'react';
import clsx from 'clsx';

interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  children: React.ReactNode;
}

export const IconButton: React.FC<IconButtonProps> = ({
  className,
  children,
  ...props
}) => {
  return (
    <button className={clsx('ui-icon-button', className)} {...props}>
      {children}
    </button>
  );
};

