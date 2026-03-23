import React from "react";
import clsx from "clsx";

type SpinnerSize = "sm" | "md" | "lg";

interface SpinnerProps extends React.HTMLAttributes<HTMLSpanElement> {
  size?: SpinnerSize;
  label?: string;
}

export const Spinner: React.FC<SpinnerProps> = ({
  size = "md",
  label = "Loading",
  className,
  ...props
}) => {
  return (
    <span
      className={clsx("ui-spinner", `ui-spinner--${size}`, className)}
      role="status"
      aria-label={label}
      {...props}
    />
  );
};
