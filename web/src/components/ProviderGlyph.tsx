import { useId } from "react";
import type { SVGProps } from "react";
import {
  lookupProviderBrand,
  hexToRgb,
  parseHexAlpha,
  normalizeProviderKey,
  providerDisplayName,
} from "../generated/provider-brands";
import type { ProviderBrandConfig } from "../generated/provider-brands";

/**
 * Provider brand glyphs — real logo marks for the AI coding agents Longhouse
 * supports. Single source of truth for how a provider is drawn anywhere in the
 * web app (timeline rows, pickers, session header, landing, observability).
 *
 * Two channels of color, deliberately non-overlapping:
 *   • the glyph carries PROVIDER IDENTITY (the provider's real brand color)
 *   • runtime/status color lives elsewhere (the live dot), never on the glyph
 *
 * Colors and rendering rules are driven by config/provider-brands.json.
 */

export type ProviderGlyphTone = "brand" | "mono";

/** OpenAI / Codex — monochrome blossom mark. Single path, uses currentColor. */
function OpenAIMark() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M20.562 10.188c.25-.688.313-1.376.25-2.063-.062-.687-.312-1.375-.625-2-.562-.937-1.375-1.687-2.312-2.125-1-.437-2.063-.562-3.125-.312-.5-.5-1.063-.938-1.688-1.25S11.687 2 11 2a5.17 5.17 0 0 0-3 .938c-.875.624-1.5 1.5-1.813 2.5-.75.187-1.375.5-2 .875-.562.437-1 1-1.375 1.562-.562.938-.75 2-.625 3.063a5.44 5.44 0 0 0 1.25 2.874a4.7 4.7 0 0 0-.25 2.063c.063.688.313 1.375.625 2 .563.938 1.375 1.688 2.313 2.125 1 .438 2.062.563 3.125.313.5.5 1.062.937 1.687 1.25S12.312 22 13 22a5.17 5.17 0 0 0 3-.937c.875-.625 1.5-1.5 1.812-2.5a4.54 4.54 0 0 0 1.938-.875c.562-.438 1.062-.938 1.375-1.563.562-.937.75-2 .625-3.062-.125-1.063-.5-2.063-1.188-2.876M13.062 20.688c-1 0-1.75-.313-2.437-.875l.125-.063 4-2.312a.5.5 0 0 0 .25-.25.57.57 0 0 0 .062-.313V11.25l-1.5.875v4.5l-2.75 1.563-2.75-1.563v-3.187l-1.5-.875v4.062a.57.57 0 0 0 .063.313.5.5 0 0 0 .25.25l4 2.312.125.063c-.688.562-1.438.875-2.438.875-1.062 0-2.062-.375-2.812-1.063a3.98 3.98 0 0 1-1.25-2.687c0-.5.125-1 .313-1.438.125.063.25.125.375.188l4 2.312a.5.5 0 0 0 .5 0l4-2.312a.5.5 0 0 0 .25-.438v-4.625l-1.5-.875v4.625l-3 1.75-3-1.75V8.75l3-1.75 3 1.75v-1.75l-2.75-1.563a.5.5 0 0 0-.5 0l-4 2.313a.5.5 0 0 0-.25.25.57.57 0 0 0-.063.312v4.625l1.5.875v-4.625l3-1.75 3 1.75v4.625l1.5-.875V9.5a.5.5 0 0 0-.25-.438l-4-2.312-.125-.063c.688-.562 1.438-.875 2.438-.875 1.062 0 2.062.375 2.812 1.063a3.98 3.98 0 0 1 1.25 2.687c0 .5-.125 1-.312 1.438-.125-.063-.25-.125-.375-.188l-4-2.312a.5.5 0 0 0-.5 0l-4 2.312a.5.5 0 0 0-.25.438v4.625l1.5.875v-4.625l3-1.75 3 1.75v4.625l-1.5.875v-4.625l-3-1.75-3 1.75v4.625a.5.5 0 0 0 .25.438l4 2.312.125.063c-.688.562-1.438.875-2.438.875Z" />
    </svg>
  );
}

