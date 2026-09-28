import { SCENE_GLYPHS, SCENE_MATERIAL, SCENE_SPEC, type SceneProfileKey } from "./sceneSpec";
import type { SourceRaster } from "./sourceRenderer";

type GlyphSignature = {
  glyph: (typeof SCENE_GLYPHS)[number];
  samples: readonly [number, number, number, number, number, number];
};

type EdgeEvidence = {
  x: number;
  y: number;
};

const GLYPH_SIGNATURES: readonly GlyphSignature[] = [
  { glyph: ".", samples: [0, 0, 0, 0, 0.05, 0.34] },
  { glyph: "-", samples: [0, 0, 0.68, 0.68, 0, 0] },
  { glyph: "|", samples: [0.26, 0.26, 0.72, 0.72, 0.26, 0.26] },
  { glyph: "/", samples: [0, 0.7, 0.34, 0.34, 0.7, 0] },
  { glyph: "\\", samples: [0.7, 0, 0.34, 0.34, 0, 0.7] },
  { glyph: "+", samples: [0.18, 0.18, 0.78, 0.78, 0.28, 0.28] },
  { glyph: "#", samples: [0.58, 0.58, 0.84, 0.84, 0.58, 0.58] },
  { glyph: "@", samples: [0.9, 0.9, 0.94, 0.94, 0.9, 0.9] },
];

const GLYPH_INDEX = new Map(SCENE_GLYPHS.map((glyph, index) => [glyph, index]));

function squaredDistance(samples: readonly number[], signature: readonly number[]): number {
  let distance = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const delta = samples[index] - signature[index];
    distance += delta * delta;
  }
  return distance / samples.length;
}

function edgeGlyph(edge: EdgeEvidence): number | undefined {
  const magnitude = Math.hypot(edge.x, edge.y);
  if (magnitude < 0.13) return undefined;
  const absoluteX = Math.abs(edge.x);
  const absoluteY = Math.abs(edge.y);
  if (absoluteX > absoluteY * 1.7) return GLYPH_INDEX.get("|");
  if (absoluteY > absoluteX * 1.7) return GLYPH_INDEX.get("-");
  return GLYPH_INDEX.get(edge.x * edge.y > 0 ? "/" : "\\");
}

export function selectGlyphIndex(
  samples: readonly [number, number, number, number, number, number],
  edge: EdgeEvidence,
  previousGlyphIndex?: number,
  stabilize = true,
): number {
  const directionalGlyph = edgeGlyph(edge);
  let bestIndex = 0;
  let bestCost = Number.POSITIVE_INFINITY;
  const costs = GLYPH_SIGNATURES.map((signature, index) => {
    let cost = squaredDistance(samples, signature.samples);
    if (directionalGlyph === index) cost *= 0.7;
    if (cost < bestCost) {
      bestCost = cost;
      bestIndex = index;
    }
    return cost;
  });

  if (
    stabilize &&
    previousGlyphIndex !== undefined &&
    previousGlyphIndex >= 0 &&
    previousGlyphIndex < costs.length &&
    costs[previousGlyphIndex] <= bestCost + 0.045
  ) {
    return previousGlyphIndex;
  }
  return bestIndex;
}

function cellAverage(raster: SourceRaster, cellX: number, cellY: number): number {
  let sum = 0;
  for (let sampleY = 0; sampleY < SCENE_SPEC.samplesPerCell.y; sampleY += 1) {
    for (let sampleX = 0; sampleX < SCENE_SPEC.samplesPerCell.x; sampleX += 1) {
      const x = Math.max(0, Math.min(raster.width - 1, cellX * 2 + sampleX));
      const y = Math.max(0, Math.min(raster.height - 1, cellY * 3 + sampleY));
      sum += raster.luminance[y * raster.width + x];
    }
  }
  return sum / 6;
}

function paletteIndex(material: number, glyphIndex: number): number {
  if (material <= SCENE_MATERIAL.void) return 0;
  return 1 + (material - 1) * SCENE_GLYPHS.length + glyphIndex;
}

function unpackGlyphIndex(value: number): number | undefined {
  if (value <= 0) return undefined;
  return (value - 1) % SCENE_GLYPHS.length;
}

export function renderGlyphFrame(
  raster: SourceRaster,
  profileKey: SceneProfileKey,
  previousFrame?: Uint8Array,
): Uint8Array {
  const profile = SCENE_SPEC.profiles[profileKey];
  const frame = new Uint8Array(profile.width * profile.height);

  for (let cellY = 0; cellY < profile.height; cellY += 1) {
    for (let cellX = 0; cellX < profile.width; cellX += 1) {
      const samples = [] as number[];
      let strongest = 0;
      let material: number = SCENE_MATERIAL.void;
      let hasEmissiveSample = false;
      for (let sampleY = 0; sampleY < 3; sampleY += 1) {
        for (let sampleX = 0; sampleX < 2; sampleX += 1) {
          const sourceIndex = (cellY * 3 + sampleY) * raster.width + cellX * 2 + sampleX;
          const value = raster.luminance[sourceIndex];
          samples.push(value);
          if (value > strongest) {
            strongest = value;
            material = raster.material[sourceIndex];
          }
          if (raster.emissive[sourceIndex]) hasEmissiveSample = true;
        }
      }

      const frameIndex = cellY * profile.width + cellX;
      if (strongest < 0.075 || material === SCENE_MATERIAL.void) {
        frame[frameIndex] = 0;
        continue;
      }

      const edge = {
        x: cellAverage(raster, cellX + 1, cellY) - cellAverage(raster, cellX - 1, cellY),
        y: cellAverage(raster, cellX, cellY + 1) - cellAverage(raster, cellX, cellY - 1),
      };
      const previousGlyph = previousFrame ? unpackGlyphIndex(previousFrame[frameIndex]) : undefined;
      const glyphIndex = selectGlyphIndex(
        samples as [number, number, number, number, number, number],
        edge,
        previousGlyph,
        !hasEmissiveSample,
      );
      frame[frameIndex] = paletteIndex(material, glyphIndex);
    }
  }
  return frame;
}

export function getGlyphSignaturesForTest(): readonly GlyphSignature[] {
  return GLYPH_SIGNATURES;
}
