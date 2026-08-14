const STORAGE_KEY = "longhouse.demo.seed.v1";

/** Stable 32-bit FNV-1a plus a final avalanche. */
export function demoHash(...parts: Array<string | number>): number {
  let hash = 0x811c9dc5;
  const source = parts.join("\u001f");
  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x7feb352d);
  hash ^= hash >>> 15;
  hash = Math.imul(hash, 0x846ca68b);
  hash ^= hash >>> 16;
  return hash >>> 0;
}

function freshSeed(): string {
  try {
    const values = new Uint32Array(2);
    globalThis.crypto?.getRandomValues(values);
    if (values[0] || values[1]) {
      return `${values[0].toString(36)}${values[1].toString(36)}`;
    }
  } catch {
    // Privacy modes can restrict crypto and storage independently.
  }
  return `local-${Date.now().toString(36)}`;
}

export function resolveDemoSeed(
  explicitSeed: string | null | undefined,
  storage: Pick<Storage, "getItem" | "setItem"> | null,
): string {
  const explicit = explicitSeed?.trim();
  if (explicit) return explicit.slice(0, 128);

  try {
    const stored = storage?.getItem(STORAGE_KEY)?.trim();
    if (stored) return stored;
  } catch {
    // Continue with an in-memory seed when storage is unavailable.
  }

  const created = freshSeed();
  try {
    storage?.setItem(STORAGE_KEY, created);
  } catch {
    // The mounted component retains this value even if persistence fails.
  }
  return created;
}

function gcd(left: number, right: number): number {
  let a = Math.abs(left);
  let b = Math.abs(right);
  while (b) [a, b] = [b, a % b];
  return a;
}

/** A seeded affine permutation with full coverage and no adjacent repeats. */
export function demoRecipeIndex(seed: string, ordinal: number, count: number): number {
  if (count < 1) throw new Error("demo recipe library must not be empty");
  if (count === 1) return 0;
  const safeOrdinal = Math.max(0, Math.floor(ordinal));
  const offset = demoHash(seed, "recipe-offset") % count;
  let step = 1 + (demoHash(seed, "recipe-step") % (count - 1));
  while (gcd(step, count) !== 1) step = step === count - 1 ? 1 : step + 1;
  return (offset + safeOrdinal * step) % count;
}
