import React from 'react';

export function HeroVisual({ hideCore = false }: { hideCore?: boolean }) {
  return (
    <svg viewBox="0 0 500 500" className="hero-visual-svg">
      <defs>
        <linearGradient id="hero-core-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#a855f7" />
        </linearGradient>
        <linearGradient id="hero-line-grad" x1="0%" y1="0%" x2="100%" y2="0%" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#6366f1" stopOpacity="0" />
          <stop offset="50%" stopColor="#6366f1" stopOpacity="1" />
          <stop offset="100%" stopColor="#6366f1" stopOpacity="0" />
        </linearGradient>
        <radialGradient id="hero-glow-grad" cx="50%" cy="50%" r="50%" fx="50%" fy="50%">
          <stop offset="0%" stopColor="#6366f1" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#6366f1" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="hero-scan-grad" cx="50%" cy="50%" r="50%" fx="50%" fy="50%">
            <stop offset="0%" stopColor="white" stopOpacity="0" />
            <stop offset="80%" stopColor="white" stopOpacity="0" />
            <stop offset="100%" stopColor="white" stopOpacity="0.05" />
        </radialGradient>
      </defs>

      {/* Deep Background Glow */}
      <circle cx="250" cy="250" r="200" fill="url(#hero-glow-grad)" className="hero-bg-pulse" />

      {/* Rotating Starfield (Background) */}
      <g className="hero-starfield">
        <circle cx="100" cy="100" r="1.5" fill="white" opacity="0.3" />
        <circle cx="400" cy="400" r="1.5" fill="white" opacity="0.3" />
        <circle cx="150" cy="400" r="1" fill="white" opacity="0.2" />
        <circle cx="400" cy="100" r="1" fill="white" opacity="0.2" />
        <circle cx="250" cy="80" r="1" fill="white" opacity="0.2" />
        <circle cx="80" cy="250" r="1" fill="white" opacity="0.2" />
        <circle cx="450" cy="250" r="1.5" fill="#a855f7" opacity="0.4" />
        <circle cx="250" cy="450" r="1.5" fill="#6366f1" opacity="0.4" />
      </g>

      {/* Scanner Sweep Effect */}
      <g className="hero-scanner">
        <path d="M250 250 L500 250 A 250 250 0 0 1 250 500 Z" fill="url(#hero-scan-grad)" opacity="0.3" />
      </g>

      {/* Orbit Rings */}
      <circle cx="250" cy="250" r="160" className="hero-orbit-track" />
      <circle cx="250" cy="250" r="210" className="hero-orbit-track-outer" />
      <circle cx="250" cy="250" r="120" className="hero-orbit-track-inner" />

      {/* Core (Centered at 250, 250) - Optional */}
      {!hideCore && (
        <g transform="translate(250, 250)">
          {/* Outer Hex Frame */}
          <path d="M0 -60 L52 -30 L52 30 L0 60 L-52 30 L-52 -30 Z" fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="1" />

          {/* Main Hex Core */}
          <path d="M0 -50 L43 -25 L43 25 L0 50 L-43 25 L-43 -25 Z" className="hero-hex-core" />

          {/* Inner Geometric Detail */}
          <path d="M0 -30 L26 -15 L26 15 L0 30 L-26 15 L-26 -15 Z" fill="none" stroke="rgba(255,255,255,0.3)" strokeWidth="1" className="hero-hex-inner" />

          {/* Center Pulse */}
          <circle r="8" fill="white" className="hero-core-pulse" />
        </g>
      )}

      {/* Main Orbiting System */}
      <g className="hero-orbit-group">
        {/* ================= ICON 1 (Top) ================= */}
        <path id="path1" d="M250 250 L250 90" className="hero-connector-line" />
        {/* Data Packet */}
        <circle r="3" fill="white">
          <animateMotion dur="3s" repeatCount="indefinite" rotate="auto">
            <mpath href="#path1" />
          </animateMotion>
          <animate attributeName="opacity" values="0;1;0" dur="3s" repeatCount="indefinite" />
          <animate attributeName="fill" values="#fff;#a855f7;#fff" dur="3s" repeatCount="indefinite" />
        </circle>

        <g transform="translate(250, 90)">
          <g className="hero-orbit-icon">
            <g className="hero-icon-scale">
              <circle r="24" className="hero-icon-bg" />
              <path d="M-12 -8 L0 4 L12 -8 M-12 -8 L-12 10 L12 10 L12 -8" stroke="white" strokeWidth="2" fill="none" />
            </g>
          </g>
        </g>

        {/* ================= ICON 2 (Bottom Right) ================= */}
        <path id="path2" d="M250 250 L388 330" className="hero-connector-line" />
        {/* Data Packet */}
        <circle r="3" fill="#ec4899">
          <animateMotion dur="4s" repeatCount="indefinite" rotate="auto" keyPoints="1;0" keyTimes="0;1" calcMode="linear">
            <mpath href="#path2" />
          </animateMotion>
          <animate attributeName="opacity" values="0;1;0" dur="4s" repeatCount="indefinite" />
        </circle>

        <g transform="translate(388, 330)">
          <g className="hero-orbit-icon">
            <g className="hero-icon-scale">
              <circle r="24" className="hero-icon-bg" />
              <path d="M-10 -7 H10 V7 H0 L-5 12 V7 H-10 Z" stroke="white" strokeWidth="2" fill="none" />
            </g>
          </g>
        </g>

        {/* ================= ICON 3 (Bottom Left) ================= */}
        <path id="path3" d="M250 250 L112 330" className="hero-connector-line" />
        {/* Data Packet */}
        <circle r="3" fill="#6366f1">
          <animateMotion dur="3.5s" repeatCount="indefinite" rotate="auto">
            <mpath href="#path3" />
          </animateMotion>
          <animate attributeName="opacity" values="0;1;0" dur="3.5s" repeatCount="indefinite" />
        </circle>

        <g transform="translate(112, 330)">
          <g className="hero-orbit-icon">
            <g className="hero-icon-scale">
              <circle r="24" className="hero-icon-bg" />
              <rect x="-10" y="-9" width="20" height="18" rx="3" stroke="white" strokeWidth="2" fill="none" />
              <line x1="-6" y1="-9" x2="-6" y2="-12" stroke="white" strokeWidth="2" />
              <line x1="6" y1="-9" x2="6" y2="-12" stroke="white" strokeWidth="2" />
            </g>
          </g>
        </g>
      </g>

      {/* Foreground Floating Particles (Subtle) */}
      <g className="hero-particles-fg">
         <circle cx="300" cy="200" r="2" fill="#ec4899" opacity="0.6">
            <animate attributeName="opacity" values="0;0.8;0" dur="4s" repeatCount="indefinite" />
         </circle>
         <circle cx="200" cy="300" r="2" fill="#6366f1" opacity="0.6">
            <animate attributeName="opacity" values="0;0.8;0" dur="5s" begin="2s" repeatCount="indefinite" />
         </circle>
      </g>
    </svg>
  );
}
