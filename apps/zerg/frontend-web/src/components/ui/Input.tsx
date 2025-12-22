import React from 'react';
import clsx from 'clsx';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  // Use interface to allow extension if needed, but must have at least one property or be a type
  className?: string;
}

export const Input: React.FC<InputProps> = ({ className, ...props }) => {
  return <input className={clsx('ui-input', className)} {...props} />;
};
