// @generated from config/provider-brands.json — do not edit by hand.
// Run: python3 scripts/generate/provider_brands.py

export interface ProviderBrandConfig {
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
    aliases: [],
  },
  "antigravity": {
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
    aliases: ["gemini"],
  },
  "codex": {
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
    aliases: ["openai"],
  },
  "opencode": {
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
    aliases: [],
  },
  "cursor": {
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
    aliases: [],
  },
  "zai": {
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
    aliases: [],
  },
};

const PROVIDER_ALIASES: Record<string, string> = {
  "gemini": "antigravity",
  "openai": "codex",
};

const DEFAULT_CONFIG: ProviderBrandConfig = {
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
  const key = provider.toLowerCase();
  if (key === "gemini") return "antigravity";
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
