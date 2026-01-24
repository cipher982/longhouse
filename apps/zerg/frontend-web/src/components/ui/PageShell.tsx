import React from 'react';
import clsx from 'clsx';

type PageShellSize = 'narrow' | 'normal' | 'wide' | 'full';

interface PageShellProps {
  size?: PageShellSize;
  className?: string;
  children: React.ReactNode;
}

export const PageShell: React.FC<PageShellProps> = ({
  size = 'normal',
  className,
  children,
}) => {
  return (
    <div className={clsx('page-shell', `page-shell--${size}`, className)}>
      {children}
    </div>
  );
};
