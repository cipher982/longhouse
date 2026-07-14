import { useId } from "react";
import type { SVGProps } from "react";
import {
  lookupProviderBrand,
  hexToRgb,
  parseHexAlpha,
  normalizeProviderKey,
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
      <path d="M20.562 10.188c.25-.688.313-1.376.25-2.063-.062-.687-.312-1.375-.625-2-.562-.937-1.375-1.687-2.312-2.125-1-.437-2.063-.562-3.125-.312-.5-.5-1.063-.938-1.688-1.25S11.687 2 11 2a5.17 5.17 0 0 0-3 .938c-.875.624-1.5 1.5-1.813 2.5-.75.187-1.375.5-2 .875-.562.437-1 1-1.375 1.562-.562.938-.75 2-.625 3.063a5.44 5.44 0 0 0 1.25 2.874a4.7 4.7 0 0 0-.25 2.063c.063.688.313 1.375.625 2 .563.938 1.375 1.688 2.313 2.125 1 .438 2.062.563 3.125.313.5.5 1.062.937 1.687 1.25S12.312 22 13 22a5.17 5.17 0 0 0 3-.937c.875-.625 1.5-1.5 1.812-2.5a4.54 4.54 0 0 0 1.938-.875c.562-.438 1.062-.938 1.375-1.563.562-.937.75-2 .625-3.062-.125-1.063-.5-2.063-1.188-2.876M13.062 20.688c-1 0-1.75-.313-2.437-.875l.125-.063 4-2.312a.5.5 0 0 0 .25-.25.57.57 0 0 0 .062-.313V11.25l1.688 1v4.625a3.685 3.685 0 0 1-3.688 3.813M5 17.25c-.438-.75-.625-1.625-.438-2.5l.125.063 4 2.312a.56.56 0 0 0 .313.063c.125 0 .25 0 .312-.063l4.875-2.812v1.937l-4.062 2.375A3.7 3.7 0 0 1 7.312 19c-1-.25-1.812-.875-2.312-1.75M3.937 8.563a3.8 3.8 0 0 1 1.938-1.626v4.751c0 .124 0 .25.062.312a.5.5 0 0 0 .25.25l4.875 2.813-1.687 1-4-2.313a3.7 3.7 0 0 1-1.75-2.25c-.25-.937-.188-2.062.312-2.937m13.813 3.187-4.875-2.812 1.687-1 4 2.312c.625.375 1.125.875 1.438 1.5s.5 1.313.437 2.063a3.7 3.7 0 0 1-.75 1.937c-.437.563-1 1-1.687 1.25v-4.75c0-.125 0-.25-.063-.312 0 0-.062-.126-.187-.188m1.687-2.5-.125-.062-4-2.313c-.125-.062-.187-.062-.312-.062s-.25 0-.313.062L9.812 9.688V7.75l4.063-2.375c.625-.375 1.312-.5 2.062-.5.688 0 1.375.25 2 .688.563.437 1.063 1 1.313 1.625s.312 1.375.187 2.062m-10.5 3.5-1.687-1V7.063c0-.688.187-1.438.562-2C8.187 4.438 8.75 4 9.375 3.688a3.37 3.37 0 0 1 2.062-.313c.688.063 1.375.375 1.938.813l-.125.062-4 2.313a.5.5 0 0 0-.25.25c-.063.125-.063.187-.063.312zm.875-2L12 9.5l2.187 1.25v2.5L12 14.5l-2.188-1.25z" />
    </svg>
  );
}

/** Claude — terracotta sunburst. Single path, uses currentColor. */
function ClaudeMark() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" />
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

const PROVIDER_DISPLAY: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  openai: "OpenAI",
  opencode: "OpenCode",
  antigravity: "Antigravity",
  cursor: "Cursor",
  zai: "Z.ai",
};

function MarkFor(provider: string) {
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
  const title = PROVIDER_DISPLAY[key] ?? (provider ? provider : "Session");
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
      {MarkFor(key)}
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
