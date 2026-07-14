#!/usr/bin/env python3
"""Generate TypeScript and Swift provider-brand modules from config/provider-brands.json.

Single source of truth: config/provider-brands.json.
Outputs:
  - web/src/generated/provider-brands.ts
  - ios/Sources/Shared/ProviderBrands.generated.swift
  - desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/ProviderBrands.generated.swift
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "provider-brands.json"
TS_OUT = REPO / "web" / "src" / "generated" / "provider-brands.ts"
SWIFT_OUT_IOS = REPO / "ios" / "Sources" / "Shared" / "ProviderBrands.generated.swift"
SWIFT_OUT_DESKTOP = (
    REPO
    / "desktop"
    / "LonghouseMenuBarHarness"
    / "Sources"
    / "LonghouseMenuBarCore"
    / "ProviderBrands.generated.swift"
)


def load() -> dict:
    return json.loads(CONFIG.read_text())


def _expand_hex(hex_str: str) -> str:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c + c for c in h)
    if len(h) == 6:
        h = h + "FF"
    return h


def _hex_to_rgba(hex_str: str) -> tuple[int, int, int, float]:
    h = _expand_hex(hex_str)
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    a = int(h[6:8], 16) / 255.0
    return r, g, b, a


def _resolve_providers(data: dict) -> dict:
    defaults = data["defaults"]
    providers = data["providers"]
    resolved = {}
    for key, raw in providers.items():
        entry = {
            "brand": raw.get("brand", defaults["brand"]),
            "glyph_style": raw.get("glyph_style", defaults["glyph_style"]),
            "mark_color": raw.get("mark_color", defaults["mark_color"]),
            "chip": {**defaults["chip"], **raw.get("chip", {})},
            "aliases": raw.get("aliases", []),
        }
        resolved[key] = entry
    return resolved


def _ts_bool(v: bool) -> str:
    return "true" if v else "false"


def _ts_nullable_hex(v: str | None) -> str:
    if v is None:
        return "null"
    return json.dumps(v)


def render_ts(data: dict) -> str:
    defaults = data["defaults"]
    providers = _resolve_providers(data)

    entries = []
    for key, p in providers.items():
        fill = p["chip"]["fill"]
        stroke = p["chip"]["stroke"]
        entries.append(
            f'  {json.dumps(key)}: {{\n'
            f'    brand: {json.dumps(p["brand"])},\n'
            f'    glyphStyle: {json.dumps(p["glyph_style"])},\n'
            f'    markColor: {_ts_nullable_hex(p["mark_color"])},\n'
            f'    chipFillType: {json.dumps(fill["type"])},\n'
            f'    chipFillAlpha: {fill.get("alpha", "null")},\n'
            f'    chipFillColor: {json.dumps(fill.get("color", None)) if fill.get("color") else "null"},\n'
            f'    chipStrokeType: {json.dumps(stroke["type"])},\n'
            f'    chipStrokeAlpha: {stroke.get("alpha", "null")},\n'
            f'    chipStrokeColor: {json.dumps(stroke.get("color", None)) if stroke.get("color") else "null"},\n'
            f'    chipStrokeWidth: {stroke["width"]},\n'
            f'    cornerRadiusFactor: {p["chip"]["corner_radius_factor"]},\n'
            f'    aliases: {json.dumps(p["aliases"])},\n'
            f"  }},"
        )

    alias_map_entries = []
    for key, p in providers.items():
        for alias in p["aliases"]:
            alias_map_entries.append(f"  {json.dumps(alias)}: {json.dumps(key)},")

    return f"""// @generated from config/provider-brands.json — do not edit by hand.
// Run: python3 scripts/generate/provider_brands.py

export interface ProviderBrandConfig {{
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
}}

export const DEFAULT_PROVIDER_BRAND = {json.dumps(defaults["brand"])};

const PROVIDER_BRANDS: Record<string, ProviderBrandConfig> = {{
{chr(10).join(entries)}
}};

const PROVIDER_ALIASES: Record<string, string> = {{
{chr(10).join(alias_map_entries)}
}};

const DEFAULT_CONFIG: ProviderBrandConfig = {{
  brand: DEFAULT_PROVIDER_BRAND,
  glyphStyle: {json.dumps(defaults["glyph_style"])},
  markColor: null,
  chipFillType: {json.dumps(defaults["chip"]["fill"]["type"])},
  chipFillAlpha: {defaults["chip"]["fill"]["alpha"]},
  chipFillColor: null,
  chipStrokeType: {json.dumps(defaults["chip"]["stroke"]["type"])},
  chipStrokeAlpha: {defaults["chip"]["stroke"]["alpha"]},
  chipStrokeColor: null,
  chipStrokeWidth: {defaults["chip"]["stroke"]["width"]},
  cornerRadiusFactor: {defaults["chip"]["corner_radius_factor"]},
  aliases: [],
}};

export function normalizeProviderKey(provider: string): string {{
  const key = provider.toLowerCase();
  if (key === "gemini") return "antigravity";
  if (PROVIDER_ALIASES[key]) return PROVIDER_ALIASES[key];
  return key;
}}

export function lookupProviderBrand(provider: string | null | undefined): ProviderBrandConfig {{
  if (!provider) return DEFAULT_CONFIG;
  const key = normalizeProviderKey(provider);
  return PROVIDER_BRANDS[key] ?? DEFAULT_CONFIG;
}}

