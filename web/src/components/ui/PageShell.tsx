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
    <div className="page-shell">
      <div className={clsx('page-shell-content', `page-shell-content--${size}`, className)}>
        {children}
      </div>
    </div>
  );
};