/** Claude — terracotta sunburst. Single path, uses currentColor. */
function ClaudeMark() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.2139-.3035 2.4528-.176 1.8451-.1275.8439.0728.0546.1579-.0607.2732-.2733 2.4893-1.8942 1.8336-1.4754 1.0319-.7953.589-.1579.8196.2793.3035.5767-.2246.8803-.2793.3279-.9712.7406-1.4511 1.0018-2.1918 1.524-.8559.5524-.0304.1639.1457.0243 1.882-.1821 2.0339-.2307 2.7199-.3522.8196.0607.7042.6374-.0243.9893-.5524.3821-1.0018.0607-2.4893.1882-1.8215.1457-2.0521.2307-3.0083.3218-.3522.0668-.4553.3158-.1457.5524 2.7378 1.524 1.0926.6192 2.5378 1.4511 1.8215 1.0319.8925.589.4553.425.1214.8196-.4614.6496-.7103.1579-.3401-.0486-1.0018-.6192-2.3618-1.439-1.8215-1.0926-2.9134-1.706-.4432-.2307-.1214.1275.1275.1821 1.238 1.5368 1.311 1.524 1.032 1.3356.9107 1.032.5767.7042.164.7892-.3821.6496-.425.1821-.8196-.1214-.261-.1821-.437-.4797-1.3964-1.6339-1.275-1.4633-1.093-1.214-.079-.0243.006.1518-.0971 1.202-.1336 1.275-.079 1.033-.1096 1.238-.4371.6496-.7468.243-.6435-.3035-.2125-.5828.1275-1.208.2307-2.2646.1821-1.1842.1214-1.1296.1457-.8439-.0728-.0607-.1579.0607-2.2282 1.3356-1.8942 1.214-1.032.6192-.8196.4918-.6496.1275-.5767-.3401-.4553-.7103.1457-.7468.3943-.3943.7164-.4797Z" />
    </svg>
  );
}

/** Pi — the official three-color mark from pi.dev, reduced for small UI. */
function PiMark({ mono }: { mono: boolean }) {
  return (
    <svg viewBox="0 0 800 800" aria-hidden="true" focusable="false">
      <path fill={mono ? "currentColor" : "#F09082"} d="M165.29 165.29H517.36V400H400V282.65H165.29Z" />
      <path fill={mono ? "currentColor" : "#4D9ABF"} d="M165.29 282.65H282.65V400H400V517.36H282.65V634.72H165.29Z" />
      <path fill={mono ? "currentColor" : "#F1BE58"} d="M517.36 400H634.72V634.72H517.36Z" />
    </svg>
  );
}

/** OMP — the official Pi mark with its orange plugin connector. */
function OMPMark({ mono }: { mono: boolean }) {
  const gradientId = `omp-mark-${useId().replace(/:/g, "")}`;
  return (
    <svg viewBox="0 0 120 90" aria-hidden="true" focusable="false">
      {!mono && (
        <defs>
          <linearGradient id={gradientId} x1="10" y1="8" x2="110" y2="82" gradientUnits="userSpaceOnUse">
            <stop stopColor="#F044C7" />
            <stop offset="1" stopColor="#6E9BFF" />
          </linearGradient>
        </defs>
      )}
      <rect x="10" y="8" width="100" height="12" rx="2" fill={mono ? "currentColor" : `url(#${gradientId})`} />
      <rect x="25" y="20" width="12" height="62" rx="2" fill={mono ? "currentColor" : `url(#${gradientId})`} />
      <rect x="75" y="20" width="12" height="45" rx="2" fill={mono ? "currentColor" : `url(#${gradientId})`} />
      <rect x="71" y="55" width="20" height="16" rx="3" fill={mono ? "currentColor" : "#F97316"} />
      <rect x="76" y="59" width="3" height="8" rx="1" fill="#0D0D0D" />
      <rect x="82" y="59" width="3" height="8" rx="1" fill="#0D0D0D" />
      <circle cx="18" cy="14" r="2" fill={mono ? "currentColor" : "#F97316"} opacity="0.8" />
      <circle cx="102" cy="14" r="2" fill={mono ? "currentColor" : "#F97316"} opacity="0.8" />
    </svg>
  );
}

/** Z.ai — a compact geometric Z with a spark, kept distinct from the terminal fallback. */
function ZaiMark() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M4 5h16v3.1L9 16h11v3H4v-3.1L15 8H4V5Z" />
      <path d="m18.2 2 .65 1.55L20.4 4.2l-1.55.65-.65 1.55-.65-1.55L16 4.2l1.55-.65L18.2 2Z" />
    </svg>
  );
}

/** OpenCode — grayscale bracket/window frame mark. */
function OpenCodeMark() {
  return (
    <svg viewBox="0 0 512 512" fill="currentColor" aria-hidden="true" focusable="false">
      <path opacity="0.55" d="M320 224V352H192V224H320Z" />
      <path fillRule="evenodd" clipRule="evenodd" d="M384 416H128V96H384V416ZM320 160H192V352H320V160Z" />
    </svg>
  );
}


/** Antigravity — geometric orbit mark (no official icon-only SVG published). */
function AntigravityMark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="3" fill="currentColor" />
      <path
        d="M12 2.5c5.247 0 9.5 4.253 9.5 9.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        opacity="0.5"
      />
      <path
        d="M12 21.5c-5.247 0-9.5-4.253-9.5-9.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        opacity="0.5"
      />
    </svg>
  );
}

