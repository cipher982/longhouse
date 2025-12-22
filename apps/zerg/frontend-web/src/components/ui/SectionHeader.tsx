import React from 'react';
import clsx from 'clsx';

interface SectionHeaderProps {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}

export const SectionHeader: React.FC<SectionHeaderProps> = ({
  title,
  description,
  actions,
  className,
}) => {
  return (
    <div className={clsx('ui-section-header', className)}>
      <div className="ui-section-header__content">
        <h2 className="ui-section-header__title">{title}</h2>
        {description && (
          <p className="ui-section-header__description">{description}</p>
        )}
      </div>
      {actions && <div className="ui-section-header__actions">{actions}</div>}
    </div>
  );
};
