import type { SVGProps } from "react";

interface SwarmLogoProps extends SVGProps<SVGSVGElement> {
  size?: number;
}

export function SwarmLogo({ size = 200, className, ...props }: SwarmLogoProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 200 200"
      fill="none"
      width={size}
      height={size}
      className={className}
      {...props}
    >
      <defs>
        {/* Main helmet gradient (purple) */}
        <linearGradient id="helmetGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#a78bfa" />
          <stop offset="50%" stopColor="#8b5cf6" />
          <stop offset="100%" stopColor="#6d28d9" />
        </linearGradient>

        {/* Visor gradient (dark with slight transparency) */}
        <linearGradient id="visorGrad" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stopColor="#374151" />
          <stop offset="100%" stopColor="#1f2937" />
        </linearGradient>

        {/* Glow effect for the swarm icons */}
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="coloredBlur" />
          <feMerge>
            <feMergeNode in="coloredBlur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>

        {/* Highlight gradient */}
        <linearGradient id="highlightGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>

        {/* Striped pattern for swarm icons */}
        <pattern id="stripes" patternUnits="userSpaceOnUse" width="6" height="3">
          <rect width="6" height="1.5" fill="#4ade80" />
          <rect y="1.5" width="6" height="1.5" fill="transparent" />
        </pattern>

        {/* Mask for striped swarm icons */}
        <mask id="stripeMask">
          <rect width="100%" height="100%" fill="url(#stripes)" />
        </mask>
      </defs>

      {/* Main helmet body */}
      <ellipse
        cx="100"
        cy="105"
        rx="65"
        ry="75"
        fill="url(#helmetGrad)"
        stroke="#3b2d6b"
        strokeWidth="3"
      />

      {/* Top helmet panel lines */}
      <path d="M100 30 Q100 60 100 80" stroke="#3b2d6b" strokeWidth="2.5" fill="none" />
      <path d="M60 50 Q80 70 100 80" stroke="#3b2d6b" strokeWidth="2" fill="none" />
      <path d="M140 50 Q120 70 100 80" stroke="#3b2d6b" strokeWidth="2" fill="none" />

      {/* Left ear panel */}
      <ellipse
        cx="42"
        cy="115"
        rx="18"
        ry="30"
        fill="url(#helmetGrad)"
        stroke="#3b2d6b"
        strokeWidth="2.5"
      />

      {/* Right ear panel */}
      <ellipse
        cx="158"
        cy="115"
        rx="18"
        ry="30"
        fill="url(#helmetGrad)"
        stroke="#3b2d6b"
        strokeWidth="2.5"
      />

      {/* Chin panel */}
      <path
        d="M55 145 Q100 185 145 145 L140 165 Q100 195 60 165 Z"
        fill="url(#helmetGrad)"
        stroke="#3b2d6b"
        strokeWidth="2"
      />

      {/* Bottom panel lines */}
      <path d="M70 150 Q100 165 130 150" stroke="#3b2d6b" strokeWidth="2" fill="none" />

      {/* Visor background */}
      <rect
        x="50"
        y="75"
        rx="12"
        ry="12"
        width="100"
        height="55"
        fill="url(#visorGrad)"
        stroke="#5eead4"
        strokeWidth="2"
      />

      {/* Visor inner glow */}
      <rect
        x="52"
        y="77"
        rx="10"
        ry="10"
        width="96"
        height="51"
        fill="none"
        stroke="#5eead4"
        strokeWidth="0.5"
        opacity="0.5"
      />

      {/* Swarm icon - top (head) */}
      <g filter="url(#glow)">
        <ellipse cx="100" cy="88" rx="10" ry="8" fill="#4ade80" mask="url(#stripeMask)" />
        <ellipse
          cx="100"
          cy="88"
          rx="10"
          ry="8"
          fill="none"
          stroke="#4ade80"
          strokeWidth="1"
          opacity="0.8"
        />
      </g>

      {/* Swarm icon - bottom left */}
      <g filter="url(#glow)">
        <ellipse cx="78" cy="112" rx="12" ry="10" fill="#4ade80" mask="url(#stripeMask)" />
        <ellipse
          cx="78"
          cy="112"
          rx="12"
          ry="10"
          fill="none"
          stroke="#4ade80"
          strokeWidth="1"
          opacity="0.8"
        />
      </g>

      {/* Swarm icon - bottom right */}
      <g filter="url(#glow)">
        <ellipse cx="122" cy="112" rx="12" ry="10" fill="#4ade80" mask="url(#stripeMask)" />
        <ellipse
          cx="122"
          cy="112"
          rx="12"
          ry="10"
          fill="none"
          stroke="#4ade80"
          strokeWidth="1"
          opacity="0.8"
        />
      </g>

      {/* Connection lines between swarm icons */}
      <g stroke="#4ade80" strokeWidth="1.5" opacity="0.6" filter="url(#glow)">
        <line x1="92" y1="93" x2="82" y2="105" />
        <line x1="108" y1="93" x2="118" y2="105" />
        <line x1="88" y1="115" x2="112" y2="115" />
      </g>

      {/* Helmet highlight (top reflection) */}
      <ellipse cx="85" cy="50" rx="25" ry="12" fill="url(#highlightGrad)" />

      {/* Small highlight on visor */}
      <ellipse cx="130" cy="85" rx="8" ry="4" fill="#ffffff" opacity="0.15" />
    </svg>
  );
}