export function providerBrandColor(provider: string | null | undefined): string {{
  return lookupProviderBrand(provider).brand;
}}

export function hexToRgb(hex: string): string {{
  const m = hex.replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  const n = parseInt(full.slice(0, 6), 16);
  if (Number.isNaN(n)) return "154, 143, 126";
  return `${{(n >> 16) & 255}}, ${{(n >> 8) & 255}}, ${{n & 255}}`;
}}

export function parseHexAlpha(hex: string): number {{
  const m = hex.replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  if (full.length >= 8) {{
    return parseInt(full.slice(6, 8), 16) / 255;
  }}
  return 1;
}}
"""


def _swift_color(hex_str: str) -> str:
    """Render a hex color string as a Swift Color initializer."""
    r, g, b, a = _hex_to_rgba(hex_str)
    rf = r / 255.0
    gf = g / 255.0
    bf = b / 255.0
    return f"Color(red: {rf:.6g}, green: {gf:.6g}, blue: {bf:.6g}, opacity: {a:.6g})"


def _swift_color_opt(hex_str: str | None) -> str:
    if hex_str is None:
        return "nil"
    return _swift_color(hex_str)


def render_swift(data: dict) -> str:
    defaults = data["defaults"]
    providers = _resolve_providers(data)

    # Per-provider static config properties
    config_props = []
    # Lookup switch cases (string key -> static property)
    lookup_cases = []
    for key, p in providers.items():
        fill = p["chip"]["fill"]
        stroke = p["chip"]["stroke"]
        brand = p["brand"]
        fill_color = _swift_color_opt(fill.get("color"))
        stroke_color = _swift_color_opt(stroke.get("color"))

        lookup_cases.append(f"        case {json.dumps(key)}: return {key}")
        for alias in p["aliases"]:
            lookup_cases.append(f"        case {json.dumps(alias)}: return {key}")

        config_props.append(
            f"    static let {key} = ProviderBrandConfig(\n"
            f"        brand: {_swift_color(brand)},\n"
            f'        glyphStyle: {json.dumps(p["glyph_style"])},\n'
            f"        markColor: {_swift_color_opt(p['mark_color'])},\n"
            f'        chipFillType: {json.dumps(fill["type"])},\n'
            f"        chipFillAlpha: {fill.get('alpha', 'nil')},\n"
            f"        chipFillColor: {fill_color},\n"
            f'        chipStrokeType: {json.dumps(stroke["type"])},\n'
            f"        chipStrokeAlpha: {stroke.get('alpha', 'nil')},\n"
            f"        chipStrokeColor: {stroke_color},\n"
            f"        chipStrokeWidth: {stroke['width']},\n"
            f"        cornerRadiusFactor: {p['chip']['corner_radius_factor']},\n"
            f"    )"
        )

    default_fill = defaults["chip"]["fill"]
    default_stroke = defaults["chip"]["stroke"]

    return f"""// @generated from config/provider-brands.json — do not edit by hand.
// Run: python3 scripts/generate/provider_brands.py

import SwiftUI

public struct ProviderBrandConfig: Sendable {{
    public let brand: Color
    public let glyphStyle: String
    public let markColor: Color?
    public let chipFillType: String
    public let chipFillAlpha: Double?
    public let chipFillColor: Color?
    public let chipStrokeType: String
    public let chipStrokeAlpha: Double?
    public let chipStrokeColor: Color?
    public let chipStrokeWidth: Double
    public let cornerRadiusFactor: Double
}}

private let defaultConfig = ProviderBrandConfig(
    brand: {_swift_color(defaults["brand"])},
    glyphStyle: {json.dumps(defaults["glyph_style"])},
    markColor: nil,
    chipFillType: {json.dumps(default_fill["type"])},
    chipFillAlpha: {default_fill["alpha"]},
    chipFillColor: nil,
    chipStrokeType: {json.dumps(default_stroke["type"])},
    chipStrokeAlpha: {default_stroke["alpha"]},
    chipStrokeColor: nil,
    chipStrokeWidth: {default_stroke["width"]},
    cornerRadiusFactor: {defaults["chip"]["corner_radius_factor"]},
)

public enum ProviderBrands {{
    public static func lookup(_ provider: String?) -> ProviderBrandConfig {{
        guard let provider, !provider.isEmpty else {{ return defaultConfig }}
        let raw = provider.lowercased()
        let key = raw == "gemini" ? "antigravity" : raw
        switch key {{
{chr(10).join(lookup_cases)}
        default: return defaultConfig
        }}
    }}

{chr(10).join(config_props)}
}}
"""


def main() -> int:
    data = load()
    ts_src = render_ts(data)
    swift_src = render_swift(data)

    TS_OUT.parent.mkdir(parents=True, exist_ok=True)
    SWIFT_OUT_IOS.parent.mkdir(parents=True, exist_ok=True)
    SWIFT_OUT_DESKTOP.parent.mkdir(parents=True, exist_ok=True)

    TS_OUT.write_text(ts_src)
    SWIFT_OUT_IOS.write_text(swift_src)
    SWIFT_OUT_DESKTOP.write_text(swift_src)

    print(f"wrote {TS_OUT}")
    print(f"wrote {SWIFT_OUT_IOS}")
    print(f"wrote {SWIFT_OUT_DESKTOP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
