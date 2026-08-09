import type { ReactNode } from "react";

interface PhoneFrameProps {
  children: ReactNode;
}

export function PhoneFrame({ children }: PhoneFrameProps) {
  return (
    <div className="phone-frame landing-thesis-phone">
      <div className="phone-frame-screen">{children}</div>
    </div>
  );
}
