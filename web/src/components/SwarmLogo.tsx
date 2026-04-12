import type { ImgHTMLAttributes } from "react";

interface SwarmLogoProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, "src" | "alt" | "width" | "height"> {
  size?: number;
}

export function SwarmLogo({ size = 200, className, ...props }: SwarmLogoProps) {
  return (
    <img
      src="/longhouse-logo.svg"
      alt="Longhouse"
      width={size}
      height={size}
      className={className}
      {...props}
    />
  );
}
