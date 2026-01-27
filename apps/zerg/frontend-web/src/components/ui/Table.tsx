import React from 'react';
import clsx from 'clsx';

interface TableProps {
  className?: string;
  children: React.ReactNode;
}

export const Table: React.FC<TableProps> & {
  Header: React.FC<{ children: React.ReactNode; className?: string }>;
  Body: React.FC<{ children: React.ReactNode; className?: string; id?: string }>;
  Row: React.FC<{ children: React.ReactNode; className?: string; onClick?: () => void; onKeyDown?: (e: React.KeyboardEvent<HTMLTableRowElement>) => void; style?: React.CSSProperties; 'aria-expanded'?: boolean | 'true' | 'false'; 'data-fiche-id'?: number }>;
  Cell: React.FC<{ children: React.ReactNode; className?: string; isHeader?: boolean; colSpan?: number; onClick?: () => void; style?: React.CSSProperties; 'data-label'?: string }>;
} = ({ className, children }) => {
  return (
    <div className={clsx('ui-table-container', className)}>
      <table className="ui-table">{children}</table>
    </div>
  );
};

Table.Header = ({ children, className }) => (
  <thead className={className}>
    <tr>{children}</tr>
  </thead>
);

Table.Body = ({ children, className, id }) => (
  <tbody className={className} id={id}>{children}</tbody>
);

Table.Row = ({ children, className, onClick, onKeyDown, style, ...props }) => {
  // Keyboard handler for clickable rows (Enter/Space triggers click)
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (onClick && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      onClick();
    }
    onKeyDown?.(e);
  };

  return (
    <tr
      className={clsx(className, onClick && 'ui-table-row--clickable')}
      onClick={onClick}
      onKeyDown={onClick ? handleKeyDown : onKeyDown}
      style={style}
      tabIndex={onClick ? 0 : undefined}
      // Note: Don't add role="button" as it breaks table semantics.
      // Clickable rows use tabIndex for keyboard focus instead.
      {...props}
    >
      {children}
    </tr>
  );
};

Table.Cell = ({ children, className, isHeader = false, colSpan, onClick, style, ...props }) => {
  if (isHeader) {
    return <th className={className} onClick={onClick} style={style} {...props}>{children}</th>;
  }
  return <td className={className} colSpan={colSpan} onClick={onClick} style={style} {...props}>{children}</td>;
};
