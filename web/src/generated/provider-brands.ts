// @generated from schemas/managed_providers.yml and config/provider-brands.json — do not edit by hand.
// Run: python3 scripts/generate/provider_brands.py

export interface ProviderBrandConfig {
  displayName: string;
  marketingName: string;
  brand: string;
  glyphStyle: "original" | "template";
  markColor: string | null;
  chipFillType: "brand_alpha" | "solid";
  chipFillAlpha: number | null;
  chipFillColor: string | null;
  chipStrokeType: "brand_alpha" | "solid";
  chipStrokeAlpha: number | null;
  chipStrokeColor: string | null;
  chipStrokeWidth: number;
  cornerRadiusFactor: number;
  aliases: string[];
}

export const DEFAULT_PROVIDER_BRAND = "#9A8F7E";

const PROVIDER_BRANDS: Record<string, ProviderBrandConfig> = {
  "claude": {
    displayName: "Claude",
    marketingName: "Claude Code",
    brand: "#D97757",
    glyphStyle: "original",
    markColor: null,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: null,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: null,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["claude-code"],
  },
  "antigravity": {
    displayName: "Antigravity",
    marketingName: "Antigravity CLI",
    brand: "#4F87ED",
    glyphStyle: "original",
    markColor: null,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: null,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: null,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["gemini", "agy", "google-antigravity"],
  },
  "codex": {
    displayName: "Codex",
    marketingName: "Codex CLI",
    brand: "#F3EAD9",
    glyphStyle: "template",
    markColor: "#FFFFFFEB",
    chipFillType: "solid",
    chipFillAlpha: null,
    chipFillColor: "#141517",
    chipStrokeType: "solid",
    chipStrokeAlpha: null,
    chipStrokeColor: "#FFFFFF52",
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.5,
    aliases: ["openai", "codex-cli", "openai-codex"],
  },
  "opencode": {
    displayName: "OpenCode",
    marketingName: "OpenCode",
    brand: "#C9C4C4",
    glyphStyle: "template",
    markColor: "#85D1FA",
    chipFillType: "solid",
    chipFillAlpha: null,
    chipFillColor: "#1F2B38",
    chipStrokeType: "solid",
    chipStrokeAlpha: null,
    chipStrokeColor: "#66BDEB73",
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.18,
    aliases: ["open-code"],
  },
  "cursor": {
    displayName: "Cursor",
    marketingName: "Cursor Agent",
    brand: "#14120B",
    glyphStyle: "template",
    markColor: "#EDECEC",
    chipFillType: "solid",
    chipFillAlpha: null,
    chipFillColor: "#14120B",
    chipStrokeType: "solid",
    chipStrokeAlpha: null,
    chipStrokeColor: "#FFFFFF47",
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["cursor-agent"],
  },
  "zai": {
    displayName: "Z.ai",
    marketingName: "Z.ai",
    brand: "#B06E8A",
    glyphStyle: "original",
    markColor: null,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: null,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: null,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["z.ai"],
  },
  "pi": {
    displayName: "Pi",
    marketingName: "Pi Agent",
    brand: "#A855F7",
    glyphStyle: "original",
    markColor: null,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: null,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: null,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["pi-agent"],
  },
  "omp": {
    displayName: "OMP",
    marketingName: "Oh My Pi",
    brand: "#F97316",
    glyphStyle: "original",
    markColor: null,
    chipFillType: "brand_alpha",
    chipFillAlpha: 0.16,
    chipFillColor: null,
    chipStrokeType: "brand_alpha",
    chipStrokeAlpha: 0.22,
    chipStrokeColor: null,
    chipStrokeWidth: 0.5,
    cornerRadiusFactor: 0.28,
    aliases: ["oh-my-pi", "oh my pi", "ohmypi"],
  },
};

const PROVIDER_ALIASES: Record<string, string> = {
  "claude-code": "claude",
  "gemini": "antigravity",
  "agy": "antigravity",
  "google-antigravity": "antigravity",
  "openai": "codex",
  "codex-cli": "codex",
  "openai-codex": "codex",
  "open-code": "opencode",
  "cursor-agent": "cursor",
  "z.ai": "zai",
  "pi-agent": "pi",
  "oh-my-pi": "omp",
  "oh my pi": "omp",
  "ohmypi": "omp",
};

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  "claude": "Claude",
  "claude-code": "Claude",
  "antigravity": "Antigravity",
  "gemini": "Antigravity",
  "agy": "Antigravity",
  "google-antigravity": "Antigravity",
  "codex": "Codex",
  "openai": "OpenAI",
  "codex-cli": "Codex",
  "openai-codex": "Codex",
  "opencode": "OpenCode",
  "open-code": "OpenCode",
  "cursor": "Cursor",
  "cursor-agent": "Cursor",
  "zai": "Z.ai",
  "z.ai": "Z.ai",
  "pi": "Pi",
  "pi-agent": "Pi",
  "omp": "OMP",
  "oh-my-pi": "OMP",
  "oh my pi": "OMP",
  "ohmypi": "OMP",
};

const DEFAULT_CONFIG: ProviderBrandConfig = {
  displayName: "Session",
  marketingName: "Session",
  brand: DEFAULT_PROVIDER_BRAND,
  glyphStyle: "original",
  markColor: null,
  chipFillType: "brand_alpha",
  chipFillAlpha: 0.16,
  chipFillColor: null,
  chipStrokeType: "brand_alpha",
  chipStrokeAlpha: 0.22,
  chipStrokeColor: null,
  chipStrokeWidth: 0.5,
  cornerRadiusFactor: 0.28,
  aliases: [],
};

export function normalizeProviderKey(provider: string): string {
  const key = provider.trim().toLowerCase();
  if (PROVIDER_ALIASES[key]) return PROVIDER_ALIASES[key];
  return key;
}

export function lookupProviderBrand(provider: string | null | undefined): ProviderBrandConfig {
  if (!provider) return DEFAULT_CONFIG;
  const key = normalizeProviderKey(provider);
  return PROVIDER_BRANDS[key] ?? DEFAULT_CONFIG;
}

export function providerBrandColor(provider: string | null | undefined): string {
  return lookupProviderBrand(provider).brand;
}

export function providerDisplayName(
  provider: string | null | undefined,
  fallback = "Session",
): string {
  const cleaned = provider?.trim();
  if (!cleaned) return fallback;
  return PROVIDER_DISPLAY_NAMES[cleaned.toLowerCase()]
    ?? cleaned.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function hexToRgb(hex: string): string {
  const m = hex.replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  const n = parseInt(full.slice(0, 6), 16);
  if (Number.isNaN(n)) return "154, 143, 126";
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

export function parseHexAlpha(hex: string): number {
  const m = hex.replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  if (full.length >= 8) {
    return parseInt(full.slice(6, 8), 16) / 255;
  }
  return 1;
}
