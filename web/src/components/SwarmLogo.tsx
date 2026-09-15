import type { ImgHTMLAttributes } from "react";
// Imported so Vite gives the mark a content-hashed URL: a CDN or browser cache
// can never pin an old recolor of it the way /longhouse-logo.svg was pinned.
import longhouseLogo from "../assets/longhouse-logo.svg";

interface SwarmLogoProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, "src" | "alt" | "width" | "height"> {
  size?: number;
}

export function SwarmLogo({ size = 200, className, ...props }: SwarmLogoProps) {
  return (
    <img
      src={longhouseLogo}
      alt="Longhouse"
      width={size}
      height={size}
      className={className}
      {...props}
    />
  );
}