/**
 * Cursor — official 2D cube mark from cursor.com/brand (square avatar / favicon).
 * Remapped from brand-logo-8.svg into a 24×24 currentColor path.
 */
function CursorMark() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M19.201 7.497L12.355 3.545C12.135 3.418 11.864 3.418 11.644 3.545L4.799 7.497C4.614 7.604 4.5 7.801 4.5 8.015V15.985C4.5 16.199 4.614 16.396 4.799 16.503L11.645 20.455C11.864 20.582 12.136 20.582 12.355 20.455L19.201 16.503C19.386 16.396 19.5 16.199 19.5 15.985V8.015C19.5 7.801 19.386 7.604 19.201 7.497H19.201ZM18.771 8.335L12.162 19.781C12.118 19.858 12 19.827 12 19.737V12.242C12 12.093 11.92 11.954 11.79 11.879L5.299 8.132C5.222 8.087 5.254 7.969 5.343 7.969H18.56C18.748 7.969 18.865 8.172 18.771 8.335H18.771V8.335Z" />
    </svg>
  );
}

/** Fallback for unknown providers — a simple terminal/code chevron. */
function FallbackMark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d="m8 9 3 3-3 3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M13 15h3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export type ProviderGlyphProps = {
  provider: string | null | undefined;
  /** Glyph diameter in px (the chip is sized to this). Default 18. */
  size?: number;
  /** "chip" wraps the mark in a tinted rounded square; "bare" draws just the mark. */
  variant?: "chip" | "bare";
  /** "brand" uses the provider's real color; "mono" forces a neutral tint. */
  tone?: ProviderGlyphTone;
  className?: string;
  style?: React.CSSProperties;
};

function MarkFor(provider: string, tone: ProviderGlyphTone) {
  const mono = tone === "mono";
  switch (provider) {
    case "codex":
    case "openai":
      return <OpenAIMark />;
    case "claude":
      return <ClaudeMark />;
    case "opencode":
      return <OpenCodeMark />;
    case "antigravity":
      return <AntigravityMark />;
    case "cursor":
      return <CursorMark />;
    case "pi":
      return <PiMark mono={mono} />;
    case "omp":
      return <OMPMark mono={mono} />;
    case "zai":
      return <ZaiMark />;
    default:
      return <FallbackMark />;
  }
}

function resolveChipColor(
  type: string,
  colorHex: string | null,
  alpha: number | null,
  brandHex: string,
): string {
  if (type === "solid" && colorHex) {
    const rgb = hexToRgb(colorHex);
    const a = parseHexAlpha(colorHex);
    return `rgba(${rgb}, ${a})`;
  }
  return `rgba(${hexToRgb(brandHex)}, ${alpha ?? 0.16})`;
}

/**
 * Render a provider's brand glyph. Drop-in anywhere a provider is shown.
 */
export function ProviderGlyph({
  provider,
  size = 18,
  variant = "chip",
  tone = "brand",
  className,
  style,
}: ProviderGlyphProps) {
  const key = normalizeProviderKey(provider ?? "");
  const config = lookupProviderBrand(provider);
  const title = providerDisplayName(provider);
  const monoFallback = "var(--color-text-secondary)";

  const markColor =
    tone === "mono"
      ? monoFallback
      : config.glyphStyle === "template" && config.markColor
        ? config.markColor
        : config.brand;

  const markPx = Math.round(size * (variant === "chip" ? 0.66 : 1));

  const mark = (
    <span
      style={{
        display: "inline-flex",
        width: markPx,
        height: markPx,
        color: markColor,
        lineHeight: 0,
      }}
    >
      {MarkFor(key, tone)}
    </span>
  );

  if (variant === "bare") {
    return (
      <span
        className={className}
        style={{ display: "inline-flex", width: size, height: size, alignItems: "center", justifyContent: "center", ...style }}
        aria-label={title}
        role="img"
      >
        {mark}
      </span>
    );
  }

  const chipBg = resolveChipColor(
    config.chipFillType,
    config.chipFillColor,
    config.chipFillAlpha,
    config.brand,
  );
  const chipBorder = resolveChipColor(
    config.chipStrokeType,
    config.chipStrokeColor,
    config.chipStrokeAlpha,
    config.brand,
  );
  const corner = Math.max(4, Math.round(size * config.cornerRadiusFactor));

  return (
    <span
      className={className}
      style={{
        display: "inline-flex",
        width: size,
        height: size,
        alignItems: "center",
        justifyContent: "center",
        borderRadius: corner,
        background: chipBg,
        boxShadow: `inset 0 0 0 0.5px ${chipBorder}`,
        flex: "0 0 auto",
        ...style,
      }}
      aria-label={title}
      role="img"
      title={title}
    >
      {mark}
    </span>
  );
}

export type { SVGProps };
