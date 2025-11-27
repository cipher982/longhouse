import React from "react";

interface EmptyStateIllustrationProps {
  className?: string;
}

export function EmptyStateIllustration({ className }: EmptyStateIllustrationProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 200 200"
      className={className}
      fill="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="grad1" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.1" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0.05" />
        </linearGradient>
      </defs>
      
      {/* Background Circle */}
      <circle cx="100" cy="100" r="80" fill="url(#grad1)" />
      
      {/* Robot Head Shape - Hexagon-ish */}
      <path
        d="M60 70 L70 50 H130 L140 70 V130 L130 150 H70 L60 130 Z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      
      {/* Eyes */}
      <circle cx="85" cy="95" r="8" fill="currentColor" opacity="0.8" />
      <circle cx="115" cy="95" r="8" fill="currentColor" opacity="0.8" />
      
      {/* Eye Glow (subtle) */}
      <circle cx="85" cy="95" r="4" fill="#fff" opacity="0.4" />
      <circle cx="115" cy="95" r="4" fill="#fff" opacity="0.4" />

      {/* Antenna */}
      <line x1="100" y1="50" x2="100" y2="30" stroke="currentColor" strokeWidth="2" />
      <circle cx="100" cy="25" r="5" stroke="currentColor" strokeWidth="2" fill="none" />
      <circle cx="100" cy="25" r="2" fill="currentColor" />

      {/* Mouth area */}
      <path
        d="M80 125 H120"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        opacity="0.6"
      />
      
      {/* Tech markings */}
      <path d="M50 100 H40" stroke="currentColor" strokeWidth="2" opacity="0.4" />
      <path d="M150 100 H160" stroke="currentColor" strokeWidth="2" opacity="0.4" />
      <circle cx="70" cy="140" r="2" fill="currentColor" opacity="0.6" />
      <circle cx="130" cy="140" r="2" fill="currentColor" opacity="0.6" />
    </svg>
  );
}

